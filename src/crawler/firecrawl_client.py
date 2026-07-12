"""Firecrawl 集成 — 抓取豆瓣和闲鱼的租房信息。

支持两种模式:
- 云 API (默认): https://api.firecrawl.dev, 免费 1000 credits/月
  申请地址: https://www.firecrawl.dev/pricing
  需配置 FIRECRAWL_API_KEY
- 自部署: docker run -p 3002:3002 firecrawl/firecrawl
  设置 FIRECRAWL_URL=http://localhost:3002，不需要 API key

文档: https://docs.firecrawl.dev
"""

import logging
import os
import httpx
from src.http_client import get_http_client

logger = logging.getLogger("rental.firecrawl")

FIRECRAWL_URL = os.getenv("FIRECRAWL_URL", "https://api.firecrawl.dev")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")

# 杭州豆瓣租房小组
DOUBAN_GROUPS = [
    "https://www.douban.com/group/hzhouses/discussion",     # 杭州租房
    "https://www.douban.com/group/hzrent/discussion",       # 杭州租房小组
    "https://www.douban.com/group/526987/discussion",       # 杭州租房-整租
    "https://www.douban.com/group/586947/discussion",       # 杭州转租
]

# 闲鱼搜索 (杭州租房)
XIANYU_SEARCHES = [
    "https://www.goofish.com/search?q=杭州租房",
    "https://www.goofish.com/search?q=杭州转租",
]

# Wellcee 杭州 (国际化租房平台，SPA 需 JS 渲染)
WELLCEE_URLS = [
    "https://www.wellcee.com/rent/hangzhou",
]


async def scrape_url(url: str, wait_for: int = 0) -> dict | None:
    """抓取单个 URL，返回 markdown 内容和元数据。

    wait_for: JS 渲染等待毫秒数。豆瓣等 SSR 页面用 0，闲鱼等 SPA 用 3000+。
    """
    headers = {"Content-Type": "application/json"}
    if FIRECRAWL_API_KEY:
        headers["Authorization"] = f"Bearer {FIRECRAWL_API_KEY}"

    payload: dict = {"url": url, "formats": ["markdown", "links"]}
    if wait_for > 0:
        payload["waitFor"] = wait_for  # Firecrawl 顶层参数，非 scrapeOptions

    client = get_http_client()
    try:
        resp = await client.post(
            f"{FIRECRAWL_URL}/v1/scrape",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning(
                f"Firecrawl HTTP {resp.status_code} for {url[:100]} "
                f"(key={'configured' if FIRECRAWL_API_KEY else 'MISSING'})"
            )
            return None
        data = resp.json()
        if not data.get("success"):
            logger.warning(
                f"Firecrawl success=false for {url[:100]}: "
                f"{data.get('error', 'no error message')}"
            )
            return None
        return data.get("data", {})
    except httpx.RequestError as e:
        logger.warning(f"Firecrawl request failed for {url[:100]}: {e}")
        return None


async def crawl_douban_group(group_url: str, limit: int = 20) -> list[dict]:
    """抓取豆瓣小组讨论列表。豆瓣是 SSR，无需 JS 渲染。"""
    result = await scrape_url(group_url)  # wait_for=0，豆瓣是服务器渲染
    if not result:
        return []

    links = result.get("links", [])
    markdown = result.get("markdown", "")

    # 从 links 中筛选话题链接: /group/topic/xxxxx/
    topics = []
    seen = set()
    for url in links:
        if "/group/topic/" in url and url not in seen:
            seen.add(url)
            topics.append({"url": url, "source": "豆瓣", "group_url": group_url})
            if len(topics) >= limit:
                break

    return topics


async def crawl_douban_by_keyword(
    group_url: str, keyword: str, limit: int = 30
) -> list[dict]:
    """抓取豆瓣小组搜索结果 — 按关键词过滤话题。

    豆瓣小组支持 URL 参数搜索:
        https://www.douban.com/group/hzhouses/discussion?q=下沙

    与 crawl_douban_group 不同，本函数构造搜索 URL 后抓取，
    只返回标题/内容含关键词的话题。

    Args:
        group_url: 豆瓣小组讨论页 URL
        keyword: 搜索关键词
        limit: 最大话题数

    Returns:
        [{"url": str, "source": "豆瓣", "group_url": str}, ...]
    """
    # 构造搜索 URL: 在 discussion 后面加 ?q=keyword
    search_url = f"{group_url.rstrip('/')}?q={keyword}"
    result = await scrape_url(search_url, wait_for=0)
    if not result:
        return []

    links = result.get("links", [])
    topics = []
    seen = set()
    for url in links:
        if "/group/topic/" in url and url not in seen:
            seen.add(url)
            topics.append({"url": url, "source": "豆瓣", "group_url": group_url})
            if len(topics) >= limit:
                break

    logger.info(f"douban_by_keyword: '{keyword}' → {len(topics)} topics from {group_url}")
    return topics


async def crawl_xianyu_search(search_url: str, limit: int = 20) -> list[dict]:
    """抓取闲鱼搜索页，返回商品链接列表。"""
    result = await scrape_url(search_url, wait_for=5000)  # 闲鱼是 SPA，需 JS 渲染
    if not result:
        return []

    links = result.get("links", [])
    items = []
    seen = set()
    for url in links:
        if "/item/" in url and url not in seen:
            seen.add(url)
            items.append({"url": url, "source": "闲鱼", "search_url": search_url})
            if len(items) >= limit:
                break

    return items


async def crawl_wellcee(limit: int = 20) -> list[dict]:
    """抓取 Wellcee 杭州租房列表。SPA 页面，依赖 Firecrawl JS 渲染。"""
    items = []
    for url in WELLCEE_URLS:
        result = await scrape_url(url, wait_for=5000)  # Wellcee 是 SPA
        if not result:
            continue
        links = result.get("links", [])
        seen = set()
        for link in links:
            if "/rent/" in link and "/hangzhou/" in link and link not in seen:
                seen.add(link)
                items.append({"url": link, "source": "Wellcee"})
                if len(items) >= limit:
                    break
        if items:
            break
    return items


async def scrape_topic_detail(url: str) -> str | None:
    """抓取单个帖子/商品详情页，返回 markdown 内容。"""
    result = await scrape_url(url)
    if not result:
        return None
    return result.get("markdown", "")
