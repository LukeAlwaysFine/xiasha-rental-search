"""核心 Pipeline — 把抓取、提取、geocode、检测串起来"""

import logging
import os
import re
import asyncio
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("rental.pipeline")

from src.db.schema import get_conn, upsert_listing, search_listings, init_db
from src.crawler.firecrawl_client import (
    crawl_douban_group, scrape_topic_detail,
    DOUBAN_GROUPS,
)
from src.crawler.xianyu_async import crawl_xianyu_async, has_valid_cookies, SEARCH_KEYWORDS
from src.crawler.area_keywords import build_area_keywords
from src.crawler.firecrawl_client import crawl_douban_by_keyword
from src.crawler.douban_http import search_group as douban_search, fetch_topic_detail as douban_fetch_detail
from src.crawler.weibo_crawler import search_weibo as weibo_search
from src.crawler.xhs_crawler import search_xhs as xhs_search, has_cookies as xhs_has_cookies

# 闲鱼 Playwright（可选，需要本地运行 --login 先保存登录态）
_xianyu_state_file = os.path.join(os.path.dirname(__file__), "crawler", "xianyu_state.json")
HAS_XIANYU_STATE = os.path.exists(_xianyu_state_file)
from src.extractor.llm_extract import extract_listing
from src.geocode.amap import geocode, batch_geocode_missing
from src.detector.agent_detector import detect_with_llm, is_sublet_from_content


async def process_listing_item(item: dict, conn, sem: asyncio.Semaphore) -> dict | None:
    """处理单条原始房源：LLM 提取 + 验证 + 中介检测 + 坐标/图片合并。

    供 fetch_new_listings 和 deep_dive 共用，避免代码重复。
    conn 仅用于只读检测查询（count_by_poster / count_by_contact），
    调用方负责 conn 的生命周期管理。

    Returns:
        提取成功的 dict（含 _agent_* 元数据字段），失败返回 None。
    """
    content = item.get("content", "")
    if not content:
        return None

    async with sem:
        extracted = await extract_listing(content)
        if not extracted:
            return None
        if not _is_valid_rental(extracted):
            return None

        extracted["source_url"] = item["url"]
        extracted["source_platform"] = item.get("source", "闲鱼")
        # 优先使用 API 直接提取的 publish_time，LLM 提取的作为 fallback
        if item.get("publish_time") and not extracted.get("publish_time"):
            extracted["publish_time"] = item["publish_time"]
        # 归一化 publish_time 为 ISO 8601（微博等平台输出 HTTP date 格式）
        extracted["publish_time"] = _normalize_publish_time(extracted.get("publish_time"))
        # 过滤超过 30 天的旧房源
        if _is_too_old(extracted.get("publish_time")):
            return None
        if not extracted.get("is_sublet"):
            extracted["is_sublet"] = is_sublet_from_content(content)

        # API 级 poster_id fallback：LLM 提取优先，API 结构化字段兜底
        api_poster = item.get("poster_id", "")
        if not extracted.get("poster_id") and api_poster:
            extracted["poster_id"] = api_poster

        # 中介检测：LLM 推理 + regex 混合判定
        landlord_type, _, detect_meta = detect_with_llm(
            content=content[:2500],
            poster_id=extracted.get("poster_id", ""),
            contact=extracted.get("contact"),
            conn=conn,
            llm_agent_signals=extracted.get("agent_signals", []),
            llm_agent_confidence=extracted.get("agent_confidence", ""),
            llm_agent_reasoning=extracted.get("agent_reasoning", ""),
            seller_item_count=item.get("seller_item_count"),
        )
        extracted["_agent_llm_confidence"] = detect_meta.get("llm_confidence", "")
        extracted["_agent_llm_signals"] = detect_meta.get("llm_signals", [])
        extracted["_agent_llm_reasoning"] = detect_meta.get("llm_reasoning", "")
        extracted["_agent_hybrid_score"] = detect_meta.get("hybrid_score", 0)
        if landlord_type == "疑似中介" and extracted.get("is_sublet"):
            landlord_type = "个人"
        extracted["landlord_type"] = landlord_type

        # 合并 API 提供的结构化图片
        api_imgs = item.get("api_images") or []
        llm_imgs = extracted.get("images") or []
        if api_imgs:
            seen = set(llm_imgs)
            for img in api_imgs:
                if img not in seen:
                    llm_imgs.append(img)
                    seen.add(img)
            extracted["images"] = llm_imgs

        # 优先使用 API 提供的结构化地址（比 LLM 从文本提取的更准确）
        api_addr = item.get("api_address", "")
        if api_addr and "杭州" in str(api_addr) and len(str(api_addr)) > 3:
            extracted["address"] = api_addr

        # 优先使用 API 提供的经纬度
        api_lng = item.get("api_lng")
        api_lat = item.get("api_lat")
        if api_lng is not None and api_lat is not None:
            extracted["lng"], extracted["lat"] = api_lng, api_lat

        # Geocode fallback（仅在无 API 坐标时）
        if extracted.get("lng") is None:
            addr_for_geo = item.get("api_address", "") or extracted.get("address", "")
            if addr_for_geo and "杭州" in str(addr_for_geo):
                try:
                    coords = await geocode(str(addr_for_geo))
                    if coords:
                        extracted["lng"], extracted["lat"] = coords
                except Exception as e:
                    logger.debug(f"geocode failed for '{str(addr_for_geo)[:80]}': {e}")

        return extracted


