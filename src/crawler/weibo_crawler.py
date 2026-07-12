"""微博爬虫 — 通过 Playwright 在浏览器内调用微博移动端内部 API。

微博移动端 (m.weibo.cn) 的搜索数据通过 Ajax JSON API 加载：
  /api/container/getIndex?containerid=100103type%3D1%26q%3D...&page=1

直接 HTTP 请求被 "Sina Visitor System" 拦截，但通过 Playwright 浏览器内
的 fetch() 调用可以绕过（浏览器自动处理 JS 验证和 cookie）。

用法:
    # 搜索
    items = await search_weibo("下沙租房", limit=50)

    # 多关键词批量搜索
    items = await search_multi_keywords()
"""

import asyncio
import json
import logging
import re
from urllib.parse import quote as url_quote

from playwright.async_api import async_playwright

logger = logging.getLogger("rental.weibo")

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

ANTI_DETECT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
    window.chrome = { runtime: {} };
"""

# 下沙租房关键词（用于深潜搜索）
WEIBO_KEYWORDS = [
    "下沙租房",
    "下沙转租",
    "下沙合租",
    "下沙个人转租",
    "金沙湖租房",
    "下沙房东直租",
    "下沙大学城租房",
    "下沙单间",
    "下沙整租",
]

# 微博 API 的 card_type 含义
CARD_TYPE_WEIBO = 9   # 普通微博帖子
CARD_TYPE_GROUP = 11  # 卡片组（含嵌套卡）


def _extract_weibo_text(text: str) -> str:
    """清理微博文本中的 HTML 标签和特殊字符。"""
    text = re.sub(r"<[^>]+>", "", text)    # HTML 标签
    text = re.sub(r"#.*?#", "", text)       # 话题标签（保留文本）
    text = re.sub(r"&[a-z]+;", " ", text)   # HTML entities
    text = re.sub(r"\s+", " ", text)        # 合并空白
    return text.strip()


def _parse_weibo_card(card: dict) -> dict | None:
    """解析单个微博卡片为房源 item。

    Returns:
        None 如果卡片不含有效内容，否则返回 item dict。
    """
    mblog = card.get("mblog")
    if not mblog:
        return None

    text = _extract_weibo_text(mblog.get("text", ""))
    if len(text) < 15:
        return None

    mid = str(mblog.get("id", ""))
    created_at = mblog.get("created_at", "")

    user = mblog.get("user", {})
    poster = user.get("screen_name", "") or user.get("name", "")

    # 提取图片（优先 large → orj360）
    pics = mblog.get("pics", [])
    images = []
    if isinstance(pics, list):
        for pic in pics:
            if isinstance(pic, dict):
                img_url = pic.get("large", {}).get("url", "") or pic.get("url", "")
                if img_url:
                    images.append(img_url)

    detail_url = f"https://m.weibo.cn/detail/{mid}"

    # 构建 content（pipeline 需要的格式）
    parts = [text]
    if poster:
        parts.append(f"发布者: {poster}")
    content = "\n".join(parts)

    return {
        "url": detail_url,
        "source": "微博",
        "content": content[:4000],
        "publish_time": created_at,
        "poster_id": poster,
        "images": images,
    }


async def search_weibo(
    keyword: str,
    limit: int = 50,
    max_pages: int = 10,
) -> list[dict]:
    """搜索微博帖子。

    使用 Playwright 浏览器内 fetch() 调用 m.weibo.cn 内部 API，
    绕过 Sina Visitor System 的 JS 验证。

    Args:
        keyword: 搜索关键词
        limit: 最大结果数
        max_pages: 最大翻页数（每页约 10-20 条）

    Returns:
        [{"url", "source", "content", "publish_time", "poster_id", "images"}, ...]
    """
    collected: list[dict] = []
    seen_ids: set[str] = set()

    containerid = f"100103type%3D1%26q%3D{url_quote(keyword)}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=MOBILE_UA,
            locale="zh-CN",
        )
        await context.add_init_script(ANTI_DETECT_SCRIPT)
        page = await context.new_page()

        # 先访问首页建立 session（触发 visitor system 验证）
        try:
            await page.goto("https://m.weibo.cn", timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
        except Exception:
            pass  # 即使超时，session 可能已经建立

        # 逐页调用搜索 API
        for pn in range(1, max_pages + 1):
            api_url = (
                f"https://m.weibo.cn/api/container/getIndex"
                f"?containerid={containerid}&page={pn}"
            )

            try:
                json_text = await page.evaluate("""
                    async (url) => {
                        const resp = await fetch(url, {
                            headers: {
                                'X-Requested-With': 'XMLHttpRequest',
                                'Accept': 'application/json',
                            },
                            credentials: 'include'
                        });
                        if (!resp.ok) return null;
                        return await resp.text();
                    }
                """, api_url)

                if not json_text:
                    break

                data = json.loads(json_text)
                if data.get("ok") != 1:
                    break

                cards = data.get("data", {}).get("cards", [])
                if not cards:
                    break

                page_count = 0
                for card in cards:
                    ct = card.get("card_type", 0)

                    if ct == CARD_TYPE_WEIBO:
                        item = _parse_weibo_card(card)
                        if item and item["url"] not in seen_ids:
                            seen_ids.add(item["url"])
                            collected.append(item)
                            page_count += 1

                    elif ct == CARD_TYPE_GROUP:
                        # 卡片组（内含多张子卡）
                        for sub_card in card.get("card_group", []):
                            item = _parse_weibo_card(sub_card)
                            if item and item["url"] not in seen_ids:
                                seen_ids.add(item["url"])
                                collected.append(item)
                                page_count += 1

                logger.debug(f"Weibo page {pn}: {page_count} items (total: {len(collected)})")

                if page_count == 0 or len(collected) >= limit:
                    break

            except Exception as e:
                logger.debug(f"Weibo page {pn} failed: {e}")
                break

        await browser.close()

    logger.info(f"Weibo search '{keyword}': {len(collected)} items")
    return collected


async def search_multi_keywords(
    keywords: list[str] | None = None,
    limit_per_kw: int = 30,
) -> list[dict]:
    """多关键词批量搜索微博。

    与闲鱼关键词矩阵策略相同：用更多关键词换取更全面的覆盖。

    Args:
        keywords: 关键词列表，默认使用 WEIBO_KEYWORDS
        limit_per_kw: 每关键词最大结果数
    """
    if keywords is None:
        keywords = WEIBO_KEYWORDS

    all_items: list[dict] = []
    seen_urls: set[str] = set()

    for kw in keywords:
        try:
            items = await search_weibo(kw, limit=limit_per_kw)
            for item in items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_items.append(item)
        except Exception as e:
            logger.warning(f"Weibo keyword '{kw}' failed: {e}")
            continue

    logger.info(f"Weibo: {len(keywords)} keywords → {len(all_items)} unique items")
    return all_items


# ——— CLI ———
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="微博移动端爬虫")
    parser.add_argument("--search", type=str, help="搜索关键词")
    parser.add_argument("--limit", type=int, default=20, help="最大结果数")
    parser.add_argument("--all", action="store_true", help="全量关键词搜索")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    async def _run():
        if args.all:
            items = await search_multi_keywords()
        elif args.search:
            items = await search_weibo(args.search, limit=args.limit)
        else:
            parser.print_help()
            return

        print(f"\n{'='*60}")
        print(f"Total: {len(items)} items")
        for i, item in enumerate(items[:5]):
            text = item["content"][:100].replace("\n", " ")
            print(f"\n[{i}] {text}")
            print(f"    {item['url']}")
            print(f"    poster: {item.get('poster_id', '')}")
            print(f"    images: {len(item.get('images', []))}")
            print(f"    publish_time: {item.get('publish_time', '')}")

    asyncio.run(_run())
