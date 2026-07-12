"""批量全量抓取 — 一次性建立数据基础，不依赖服务器生命周期。

运行方式:
    LD_LIBRARY_PATH=/tmp/chromium-libs/lib python scripts/bulk_scrape.py

预计: 22 关键词 × 60 详情页 × 3s ≈ 66 分钟，入库 200-500 条
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# 确保 Chromium 系统库可加载
os.environ["LD_LIBRARY_PATH"] = "/tmp/chromium-libs/lib:" + os.environ.get("LD_LIBRARY_PATH", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("bulk_scrape")


def _parse_llm_concurrency(default: int = 40) -> int:
    """安全解析 LLM_CONCURRENCY 环境变量，异常时回退默认值。"""
    try:
        return int(os.getenv("LLM_CONCURRENCY", str(default)))
    except (ValueError, TypeError):
        logger.warning(f"LLM_CONCURRENCY 环境变量无效，使用默认值 {default}")
        return default


from src.crawler.xianyu_async import crawl_xianyu_async
from src.extractor.llm_extract import extract_listing
from src.db.schema import get_conn, upsert_listing, count_listings
from src.detector.agent_detector import detect_with_llm, is_sublet_from_content
from src.geocode.amap import geocode
from src.pipeline import _is_valid_rental, process_listing_item

# 22 个关键词：区域 × 类型 交叉覆盖
BULK_KEYWORDS = [
    # 通用
    "杭州租房", "杭州转租", "杭州合租",
    # 下沙
    "杭州 下沙 租房", "杭州 下沙 转租", "杭州 下沙 整租", "下沙 出租",
    # 滨江
    "杭州 滨江 租房", "杭州 滨江 转租",
    # 未来科技城/余杭
    "杭州 未来科技城 租房", "杭州 余杭 租房",
    # 拱墅
    "杭州 拱墅 租房", "杭州 拱墅 转租",
    # 萧山
    "杭州 萧山 租房", "杭州 萧山 转租",
    # 西湖
    "杭州 西湖 租房", "杭州 西湖 转租",
    # 上城
    "杭州 上城 租房", "杭州 上城 转租",
    # 钱塘/下沙
    "杭州 钱塘 租房", "杭州 九堡 租房",
    # 其他热点
    "杭州 三墩 租房", "杭州 西溪 租房",
]


async def process_one(item: dict, sem: asyncio.Semaphore, conn) -> dict | None:
    """H2: 复用 process_listing_item 确保 LLM+regex 混合中介检测一致性。"""
    return await process_listing_item(item, conn, sem)


async def main():
    t0 = time.time()
    logger.info("=" * 60)
    logger.info("BULK SCRAPE START: 22 keywords x 60 items")
    logger.info("=" * 60)

    # 初始统计
    init_conn = get_conn()
    initial_total = count_listings(init_conn, {"keyword": ""})
    init_conn.close()
    logger.info(f"DB before: {initial_total} listings")

    # Phase 1: 闲鱼抓取
    logger.info(f"Scraping {len(BULK_KEYWORDS)} keywords...")
    scraped = await crawl_xianyu_async(
        keywords=BULK_KEYWORDS,
        limit_per_keyword=60,
        headless=True,
    )
    elapsed_scrape = time.time() - t0
    logger.info(f"Scrape done: {len(scraped)} items in {elapsed_scrape:.0f}s ({elapsed_scrape/60:.1f}min)")

    if not scraped:
        logger.error("No items scraped! Check cookies and network.")
        return

    # Phase 2: LLM 提取 (40 并发)
    conn = get_conn()
    try:
        sem = asyncio.Semaphore(_parse_llm_concurrency())
        tasks = [process_one(item, sem, conn) for item in scraped]
        results = await asyncio.gather(*tasks)

        # Phase 3: 入库
        imported = 0
        failed = 0
        for r in results:
            if r and upsert_listing(conn, r):
                imported += 1
            elif r is None:
                failed += 1

        conn.commit()
    finally:
        conn.close()

    # 最终统计
    final_conn = get_conn()
    final_total = count_listings(final_conn, {"keyword": ""})
    final_conn.close()

    elapsed_total = time.time() - t0
    logger.info("=" * 60)
    logger.info(f"BULK SCRAPE DONE: {imported} imported, {failed} rejected")
    logger.info(f"DB: {initial_total} → {final_total} (+{final_total - initial_total})")
    logger.info(f"Total time: {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