# ——— 后提取验证：过滤非租房垃圾 ———
# 非租房关键词（卖东西、会员、数码产品、车、活动、商铺等）
_NON_RENTAL_TITLE_RE = re.compile(
    r'(会员|年卡|月卡|绿钻|充值|卡券|硬盘|固态|显卡|手机|笔记本|'
    r'摩托车|二手车|新车|代购|拼单|活动|聚餐|打球|征友|交友|'
    r'商铺|店面|门面|档口|写字楼|办公室|办公出租|车位出租|车库出租|停车位|'
    r'仓库|厂房|棋牌室|服装店|小吃店|餐饮店|美发|理发|按摩店|'
    r'汽修|汽车维修|汽车改装|生意转让|店铺转让|出租转让|'
    r'夜市|美食城|菜市场|农贸|直播间|直播|带货|培训机构|'
    r'健身房出租|健身房转让|健身房年卡|健身房会员|'
    r'瑜伽出租|瑜伽转让|瑜伽年卡|瑜伽会员|'
    r'舞蹈室出租|舞蹈室转让|舞蹈室年卡|舞蹈室会员|'
    r'画室|工作室出租|'
    r'代找|帮忙找|帮找|代找房|找房服务)',
    re.IGNORECASE
)
def _parse_llm_concurrency(default: int = 40) -> int:
    """安全解析 LLM_CONCURRENCY 环境变量，异常时回退默认值。"""
    try:
        return int(os.getenv("LLM_CONCURRENCY", str(default)))
    except (ValueError, TypeError):
        logger.warning(f"LLM_CONCURRENCY 环境变量无效，使用默认值 {default}")
        return default


_MIN_VALID_PRICE = 300   # 低于此价格可能是车位/杂物间等非居住房源
_MAX_VALID_PRICE = 50000  # 超过此价格可能是商铺/写字楼等商用房源


def _is_valid_rental(extracted: dict) -> bool:
    """后提取校验：判断 LLM 提取结果是否为有效的杭州租房。

    条件：有价格、地址含'杭州'、标题不含明显非租房关键词、月租 >= 300。
    """
    if not extracted:
        return False
    title = (extracted.get("title") or "").strip()
    address = (extracted.get("address") or "").strip()
    price = extracted.get("price")

    # 必须有价格
    if price is None:
        return False
    # 月租太低 → 非租房（车位/杂物间等）
    if price < _MIN_VALID_PRICE:
        return False
    # 月租太高 → 商用（商铺/写字楼等）
    if price > _MAX_VALID_PRICE:
        return False
    # 地址必须含杭州
    if "杭州" not in address:
        return False
    # 标题不能命中非租房关键词
    if _NON_RENTAL_TITLE_RE.search(title):
        return False

    return True


