"""小红书爬虫 — Playwright QR 码登录 + 浏览器内 API 拦截。

小红书搜索 API 需要 X-S/X-S-Common 签名头，由混淆 JS 在浏览器内生成。
本模块不在 Python 侧破解签名，而是让 Playwright 浏览器自动签名、拦截 API 响应。

用法:
    # 首次登录（headful 模式，扫码保存 cookie）
    python -m src.crawler.xhs_crawler --login

    # 搜索
    python -m src.crawler.xhs_crawler --search "下沙租房" --limit 20

    # 多关键词批量
    python -m src.crawler.xhs_crawler --all

架构:
    Phase 1: headful 浏览器 → XHS 自动弹 QR 码 → 用户 App 扫码 → 保存 cookies
    Phase 2: headless 浏览器 + cookies → 打开搜索页 → page.on('response') 拦截 API
    Phase 3: page.evaluate() + fetch() 翻页（同微博模式，X-S 由浏览器自动附加）
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote as url_quote

from playwright.async_api import async_playwright

logger = logging.getLogger("rental.xhs")

COOKIE_FILE = Path(__file__).parent / "xhs_state.json"

ANTI_DETECT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
    window.chrome = { runtime: {} };
    // 掩盖 Playwright 特有的属性
    delete navigator.__proto__.webdriver;
    Object.defineProperty(navigator, 'permissions', {get: () => ({ query: async () => ({ state: 'granted' }) })});
"""

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 下沙租房关键词
XHS_KEYWORDS = [
    "下沙租房", "下沙转租", "下沙合租", "下沙个人转租",
    "金沙湖租房", "下沙房东直租", "下沙大学城租房",
    "下沙单间", "下沙整租", "下沙短租",
    "杭州下沙租房", "下沙无中介",
]


def has_cookies() -> bool:
    """检查是否有已保存的 cookie 文件。"""
    return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 100


