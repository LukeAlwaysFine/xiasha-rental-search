"""豆瓣 HTTP 爬虫 — 用 httpx + BeautifulSoup 替代 Firecrawl。

豆瓣小组是 SSR（服务端渲染），无需 JS 执行。直接 HTTP GET 即可获取完整 HTML。
比 Firecrawl 更快、更稳定、且零费用。

搜索: https://www.douban.com/group/search?cat=1013&q=下沙租房
详情: https://www.douban.com/group/topic/xxxxx/

用法:
    # 搜索帖子
    topics = await search_group("下沙租房", limit=30)

    # 抓取详情
    content = await fetch_topic_detail("https://www.douban.com/group/topic/123456/")
"""

import logging
import re
import time
import asyncio
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger("rental.douban_http")

DOUBAN_BASE = "https://www.douban.com"
SEARCH_URL = f"{DOUBAN_BASE}/group/search"

# 杭州租房相关小组 ID（用于拼接 group 限定搜索或抓取讨论列表）
DOUBAN_GROUP_IDS = [
    "hzhouses",   # 杭州租房
    "hzrent",     # 杭州租房小组
    "526987",     # 杭州租房-整租
    "586947",     # 杭州转租
]

# 模拟正常浏览器
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 注意：不含 "br"（Brotli），httpx 默认不支持 Brotli 解压
    "Accept-Encoding": "gzip, deflate",
}

# 请求间隔（秒），避免被限流
_REQUEST_DELAY = 2.0
_rate_lock = asyncio.Lock()
_last_request_time: float = 0.0
# 403 退避计数器
_consecutive_403: int = 0
_MAX_CONSECUTIVE_403 = 3


async def _rate_limit():
    """两次请求之间至少间隔 _REQUEST_DELAY 秒（asyncio 安全）。"""
    global _last_request_time
    async with _rate_lock:
        now = time.time()
        wait = _last_request_time + _REQUEST_DELAY - now
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = time.time()


async def _get(url: str, client: httpx.AsyncClient | None = None) -> str | None:
    """HTTP GET 并返回 HTML 文本。"""
    global _consecutive_403
    await _rate_limit()

    # 连续 403 退避
    if _consecutive_403 >= _MAX_CONSECUTIVE_403:
        logger.warning(f"Douban: {_consecutive_403} consecutive 403s, pausing 30s...")
        await asyncio.sleep(30)
        _consecutive_403 = 0

    async def _do_get(c: httpx.AsyncClient) -> str | None:
        global _consecutive_403
        try:
            resp = await c.get(url, headers=HEADERS, follow_redirects=True, timeout=httpx.Timeout(20.0))
            if resp.status_code == 404:
                logger.debug(f"Douban 404: {url[:100]}")
                _consecutive_403 = 0  # M21: 非403响应时也重置计数器
                return None
            if resp.status_code == 403:
                _consecutive_403 += 1
                logger.warning(f"Douban 403 (blocked, #{_consecutive_403}): {url[:100]}")
                return None
            # 非 403 响应（200/500/503 等）→ 重置 403 计数器
            _consecutive_403 = 0
            if resp.status_code != 200:
                logger.debug(f"Douban HTTP {resp.status_code}: {url[:100]}")
                return None
            return resp.text
        except httpx.RequestError as e:
            logger.warning(f"Douban request error: {e}")
            _consecutive_403 = 0  # M21: 网络错误也重置计数器
            return None

    if client:
        return await _do_get(client)
    else:
        async with httpx.AsyncClient() as c:
            return await _do_get(c)