async def fetch_new_listings(
    limit_per_source: int = 10,
    keyword_hint: str = "",
    focus_area: str = "",
    progress_callback=None,  # async callable(stage: str) — 进度回调
) -> list[dict]:
    """抓取所有来源的新房源。

    豆瓣: httpx + BeautifulSoup 直接 HTTP（SSR，无需 JS）
    闲鱼: Playwright async（cookie 登录态，MTOP API 拦截）
    微博: Playwright + m.weibo.cn 内部 Ajax API
    小红书: Playwright QR 码登录 + 浏览器内 fetch API（X-S 自动签名）
    已停用：58同城（反爬拦截）、Wellcee（SPA）、安居客（法律风险）、Firecrawl（免费额度耗尽）

    Args:
        limit_per_source: 每源抓取上限
        keyword_hint: 用户搜索关键词（用于定向搜索）
        focus_area: 深潜目标区域（如 "下沙"），启用区域专项关键词矩阵
        progress_callback: 可选异步回调，接收阶段描述字符串
    """
    async def _progress(stage: str):
        if progress_callback:
            await progress_callback(stage)
    all_raw_items: list[dict] = []

    # ——— 豆瓣: HTTP 直接抓取（替代 Firecrawl）———
    try:
        await _progress("正在爬取豆瓣...")
        if focus_area:
            douban_kws = [f"{focus_area}租房", f"{focus_area}转租", f"{focus_area}合租",
                          f"{focus_area}个人转租", f"{focus_area}房东直租"]
        elif keyword_hint:
            douban_kws = [f"{keyword_hint}租房", f"{keyword_hint}转租"]
        else:
            douban_kws = ["杭州租房", "杭州转租"]

        for kw in douban_kws[:3]:  # 最多 3 个关键词
            topics = await douban_search(kw, limit=min(limit_per_source, 15))
            for t in topics:
                all_raw_items.append({
                    "url": t["url"],
                    "source": "豆瓣",
                    "content": None,  # 需要二次抓取详情
                    "publish_time": t.get("publish_time", ""),
                })
        logger.info(f"fetch: 豆瓣 HTTP 抓取完成: {len([i for i in all_raw_items if i['source']=='豆瓣'])} 个话题")
    except Exception as e:
        logger.warning(f"fetch: 豆瓣 HTTP 抓取失败: {e}")

    # ——— 闲鱼: Async Playwright（cookie 登录态）———
    if has_valid_cookies():
        await _progress("正在爬取闲鱼...")
        logger.info("fetch: 使用 Playwright 闲鱼抓取（cookie 已就绪）")
        if focus_area:
            xy_keywords = build_area_keywords(focus_area)
            logger.info(f"fetch: 深潜模式 [{focus_area}]，{len(xy_keywords)} 个关键词")
        elif keyword_hint:
            xy_keywords = [keyword_hint, f"杭州 {keyword_hint} 租房"]
        else:
            xy_keywords = SEARCH_KEYWORDS
        try:
            xy_results = await crawl_xianyu_async(
                keywords=xy_keywords,
                limit_per_keyword=limit_per_source,
                headless=True,
            )
            all_raw_items.extend(xy_results)
            logger.info(f"fetch: 闲鱼 Playwright 抓取完成: {len(xy_results)} 条")
        except Exception as e:
            logger.error(f"fetch: 闲鱼 Playwright 抓取失败: {e}", exc_info=True)
    else:
        logger.warning("fetch: 闲鱼 cookie 未就绪，跳过。请运行: python -m src.crawler.xianyu_async --login")

    # ——— 微博: Playwright + 内部 API ———
    try:
        await _progress("正在爬取微博...")
        if focus_area:
            from src.crawler.weibo_crawler import WEIBO_KEYWORDS
            wb_kws = WEIBO_KEYWORDS[:5]
        elif keyword_hint:
            wb_kws = [keyword_hint, f"{keyword_hint}租房"]
        else:
            from src.crawler.weibo_crawler import WEIBO_KEYWORDS
            wb_kws = WEIBO_KEYWORDS[:3]

        for kw in wb_kws[:3]:
            wb_items = await weibo_search(kw, limit=min(limit_per_source, 15))
            all_raw_items.extend(wb_items)
        logger.info(f"fetch: 微博抓取完成: {len([i for i in all_raw_items if i['source']=='微博'])} 条")
    except Exception as e:
        logger.warning(f"fetch: 微博抓取失败: {e}")

    # ——— 小红书: Playwright QR 码登录 + 浏览器内 fetch API ———
    try:
        if xhs_has_cookies():
            from src.crawler.xhs_crawler import XHS_KEYWORDS
            if focus_area:
                xhs_kws = XHS_KEYWORDS[:5]
            elif keyword_hint:
                xhs_kws = [keyword_hint, f"{keyword_hint} 租房"]
            else:
                xhs_kws = XHS_KEYWORDS[:3]

            for kw in xhs_kws[:3]:
                xhs_items = await xhs_search(kw, limit=min(limit_per_source, 15))
                all_raw_items.extend(xhs_items)
            logger.info(f"fetch: 小红书抓取完成: {len([i for i in all_raw_items if i['source']=='小红书'])} 条")
        else:
            logger.info("fetch: 小红书 cookie 未就绪，跳过。请运行: python -m src.crawler.xhs_crawler --login")
    except Exception as e:
        logger.warning(f"fetch: 小红书抓取失败: {e}")

    if not all_raw_items:
        logger.info("fetch: 无任何 raw items")
        return []

    # ——— 豆瓣详情抓取（仅对豆瓣 item，其他已有 content）———
    await _progress("正在获取帖子详情...")
    douban_detail_sem = asyncio.Semaphore(3)  # 豆瓣限流严格

    async def _maybe_fetch_detail(item: dict) -> dict:
        if item.get("content"):
            return item  # 闲鱼/微博已有 content
        if item["source"] == "豆瓣":
            async with douban_detail_sem:
                for attempt in range(2):
                    try:
                        detail = await douban_fetch_detail(item["url"])
                        if detail:
                            return {**item, "content": detail}
                        if attempt == 0:
                            logger.debug(f"douban detail retry: {item['url'][:80]}")
                            await asyncio.sleep(1)
                    except Exception as e:
                        logger.warning(f"douban detail fetch failed (attempt {attempt+1}/2): {item.get('url','')[:80]} {e}")
                        if attempt < 1:
                            await asyncio.sleep(1.5)
                logger.warning(f"douban detail dropped (all attempts failed): {item.get('url','')[:80]}")
            return None
        return item

    pairs_ready = await asyncio.gather(*[_maybe_fetch_detail(item) for item in all_raw_items], return_exceptions=True)
    # 过滤异常和 None 结果
    valid_pairs = []
    for p in pairs_ready:
        if isinstance(p, Exception):
            logger.warning(f"fetch detail failed: {p}")
            continue
        if p is not None and p.get("content"):
            valid_pairs.append(p)
    pairs_ready = valid_pairs

    # ——— LLM 提取管线 ———
    await _progress("正在 AI 提取房源信息...")
    conn = get_conn()
    try:
        sem = asyncio.Semaphore(_parse_llm_concurrency())

        results = await asyncio.gather(
            *[process_listing_item(item, conn, sem) for item in pairs_ready],
            return_exceptions=True,
        )

        new_listings = []
        for extracted in results:
            if isinstance(extracted, Exception):
                logger.warning(f"process_one failed: {extracted}")
                continue
            if extracted and upsert_listing(conn, extracted):
                new_listings.append(extracted)
        conn.commit()

        # ——— 入库后批量重评中介 ———
        await _progress("正在收尾处理...")
        if new_listings:
            from src.detector.agent_detector import reevaluate_all
            try:
                re_result = reevaluate_all(conn)
                if re_result["updated"] > 0:
                    logger.info(f"fetch_new_listings: 中介重评修正 {re_result['updated']} 条")
            except Exception as e:
                logger.warning(f"fetch_new_listings: 中介重评失败: {e}")

            # 自动补齐缺失坐标（后台，不阻塞）
            try:
                geo_cnt = await batch_geocode_missing()
                if geo_cnt > 0:
                    logger.info(f"fetch_new_listings: 批量 geocode 补齐 {geo_cnt} 条坐标")
            except Exception as e:
                logger.warning(f"fetch_new_listings: 批量 geocode 失败: {e}")

        logger.info(
            f"fetch_new_listings: {len(new_listings)} new listings "
            f"(豆瓣={len([i for i in all_raw_items if i['source']=='豆瓣'])}, "
            f"闲鱼={len([i for i in all_raw_items if i['source']=='闲鱼'])}, "
            f"微博={len([i for i in all_raw_items if i['source']=='微博'])}, "
            f"小红书={len([i for i in all_raw_items if i['source']=='小红书'])})"
        )
        return new_listings
    finally:
        conn.close()


