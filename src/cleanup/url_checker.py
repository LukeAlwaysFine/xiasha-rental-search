"""URL 存活检测 + 过期房源硬删除。

对 DB 中的房源逐条检测 source_url 是否仍有效：
- 闲鱼 (goofish.com): 检查页面是否含"宝贝不存在""已下架"等关键词
- 豆瓣 (douban.com): 检查 HTTP 状态码

用法:
    python -m src.cleanup.url_checker              # 全量检测
    python -m src.cleanup.url_checker --dry-run    # 仅报告，不删除
    python -m src.cleanup.url_checker --batch 50   # 每批 50 条
"""

import asyncio
import logging
import os
import sys
import time

import httpx

logger = logging.getLogger("rental.url_checker")

# 闲鱼已下架/不存在关键词
_XIANYU_GONE_KEYWORDS = [
    "宝贝不存在", "已售出", "已下架", "找不到", "页面不存在",
    "你访问的页面不存在", "sorry, the page you visited does not exist",
    "内容已被删除", "该内容已被发布者删除",
]

# 风控关键词（遇到则跳过）
_XIANYU_BLOCK_KEYWORDS = [
    "请登录", "验证码", "滑块验证", "人机验证", "访问频繁",
    "请稍后再试", "系统繁忙",
]

# 并发控制
_MAX_CONCURRENT = 10
_BATCH_DELAY = 2.0  # 批次间延迟（秒）


async def check_xianyu_alive(client: httpx.AsyncClient, url: str) -> bool | None:
    """检测闲鱼商品是否仍存活。

    Returns:
        True: 存活
        False: 已下架/删除
        None: 无法判断（被风控/网络错误），跳过本次
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        resp = await client.get(url, headers=headers, timeout=15.0)
    except httpx.TimeoutException:
        logger.debug(f"timeout: {url}")
        return None
    except httpx.RequestError as e:
        logger.debug(f"request error: {url} - {e}")
        return None

    # 404/410 → 已删除
    if resp.status_code in (404, 410):
        logger.info(f"GONE (HTTP {resp.status_code}): {url}")
        return False

    # 非 200 → 不确定
    if resp.status_code != 200:
        logger.debug(f"non-200 ({resp.status_code}): {url}")
        return None

    text = resp.text[:5000]

    # 风控关键词 → 跳过
    for kw in _XIANYU_BLOCK_KEYWORDS:
        if kw in text:
            logger.debug(f"blocked (found '{kw}'): {url}")
            return None

    # 已下架关键词 → 删除
    for kw in _XIANYU_GONE_KEYWORDS:
        if kw in text:
            logger.info(f"GONE (found '{kw}'): {url}")
            return False

    return True


async def check_douban_alive(client: httpx.AsyncClient, url: str) -> bool | None:
    """检测豆瓣话题是否仍存活。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        resp = await client.get(url, headers=headers, timeout=15.0)
    except (httpx.TimeoutException, httpx.RequestError):
        return None

    if resp.status_code in (404, 410):
        logger.info(f"GONE (HTTP {resp.status_code}): {url}")
        return False
    return True if resp.status_code == 200 else None


async def cleanup_expired_listings(
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict:
    """批量检测并删除过期房源。

    Args:
        batch_size: 每批检测数量
        dry_run: True 时仅报告不删除

    Returns:
        {"total": int, "alive": int, "deleted": int, "skipped": int, "errors": int}
    """
    from src.db.schema import get_conn

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, source_url, source_platform, title, publish_time, fetched_at "
        "FROM listings ORDER BY fetched_at ASC"
    ).fetchall()
    conn.close()

    stats = {"total": len(rows), "alive": 0, "deleted": 0, "skipped": 0, "errors": 0}

    if not rows:
        logger.info("cleanup: no listings to check")
        return stats

    logger.info(f"cleanup: checking {len(rows)} listings (batch_size={batch_size})...")

    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start:batch_start + batch_size]
            batch_delete: list[int] = []

            async def _check_one(row) -> tuple[int, bool | None]:
                async with sem:
                    url = row["source_url"]
                    platform = row["source_platform"]
                    if platform == "闲鱼":
                        result = await check_xianyu_alive(client, url)
                    elif platform == "豆瓣":
                        result = await check_douban_alive(client, url)
                    else:
                        # 未知平台，默认跳过
                        return row["id"], None
                    return row["id"], result

            results = await asyncio.gather(*[_check_one(r) for r in batch])

            for listing_id, result in results:
                if result is True:
                    stats["alive"] += 1
                elif result is False:
                    stats["deleted"] += 1
                    batch_delete.append(listing_id)
                else:
                    stats["skipped"] += 1

            # 批次内删除（使用独立的批次列表，修复只删最后 N 个的 bug）
            if batch_delete and not dry_run:
                conn = get_conn()
                try:
                    for lid in batch_delete:
                        conn.execute("DELETE FROM listings WHERE id = ?", (lid,))
                    conn.commit()
                except Exception as e:
                    stats["errors"] += 1
                    logger.error(f"batch delete failed: {e}")
                finally:
                    conn.close()

            progress = min(batch_start + batch_size, len(rows))
            logger.info(
                f"cleanup progress: {progress}/{len(rows)} "
                f"(alive={stats['alive']}, deleted={stats['deleted']}, skipped={stats['skipped']})"
            )

            if batch_start + batch_size < len(rows):
                await asyncio.sleep(_BATCH_DELAY)

    if dry_run:
        logger.info(f"DRY RUN: would have deleted {stats['deleted']} listings")

    logger.info(
        f"cleanup DONE: {stats['total']} checked, {stats['deleted']} deleted, "
        f"{stats['alive']} alive, {stats['skipped']} skipped, {stats['errors']} errors"
    )
    return stats


# ——— CLI ———
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="URL 存活检测 + 过期房源清理")
    parser.add_argument("--dry-run", action="store_true", help="仅报告，不实际删除")
    parser.add_argument("--batch", type=int, default=50, help="每批检测数量 (默认 50)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _main():
        from dotenv import load_dotenv
        load_dotenv()
        return await cleanup_expired_listings(batch_size=args.batch, dry_run=args.dry_run)

    result = asyncio.run(_main())
    print()
    print(f"  检测: {result['total']} 条")
    print(f"  存活: {result['alive']} 条")
    print(f"  已删除: {result['deleted']} 条")
    print(f"  跳过: {result['skipped']} 条")
    if result['errors']:
        print(f"  错误: {result['errors']} 条")
