#!/usr/bin/env python3
"""Deep Dive CLI — 穷举式搜集目标区域租房信息。

用法:
    python scripts/deep_dive.py --area 下沙
    python scripts/deep_dive.py --area 下沙 --no-douban          # 仅闲鱼
    python scripts/deep_dive.py --area 下沙 --no-headless        # 可见浏览器（调试）
    python scripts/deep_dive.py --area 下沙 --limit 100          # 每关键词 100 条上限

添加新区域:
    编辑 src/crawler/area_keywords.py，在 AREA_CONFIG 中新增区域配置条目。
"""

import argparse
import asyncio
import logging
import sys
import os

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.crawler.deep_dive import deep_dive_area
from src.crawler.area_keywords import AREA_CONFIG
from src.db.schema import get_conn


def main():
    parser = argparse.ArgumentParser(
        description="Deep Dive — 穷举式搜集目标区域租房信息",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/deep_dive.py --area 下沙
  python scripts/deep_dive.py --area 下沙 --no-douban
  python scripts/deep_dive.py --area 下沙 --no-headless  # 调试

已配置区域: """ + ", ".join(AREA_CONFIG.keys()),
    )
    parser.add_argument("--area", type=str, required=True, help="目标区域名")
    parser.add_argument("--no-douban", action="store_true", help="跳过豆瓣抓取")
    parser.add_argument("--no-headless", action="store_true", help="可见浏览器模式")
    parser.add_argument("--limit", type=int, default=200, help="每关键词最大条目数 (默认 200)")
    args = parser.parse_args()

    if args.area not in AREA_CONFIG:
        print(f"错误: 未配置的区域 '{args.area}'")
        print(f"已配置: {', '.join(AREA_CONFIG.keys())}")
        print(f"在 src/crawler/area_keywords.py 中添加新区域配置")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sources = ["xianyu"]
    if not args.no_douban:
        sources.append("douban")

    # 抓取前统计
    conn = get_conn()
    before_count = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE title LIKE ? OR address LIKE ?",
        (f"%{args.area}%", f"%{args.area}%"),
    ).fetchone()[0]
    conn.close()
    print(f"\n🔍 Deep Dive 开始: [{args.area}]")
    print(f"   DB 现有 {args.area} 房源: {before_count} 条")
    print(f"   数据源: {', '.join(sources)}")
    print()

    # 运行
    async def _run():
        return await deep_dive_area(
            area_name=args.area,
            sources=sources,
            headless=not args.no_headless,
            limit_per_keyword=args.limit,
        )

    result = asyncio.run(_run())

    # 抓取后统计
    conn = get_conn()
    after_count = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE title LIKE ? OR address LIKE ?",
        (f"%{args.area}%", f"%{args.area}%"),
    ).fetchone()[0]
    # 按平台统计
    platform_stats = {}
    for row in conn.execute(
        "SELECT source_platform, COUNT(*) FROM listings WHERE title LIKE ? OR address LIKE ? GROUP BY source_platform",
        (f"%{args.area}%", f"%{args.area}%"),
    ).fetchall():
        platform_stats[row[0]] = row[1]
    conn.close()

    print()
    print("=" * 60)
    print(f"  ✅ Deep Dive [{result['area']}] 完成")
    print(f"  {result['area']} 房源: {before_count} → {after_count} (+{after_count - before_count})")
    if platform_stats:
        for plat, cnt in platform_stats.items():
            print(f"    {plat}: {cnt}")
    print(f"  关键词数:    {result.get('keywords', 0)}")
    print(f"  闲鱼 raw:    {result.get('xianyu_raw', 0)}")
    print(f"  豆瓣 raw:    {result.get('douban_raw', 0)}")
    print(f"  LLM 提取成功: {result.get('extracted', 0)}")
    print(f"  过滤/失败:    {result.get('rejected', 0)}")
    print(f"  新增入库:     {result.get('new_listings', 0)}")
    print(f"  耗时:         {result.get('duration_s', 0)}s")
    if result.get("errors"):
        print(f"  ⚠️  错误 ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"    - {e}")
    print("=" * 60)


if __name__ == "__main__":
    main()