def _normalize_publish_time(publish_time: str | None) -> str | None:
    """将各种格式的 publish_time 归一化为 ISO 8601（UTC）。

    微博等平台输出 HTTP date 格式（如 Wed Apr 15 15:04:10 +0800 2026），
    非 ISO 格式会导致 hours_ago SQL 字符串比较失效（字典序≠时间序）。
    """
    if not publish_time:
        return None
    s = str(publish_time).strip()
    # 已是 ISO 8601 则直接返回
    if "T" in s:
        return s
    # 尝试 HTTP date 格式（微博）
    from datetime import datetime, timezone as dt_timezone
    formats = [
        "%a %b %d %H:%M:%S %z %Y",   # Wed Apr 15 15:04:10 +0800 2026
        "%a, %d %b %Y %H:%M:%S %z",  # Wed, 15 Apr 2026 15:04:10 +0800
        "%a %b %d %H:%M:%S %Y",       # 无时区
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt.astimezone(dt_timezone.utc).isoformat()
        except (ValueError, TypeError):
            continue
    # 无法识别的格式，返回原值（后续 _is_too_old 保守处理）
    return s


def _is_too_old(publish_time: str, max_days: int = 30) -> bool:
    """发布时间超过 max_days 的视为过期，不应入库。

    支持多种常见日期格式。无法解析的日期不拒绝（保守处理为视为近期），
    避免误杀有效房源。
    """
    if not publish_time:
        return False  # 无发布时间 → 不拒绝，无法判断

    # H9: 使用日期粒度比较，避免同一日期在不同时间点行为不一致
    cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max_days)

    # 尝试多种常见格式
    formats = [
        # ISO 8601 变体
        "%Y-%m-%dT%H:%M:%S%z",      # 2024-01-15T14:30:00+08:00
        "%Y-%m-%dT%H:%M:%S",         # 2024-01-15T14:30:00
        "%Y-%m-%d %H:%M:%S",         # 2024-01-15 14:30:00 (SQLite 默认)
        "%Y-%m-%d",                   # 2024-01-15
        # 微博/HTTP date 格式
        "%a %b %d %H:%M:%S %z %Y",   # Mon Jan 15 14:30:00 +0800 2024
        "%a, %d %b %Y %H:%M:%S %z",  # Mon, 15 Jan 2024 14:30:00 +0800
        "%Y-%m-%dT%H:%M:%S.%f%z",    # 带毫秒 ISO
        "%Y-%m-%dT%H:%M:%S.%f",      # 带毫秒无时区
    ]

    for fmt in formats:
        try:
            pub_dt = datetime.strptime(str(publish_time).strip(), fmt)
            # 如果格式无时区，假定为 UTC
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            return pub_dt < cutoff
        except (ValueError, TypeError):
            continue

    # 兜底：尝试 fromisoformat（Python 3.11+ 支持更多格式）
    try:
        pub_dt = datetime.fromisoformat(str(publish_time).strip())
        return pub_dt < cutoff
    except (ValueError, TypeError):
        pass

    # 无法解析 → 不拒绝（可能是"2小时前"等相对时间，判定为近期）
    return False  # M1: 显式返回 False，与 docstring 一致