async def do_login():
    """Headful 模式：打开浏览器，等 XHS 弹出 QR 码，用户扫码后保存 cookie。

    XHS 在页面加载后自动通过 JS 调用 /api/sns/web/v1/login/qrcode/create，
    弹出登录框显示 QR 码。本函数拦截此 API 获取 QR URL，打印到终端供用户扫码。
    同时监听 /api/qrcode/userinfo 检查扫码状态。
    """
    print("\n>> Opening browser for Xiaohongshu QR code login...")
    print("   The QR code will appear on the page automatically.")
    print("   Scan it with your 小红书 App, then the browser will close.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DESKTOP_UA,
            locale="zh-CN",
        )
        await context.add_init_script(ANTI_DETECT_SCRIPT)
        page = await context.new_page()

        # 拦截 QR 码创建 API
        qr_url: str | None = None
        login_done = asyncio.Event()

        async def on_response(response):
            nonlocal qr_url
            url = response.url
            if "login/qrcode/create" in url and response.status == 200:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    qr_url = data.get("data", {}).get("url", "")
                    if qr_url:
                        print(f"   QR Code URL: {qr_url}")
                        print(f"   (Open this URL in a browser to see the QR code)\n")
                except Exception:
                    pass
            elif "qrcode/userinfo" in url and response.status == 200:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    code_status = data.get("data", {}).get("codeStatus", -1)
                    if code_status == 1:  # 已确认
                        print("   ✅ QR code confirmed!")
                    elif code_status == 2:  # 已扫码
                        print("   📱 QR code scanned, waiting for confirmation...")
                    elif code_status == 3:  # 登录成功
                        print("   🎉 Login successful!")
                        login_done.set()
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto("https://www.xiaohongshu.com", timeout=30000, wait_until="domcontentloaded")
        except Exception:
            pass

        # 等待扫描完成（最长 120 秒）
        print("   Waiting for QR code scan (max 120s)...\n")
        try:
            await asyncio.wait_for(login_done.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            print("   ⚠️  Timeout waiting for login. Saving whatever cookies we have...")

        page.remove_listener("response", on_response)

        # 保存 cookies
        cookies = await context.cookies()
        state = {"cookies": cookies, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        COOKIE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print(f"\n[OK] 小红书 cookie 已保存 ({len(cookies)} 个)")

        await browser.close()


async def search_xhs(
    keyword: str,
    limit: int = 30,
    max_pages: int = 5,
) -> list[dict]:
    """搜索小红书笔记。

    Args:
        keyword: 搜索关键词
        limit: 最大结果数
        max_pages: 最大翻页数

    Returns:
        [{"url", "source": "小红书", "content", "publish_time", "poster_id", "images"}, ...]
    """
    collected: list[dict] = []
    seen_ids: set[str] = set()

    if not has_cookies():
        logger.warning("小红书 cookie 未就绪，请先运行: python -m src.crawler.xhs_crawler --login")
        return collected

    state = json.loads(COOKIE_FILE.read_text())
    cookies = state.get("cookies", [])
    logger.info(f"XHS: loaded {len(cookies)} cookies, search: '{keyword}'")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DESKTOP_UA,
            locale="zh-CN",
        )
        await context.add_cookies(cookies)
        await context.add_init_script(ANTI_DETECT_SCRIPT)
        page = await context.new_page()

        # ——— 策略 A: 拦截搜索 API 响应 ———
        api_responses: list[dict] = []

        async def on_response(response):
            url = response.url
            # 拦截搜索笔记 API
            if "search/notes" in url and response.status == 200:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if data.get("success") or data.get("code") == 0:
                        api_responses.append(data)
                except Exception:
                    pass

        page.on("response", on_response)

        # 打开搜索页 → 页面会自动调用搜索 API
        search_url = (
            f"https://www.xiaohongshu.com/search_result"
            f"?keyword={url_quote(keyword)}"
            f"&source=web_search_result_notes"
        )
        try:
            await page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"XHS page load timeout: {e}")

        # 等待页面 JS 发起搜索 API 请求
        await page.wait_for_timeout(8000)

        # ——— 通过 page.evaluate + fetch 翻页 ———
        for pn in range(2, max_pages + 1):
            try:
                json_text = await page.evaluate("""
                    async (args) => {
                        try {
                            const resp = await fetch('/api/sns/web/v1/search/notes', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                },
                                body: JSON.stringify({
                                    keyword: args.keyword,
                                    page: args.page,
                                    page_size: 20,
                                    sort: 'general',
                                    source: 'web_search_result_notes'
                                }),
                                credentials: 'include'
                            });
                            if (!resp.ok) return null;
                            return await resp.text();
                        } catch(e) {
                            return null;
                        }
                    }
                """, {"keyword": keyword, "page": pn})
                if json_text:
                    data = json.loads(json_text)
                    if data.get("success") or data.get("code") == 0:
                        api_responses.append(data)
                    else:
                        break
                else:
                    break
            except Exception as e:
                logger.debug(f"XHS page {pn} fetch failed: {e}")
                break

        page.remove_listener("response", on_response)

        # ——— 解析搜索结果 ———
        for resp_data in api_responses:
            data_block = resp_data.get("data", {})
            items = data_block.get("items", []) or data_block.get("notes", [])

            for item in items:
                # 笔记卡片结构可能直接是 note 对象，也可能嵌套在 note_card 中
                note = item.get("note_card") or item.get("note") or item

                note_id = str(note.get("note_id", note.get("id", "")))
                if not note_id or note_id in seen_ids:
                    continue
                seen_ids.add(note_id)

                # 标题
                title = note.get("title", "") or note.get("display_title", "")

                # 正文
                desc = note.get("desc", "")
                # 组合标题和正文
                parts = [title] if title else []
                if desc:
                    parts.append(desc)
                content = "\n".join(parts)

                # 过滤过短/非租房内容
                if len(content) < 15:
                    continue

                # 用户信息
                user = note.get("user", {}) or note.get("author", {})
                poster = user.get("nickname", "") or user.get("name", "")

                # 图片
                images = []
                img_list = note.get("image_list", []) or note.get("images", [])
                if isinstance(img_list, list):
                    for img in img_list:
                        if isinstance(img, dict):
                            img_url = (
                                img.get("url_default", "")
                                or img.get("url", "")
                                or img.get("info_list", [{}])[0].get("url", "")
                            )
                            if img_url:
                                images.append(img_url)

                # 时间和互动
                publish_time = note.get("time", "") or note.get("create_time", "")
                if isinstance(publish_time, (int, float)):
                    from datetime import datetime, timezone
                    publish_time = datetime.fromtimestamp(
                        publish_time / 1000 if publish_time > 1e12 else publish_time,
                        tz=timezone.utc,
                    ).isoformat()

                # 价格尝试提取
                price_match = re.search(r"(\d{3,5})\s*(?:元|/月|每月|一个月)", content)
                price_hint = ""
                if price_match:
                    price_hint = f"价格: {price_match.group(1)}元/月\n"

                # 标签
                tags = note.get("tag_list", []) or note.get("tags", [])
                if isinstance(tags, list) and tags:
                    tag_names = [
                        t.get("name", "") if isinstance(t, dict) else str(t)
                        for t in tags[:5]
                    ]
                    content += "\n标签: " + ", ".join(tag_names)

                if poster:
                    content += f"\n发布者: {poster}"

                detail_url = f"https://www.xiaohongshu.com/explore/{note_id}"

                collected.append({
                    "url": detail_url,
                    "source": "小红书",
                    "content": (price_hint + content)[:4000],
                    "publish_time": publish_time,
                    "poster_id": poster,
                    "images": images,
                })

                if len(collected) >= limit:
                    break

            if len(collected) >= limit:
                break

        # ——— 策略 B 降级: 如果 API 无数据，从 DOM 提取 ———
        if not collected:
            logger.info("XHS: API interception returned no data, falling back to DOM extraction")
            collected = await _extract_from_dom(page, keyword, limit)
            # DOM 提取的数据打上来源标记和 URL
            for item in collected:
                item["source"] = "小红书"  # override

        await browser.close()

    logger.info(f"XHS search '{keyword}': {len(collected)} items")
    return collected


