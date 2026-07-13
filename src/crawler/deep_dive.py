"""Deep dive 编排器 — 穷举式搜集目标区域房源。

用法:
    python -m src.crawler.deep_dive --area 下沙
    python -m src.crawler.deep_dive --area 下沙 --no-douban  # 仅闲鱼

核心逻辑：
1. 生成区域专项关键词（area_keywords.build_area_keywords）
2. 闲鱼 Playwright 抓取（复用 crawl_xianyu_via_api）
3. 豆瓣定向抓取（通过 Firecrawl 搜索 URL）
4. LLM 提取 + geocode + 中介检测 + 入库（复用 pipeline 的 process_one 模式）
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("rental.deep_dive")


def _parse_llm_concurrency(default: int = 40) -> int:
    """安全解析 LLM_CONCURRENCY 环境变量，异常时回退默认值。"""
    try:
        return int(os.getenv("LLM_CONCURRENCY", str(default)))
    except (ValueError, TypeError):
        logger.warning(f"LLM_CONCURRENCY 环境变量无效，使用默认值 {default}")
        return default


from src.crawler.area_keywords import build_area_keywords, get_douban_queries, AREA_CONFIG
from src.crawler.xianyu_async import crawl_xianyu_via_api, has_valid_cookies
from src.db.schema import get_conn, upsert_listing
from src.pipeline import process_listing_item


async def deep_dive_area(
    area_name: str,
    sources: list[str] | None = None,
    headless: bool = True,
    limit_per_keyword: int = 200,
) -> dict:
    """深潜抓取目标区域所有房源。

    Args:
        area_name: 目标区域名（如 "下沙"），必须在 AREA_CONFIG 中配置
        sources: 数据源列表，默认 ["xianyu", "douban"]
        headless: Playwright 是否无头模式
        limit_per_keyword: 每关键词最大条目数

    Returns:
        {
            "area": str,
            "keywords": int,
            "xianyu_raw": int,
            "douban_raw": int,
            "extracted": int,      # LLM 成功提取数
            "rejected": int,       # _is_valid_rental 过滤数
            "new_listings": int,   # 实际入库数
            "errors": list[str],
            "duration_s": float,
        }
    """
    if sources is None:
        sources = ["xianyu", "douban"]

    if area_name not in AREA_CONFIG:
        return {"area": area_name, "error": f"未配置的区域: {area_name}，请在 area_keywords.py 中添加"}

    t0 = time.time()
    errors: list[str] = []
    stats = {
        "area": area_name,
        "keywords": 0,
        "xianyu_raw": 0,
        "douban_raw": 0,
        "extracted": 0,
        "rejected": 0,
        "new_listings": 0,
        "errors": errors,
        "duration_s": 0.0,
    }

    all_raw_items: list[dict] = []

    # ——— 闲鱼 ———
    if "xianyu" in sources:
        if not has_valid_cookies():
            err = "闲鱼 cookie 未就绪，请先运行: python -m src.crawler.xianyu_async --login"
            logger.error(err)
            errors.append(err)
        else:
            keywords = build_area_keywords(area_name)
            stats["keywords"] = len(keywords)
            logger.info(f"deep_dive [{area_name}]: 开始闲鱼抓取 ({len(keywords)} 关键词)...")

            try:
                xy_items = await crawl_xianyu_via_api(
                    keywords=keywords,
                    limit_per_keyword=limit_per_keyword,
                    max_pages=1,  # API 分页不可用，每关键词只拿第一页
                    headless=headless,
                )
                stats["xianyu_raw"] = len(xy_items)
                all_raw_items.extend(xy_items)
                logger.info(f"deep_dive [{area_name}]: 闲鱼抓取完成 ({len(xy_items)} raw items)")
            except Exception as e:
                err = f"闲鱼抓取失败: {e}"
                logger.error(err, exc_info=True)
                errors.append(err)

    # ——— 豆瓣 ———
    if "douban" in sources:
        logger.info(f"deep_dive [{area_name}]: 开始豆瓣定向抓取...")
        try:
            db_items = await _deep_dive_douban(area_name)
            stats["douban_raw"] = len(db_items)
            all_raw_items.extend(db_items)
            logger.info(f"deep_dive [{area_name}]: 豆瓣抓取完成 ({len(db_items)} raw items)")
        except Exception as e:
            err = f"豆瓣抓取失败: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)

    if not all_raw_items:
        stats["duration_s"] = round(time.time() - t0, 1)
        logger.warning(f"deep_dive [{area_name}]: 无任何 raw items，终止")
        return stats

    # ——— LLM 提取管线 ———
    logger.info(f"deep_dive [{area_name}]: 开始 LLM 提取 ({len(all_raw_items)} items)...")
    conn = get_conn()
    try:
        sem = asyncio.Semaphore(_parse_llm_concurrency())

        results = await asyncio.gather(
            *[process_listing_item(item, conn, sem) for item in all_raw_items],
            return_exceptions=True,
        )

        extracted_count = 0
        rejected_count = 0
        new_count = 0
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"deep_dive process_one failed: {r}")
                rejected_count += 1
                continue
            if r is None:
                rejected_count += 1
            elif upsert_listing(conn, r):
                new_count += 1
                extracted_count += 1
            else:
                extracted_count += 1  # LLM 提取成功但 upsert 去重

        conn.commit()

    finally:
        conn.close()  # H1: 确保异常时也释放连接

    stats["extracted"] = extracted_count
    stats["rejected"] = rejected_count
    stats["new_listings"] = new_count
    stats["duration_s"] = round(time.time() - t0, 1)

    logger.info(
        f"deep_dive [{area_name}] DONE: "
        f"{new_count} new listings, {extracted_count} extracted, "
        f"{rejected_count} rejected, {stats['keywords']} keywords, "
        f"{stats['duration_s']}s"
    )
    return stats


async def _deep_dive_douban(area_name: str) -> list[dict]:
    """豆瓣定向深潜：使用 douban_http（httpx + BS4）直接抓取。

    H11-H12: 替换废弃的 firecrawl_client，与 pipeline.py 统一使用 douban_http。
    1. 对区域关键词调用 douban_http.search_group 搜索话题
    2. 逐个调用 douban_http.fetch_topic_detail 抓取详情
    3. 返回 {"url", "source": "豆瓣", "content"} 列表
    """
    from src.crawler.douban_http import search_group, fetch_topic_detail

    # 生成豆瓣搜索关键词（复用 area_keywords 的豆瓣查询配置）
    douban_queries = get_douban_queries(area_name)
    if not douban_queries:
        # fallback: 用基本关键词
        douban_queries = [f"{area_name}租房", f"{area_name}转租", f"{area_name}合租"]

    all_topics: list[dict] = []
    seen_urls: set[str] = set()

    # Phase 1: 多关键词搜索 → 收集话题链接
    for query in douban_queries[:3]:  # 最多3个关键词避免过载
        try:
            topics = await search_group(query, limit=30)
            for t in topics:
                if t["url"] not in seen_urls:
                    seen_urls.add(t["url"])
                    all_topics.append(t)
        except Exception as e:
            logger.warning(f"deep_dive douban search failed [{query}]: {e}")
            continue

    logger.info(f"deep_dive [{area_name}]: 豆瓣搜索收集到 {len(all_topics)} 个话题链接")

    # Phase 2: 并行抓取话题详情
    _douban_sem = asyncio.Semaphore(3)  # 豆瓣限流严格，降低并发

    async def _fetch_detail(topic: dict) -> dict | None:
        async with _douban_sem:
            for attempt in range(2):
                try:
                    detail = await fetch_topic_detail(topic["url"])
                    if detail:
                        return {
                            "url": topic["url"],
                            "source": "豆瓣",
                            "content": detail,
                            "publish_time": topic.get("publish_time", ""),
                        }
                    if attempt == 0:
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.warning(f"deep_dive douban detail fetch failed: {e}")
                    if attempt < 1:
                        await asyncio.sleep(1.5)
            return None

    items = await asyncio.gather(*[_fetch_detail(t) for t in all_topics])
    valid = [i for i in items if i is not None]
    return valid


# ——— CLI ———
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Deep Dive — 穷举式搜集目标区域房源"
    )
    parser.add_argument("--area", type=str, required=True, help="目标区域（如 下沙）")
    parser.add_argument("--no-douban", action="store_true", help="跳过豆瓣抓取")
    parser.add_argument("--no-headless", action="store_true", help="可见浏览器模式（调试用）")
    parser.add_argument("--limit", type=int, default=200, help="每关键词最大条目数")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sources = ["xianyu"]
    if not args.no_douban:
        sources.append("douban")

    async def _main():
        result = await deep_dive_area(
            area_name=args.area,
            sources=sources,
            headless=not args.no_headless,
            limit_per_keyword=args.limit,
        )
        print()
        print("=" * 60)
        print(f"  Deep Dive [{result['area']}] 完成")
        print(f"  关键词数:    {result.get('keywords', 0)}")
        print(f"  闲鱼 raw:    {result.get('xianyu_raw', 0)}")
        print(f"  豆瓣 raw:    {result.get('douban_raw', 0)}")
        print(f"  LLM 提取成功: {result.get('extracted', 0)}")
        print(f"  过滤/失败:    {result.get('rejected', 0)}")
        print(f"  新增入库:     {result.get('new_listings', 0)}")
        print(f"  耗时:         {result.get('duration_s', 0)}s")
        if result.get("errors"):
            print(f"  错误:         {len(result['errors'])} 条")
            for e in result["errors"]:
                print(f"    - {e}")
        print("=" * 60)

    asyncio.run(_main())