def search_local(filters: dict) -> list[dict]:
    """搜索本地已缓存的房源。"""
    conn = get_conn()
    try:
        results = search_listings(conn, filters)
        return results
    finally:
        conn.close()


# 后台抓取任务引用，防止被 GC 回收
_bg_fetch_tasks: set[asyncio.Task] = set()


async def search_and_fetch(filters: dict, keyword_hint: str = "") -> list[dict]:
    """搜索 + 后台按需抓取。

    先返回缓存结果（不阻塞用户），结果不足时后台异步补数据。
    用户下次搜索或翻页时就能看到新数据。
    """
    # 无关键词时默认聚焦下沙，避免全城范围结果
    if not filters.get("keyword", "").strip():
        filters["keyword"] = "下沙"

    results = search_local(filters)

    if len(results) < filters.get("min_results", 10):
        # 后台抓取，不阻塞返回
        task = asyncio.create_task(
            fetch_new_listings(limit_per_source=30,
                               keyword_hint=keyword_hint or filters.get("keyword", ""))
        )
        _bg_fetch_tasks.add(task)
        task.add_done_callback(_bg_fetch_tasks.discard)

    return results


async def deep_dive_fetch(area: str) -> dict:
    """深潜抓取：穷举式搜集目标区域所有房源。

    直接调用 fetch_new_listings(focus_area=area)，
    利用 focus_area 启用区域关键词矩阵 + 豆瓣定向搜索。

    Args:
        area: 目标区域名（如 "下沙"）

    Returns:
        {"area": str, "new_listings": int, "errors": list[str]}
    """
    logger.info(f"deep_dive_fetch: [{area}] 开始深潜...")
    try:
        new_listings = await fetch_new_listings(
            limit_per_source=200,
            focus_area=area,
        )
        logger.info(f"deep_dive_fetch: [{area}] 完成，新增 {len(new_listings)} 条")
        return {"area": area, "new_listings": len(new_listings), "errors": []}
    except Exception as e:
        logger.error(f"deep_dive_fetch: [{area}] 失败: {e}", exc_info=True)
        return {"area": area, "new_listings": 0, "errors": [str(e)]}


# ——— 自检脚本 ———
async def _demo():
    """ponytail: 最小自检，验证 pipeline 各环节能跑通。"""
    init_db("test_rental.db")
    logging.basicConfig(level=logging.INFO)
    logger.info("demo: 数据库初始化完成")

    # 测试搜索空库
    results = search_local({"keyword": "未来科技城", "limit": 5})
    logger.info(f"demo: 本地搜索测试: {len(results)} 条结果 (预期 0)")

    logger.info("demo: 如需测试完整抓取流程，请先启动 Firecrawl Docker 并配置 .env")


if __name__ == "__main__":
    asyncio.run(_demo())