async def search_group(
    keyword: str,
    limit: int = 30,
    sort: str = "new",
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """搜索豆瓣小组帖子。

    Args:
        keyword: 搜索关键词
        limit: 最大结果数
        sort: "new"（最新）或 "relevance"（相关度）
        client: 复用的 httpx.AsyncClient（可选）

    Returns:
        [{"url": str, "title": str, "source": "豆瓣", "group": str, "publish_time": str}, ...]
    """
    params = {
        "cat": "1013",     # 小组分类
        "q": keyword,
        "sort": sort,
    }
    url = f"{SEARCH_URL}?{'&'.join(f'{k}={quote(v)}' for k, v in params.items())}"
    html = await _get(url, client)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    topics = []

    # 豆瓣搜索结果: <table class="olt"> → <tr class="pl">
    #   td.td-subject a → 标题+链接
    #   td.td-time → 发布时间
    #   td:last-child a → 小组名
    for row in soup.select("tr.pl"):
        # 标题和链接
        title_el = row.select_one("td.td-subject a")
        if not title_el:
            continue

        href = title_el.get("href", "")
        title = title_el.get_text(strip=True) or title_el.get("title", "")
        if not href or not title:
            continue

        # 只看话题详情页
        if "/group/topic/" not in href:
            continue

        full_url = urljoin(DOUBAN_BASE, href)

        # 发布时间
        time_el = row.select_one("td.td-time")
        pub_time = ""
        if time_el:
            pub_time = time_el.get("title", "") or time_el.get_text(strip=True)

        # 小组名（最后一个 td 中的 a）
        tds = row.select("td")
        group_name = ""
        if len(tds) >= 4:
            group_el = tds[-1].select_one("a")
            group_name = group_el.get_text(strip=True) if group_el else ""

        topics.append({
            "url": full_url,
            "title": title[:200],
            "source": "豆瓣",
            "group": group_name,
            "publish_time": pub_time,
        })

        if len(topics) >= limit:
            break

    logger.info(f"douban_http: '{keyword}' → {len(topics)} topics")
    return topics


async def fetch_topic_detail(
    url: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """抓取单个豆瓣话题详情页的完整内容。

    返回话题正文（纯文本 + markdown 风格的格式），用于 LLM 提取。
    """
    html = await _get(url, client)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 话题标题
    title_el = soup.select_one("h1, .article h1, .topic-title")
    title = title_el.get_text(strip=True) if title_el else ""

    # 话题正文
    content_el = soup.select_one(
        "#link-report, .topic-content, .topic-richtext, .rich-content, article"
    )
    if not content_el:
        # Fallback: 找最大的文本块
        content_el = soup.select_one(".article, #content, .topic-doc")
        if not content_el:
            logger.debug(f"douban_http: no content found for {url[:80]}")
            return title if title else None

    # 提取文本（去重：只取直接子元素的文本，避免嵌套 div 重复）
    text = content_el.get_text("\n", strip=True)
    if not text:
        return title if title else None

    # 简单的行去重（保留顺序，删除连续重复行）
    lines = text.split("\n")
    deduped: list[str] = []
    seen_lines: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and stripped not in seen_lines:
            deduped.append(line)
            seen_lines.add(stripped)

    # 组合标题和正文
    parts = [title] if title else []
    parts.extend(deduped)
    content = "\n".join(parts)

    if len(content) < 10:
        if "登录" in content or "注册" in content:
            logger.debug(f"douban_http: login wall for {url[:80]}")
        return None

    return content[:4000]


async def search_multi_keywords(
    keywords: list[str],
    limit_per_kw: int = 20,
) -> list[dict]:
    """多关键词并行搜索豆瓣。

    与闲鱼的关键词矩阵策略相同 — 用更多关键词换取更全面的覆盖。
    """
    all_topics: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient() as client:
        for kw in keywords:
            try:
                topics = await search_group(kw, limit=limit_per_kw, client=client)
                for t in topics:
                    if t["url"] not in seen_urls:
                        seen_urls.add(t["url"])
                        all_topics.append(t)
            except Exception as e:
                logger.warning(f"douban_http: keyword '{kw}' failed: {e}")
                continue

    logger.info(f"douban_http: {len(keywords)} keywords → {len(all_topics)} unique topics")
    return all_topics


# ——— CLI 测试 ———
if __name__ == "__main__":
    import asyncio

    async def _test():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        print("=== 搜索测试 ===")
        topics = await search_group("下沙租房", limit=5)
        for t in topics:
            print(f"  [{t['group']}] {t['title'][:80]}")
            print(f"    {t['url']}")
            print(f"    {t['publish_time']}")

        if topics:
            print("\n=== 详情抓取测试 ===")
            detail = await fetch_topic_detail(topics[0]["url"])
            if detail:
                print(f"  长度: {len(detail)} 字符")
                print(f"  前 500: {detail[:500]}")
            else:
                print("  抓取失败（可能需要登录）")

    asyncio.run(_test())