async def _extract_from_dom(page, keyword: str, limit: int) -> list[dict]:
    """降级方案：从已渲染的 DOM 中提取笔记卡片。

    当 API 拦截无数据时使用（例如未登录状态下搜索结果嵌入在 SSR HTML 中）。
    """
    items = []

    # 滚动页面触发懒加载
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(1500)

    # 提取笔记卡片
    cards = await page.evaluate("""() => {
        const results = [];
        // XHS 笔记卡片选择器（常见 class 名）
        const selectors = [
            'section.note-item', '.note-item', '[class*="noteItem"]',
            'section[class*="note"]', 'div[class*="feeds"] section',
            'a[href*="/explore/"]'
        ];

        for (const sel of selectors) {
            const cards = document.querySelectorAll(sel);
            if (cards.length > 2) {
                cards.forEach(c => {
                    const link = c.querySelector('a[href*="/explore/"]') || (c.tagName === 'A' ? c : null);
                    const href = link ? link.getAttribute('href') : '';
                    const text = (c.textContent || '').trim().substring(0, 300);

                    // 提取图片
                    const imgs = c.querySelectorAll('img');
                    const imgSrcs = [];
                    imgs.forEach(img => {
                        const src = img.src || img.getAttribute('data-src') || '';
                        if (src && !src.includes('avatar') && !src.includes('icon')) {
                            imgSrcs.push(src);
                        }
                    });

                    if (text.length > 20 && href) {
                        results.push({
                            url: href.startsWith('http') ? href : 'https://www.xiaohongshu.com' + href,
                            content: text,
                            images: imgSrcs.slice(0, 9),
                            poster_id: '',
                            publish_time: '',
                        });
                    }
                });
                break;  // 找到了就停
            }
        }
        return results.slice(0, 30);
    }""")

    for card in cards:
        if len(card.get("content", "")) < 15:
            continue
        items.append({
            "url": card["url"],
            "source": "小红书",
            "content": card["content"][:4000],
            "publish_time": card.get("publish_time", ""),
            "poster_id": card.get("poster_id", ""),
            "images": card.get("images", []),
        })
        if len(items) >= limit:
            break

    logger.info(f"XHS DOM extraction: {len(items)} cards from page")
    return items


async def search_multi_keywords(
    keywords: list[str] | None = None,
    limit_per_kw: int = 20,
) -> list[dict]:
    """多关键词批量搜索小红书。

    Args:
        keywords: 关键词列表，默认使用 XHS_KEYWORDS
        limit_per_kw: 每关键词最大结果数
    """
    if keywords is None:
        keywords = XHS_KEYWORDS

    all_items: list[dict] = []
    seen_urls: set[str] = set()

    for kw in keywords:
        try:
            items = await search_xhs(kw, limit=limit_per_kw)
            for item in items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_items.append(item)
            # 限流保护
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"XHS keyword '{kw}' failed: {e}")
            continue

    logger.info(f"XHS: {len(keywords)} keywords → {len(all_items)} unique items")
    return all_items


# ——— CLI ———
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="小红书爬虫")
    parser.add_argument("--login", action="store_true", help="扫码登录并保存 cookie")
    parser.add_argument("--search", type=str, help="搜索关键词")
    parser.add_argument("--limit", type=int, default=20, help="最大结果数")
    parser.add_argument("--all", action="store_true", help="全量关键词搜索")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.login:
        asyncio.run(do_login())
    elif args.search:
        async def _run():
            items = await search_xhs(args.search, limit=args.limit)
            print(f"\n{'='*60}")
            print(f"Total: {len(items)} items")
            for i, item in enumerate(items[:5]):
                text = item["content"][:120].replace("\n", " ")
                print(f"\n[{i}] {text}")
                print(f"    {item['url']}")
                print(f"    poster: {item.get('poster_id', '')}")
                print(f"    images: {len(item.get('images', []))}")
            if not items:
                print("⚠️  No results. Try logging in first: python -m src.crawler.xhs_crawler --login")
                print("   (Search results may require a logged-in session.)")
        asyncio.run(_run())
    elif args.all:
        async def _run():
            items = await search_multi_keywords()
            print(f"\nTotal: {len(items)} items from all keywords")
        asyncio.run(_run())
    else:
        parser.print_help()
