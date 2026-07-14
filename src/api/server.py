"""FastAPI 后端 — 搜索、定时抓取"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.schema import init_db, get_conn, upsert_listing, count_listings, log_search
from src.db.schema import add_favorite, remove_favorite, get_favorite_ids, get_favorites, is_favorited
from src.pipeline import search_and_fetch, fetch_new_listings, _is_valid_rental, _normalize_publish_time, _is_too_old
from src.extractor.llm_extract import extract_listing
from src.geocode.amap import geocode
from src.detector.agent_detector import detect_with_llm, is_sublet_from_content
from src.filter.llm_rerank import rerank_listings
from src.filter.llm_dedup import merge_duplicates
from src.http_client import close_http_client

# ——— 结构化日志 ———
import uuid
import time as _time
import contextvars

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    """为每条日志注入 request_id，便于关联同一次搜索的所有日志。
    使用 contextvars.ContextVar 保证线程/协程安全 (L6)。
    """

    def filter(self, record):
        if not hasattr(record, 'request_id'):
            record.request_id = _request_id_ctx.get()
        return True

    @staticmethod
    def set_request_id(rid: str | None):
        _request_id_ctx.set(rid or "-")

    @staticmethod
    def new_request_id() -> str:
        rid = uuid.uuid4().hex[:8]
        _request_id_ctx.set(rid)
        return rid

# 根 logger 用简单格式（避免 request_id 缺失导致其他模块报错）
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)

# 自定义 Formatter：优雅处理缺失的 request_id
class _SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'request_id'):
            record.request_id = '-'
        return super().format(record)

# 为 rental logger 添加 request_id 的 handler
_rental_handler = logging.StreamHandler()
_rental_handler.setFormatter(_SafeFormatter(
    "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"
))
logger = logging.getLogger("rental")
logger.addFilter(RequestIdFilter())
logger.handlers = [_rental_handler]
logger.propagate = False

# LLM 过滤开关（环境变量控制）
LLM_RERANK = os.getenv("LLM_RERANK", "true").lower() in ("1", "true", "yes")
LLM_DEDUP = os.getenv("LLM_DEDUP", "true").lower() in ("1", "true", "yes")


# ——— 简易速率限制（token bucket, per IP） ———
_rate_limits: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()  # 防御性锁，保护 check-append 原子性
_RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))  # 秒
_RATE_MAX_SEARCH = int(os.getenv("RATE_MAX_SEARCH", "30"))    # /api/search: 30 req/min
_RATE_MAX_IMPORT = int(os.getenv("RATE_MAX_IMPORT", "2"))     # /api/import: 2 req/min
_RATE_MAX_FETCH = int(os.getenv("RATE_MAX_FETCH", "2"))       # /api/fetch: 2 req/min
_RATE_MAX_POSTER_CHECK = int(os.getenv("RATE_MAX_POSTER_CHECK", "1"))  # /api/poster-check: 1 req/min

# ——— Input Tips 缓存（减少高德 API 调用，免费额度仅 5000/日）———
_inputtips_cache: dict[str, tuple[float, list[dict]]] = {}
_inputtips_cache_lock = threading.Lock()
_INPUTTIPS_CACHE_TTL = 86400  # 24h，地址提示结果稳定无需频繁刷新
_INPUTTIPS_CACHE_MAX = 5000   # 最多缓存 5000 个不同关键词


def _check_rate_limit(ip: str, max_req: int, endpoint: str = "search") -> bool:
    """返回 True 表示允许，False 表示限流。按 endpoint 分离桶。"""
    now = time.time()
    window = now - _RATE_WINDOW
    key = f"{ip}:{endpoint}"
    with _rate_lock:
        _rate_limits[key] = [t for t in _rate_limits[key] if t > window]
        # 清理空条目，防止字典无限增长
        if not _rate_limits[key]:
            _rate_limits[key] = [now]
            return True
        if len(_rate_limits[key]) >= max_req:
            return False
        _rate_limits[key].append(now)
        return True


# ——— 后台 LLM 增强缓存 ———
_enhance_cache: dict[str, dict] = {}  # {cache_key: {"listings": [...], "ready": bool}}
_enhance_lock = asyncio.Lock()
_MAX_ENHANCE_CACHE = 200  # 最大缓存条目数，防止内存无限增长


async def _bg_llm_enhance(cache_key: str, listings: list[dict], keyword: str):
    """后台 LLM 增强：重排 + 去重，结果写入内存缓存和 DB _llm_score 字段。"""
    try:
        enhanced = listings
        if LLM_RERANK:
            enhanced = await rerank_listings(enhanced, keyword)
        if LLM_DEDUP:
            enhanced = await merge_duplicates(enhanced)
        # 写回 DB _llm_score（下次搜索直接受益）
        conn = get_conn()
        try:
            for item in enhanced:
                score = item.get("_score")
                if score is not None:
                    conn.execute(
                        "UPDATE listings SET _llm_score=? WHERE source_url=?",
                        (score, item.get("source_url", "")),
                    )
            conn.commit()
        finally:
            conn.close()
        async with _enhance_lock:
            _enhance_cache[cache_key] = {"listings": enhanced, "ready": True, "ts": time.time()}
    except Exception as e:
        logger.warning(f"_bg_llm_enhance failed: {e}")
        async with _enhance_lock:
            _enhance_cache[cache_key] = {"listings": listings, "ready": False, "ts": time.time()}


async def _cleanup_enhance_cache():
    """定期清理过期的增强缓存条目（> 120s），并限制最大条目数。"""
    while True:
        await asyncio.sleep(60)
        async with _enhance_lock:
            stale = [k for k, v in _enhance_cache.items()
                       if time.time() - v.get("ts", 0) > 120]  # 包括失败条目，防止 OOM
            for k in stale:
                del _enhance_cache[k]
            # 如果仍然超过上限，淘汰最老的条目（LRU 策略）
            while len(_enhance_cache) > _MAX_ENHANCE_CACHE:
                oldest = min(_enhance_cache.keys(), key=lambda k: _enhance_cache[k].get("ts", 0))
                del _enhance_cache[oldest]


# ——— 手动抓取状态跟踪 ———
# total_stages: 准备中(1) + 豆瓣(2) + 闲鱼(3) + 微博(4) + 详情(5) + AI提取(6) + 收尾(7) = 7
_fetch_status = {"running": False, "new_count": 0, "started_at": None, "finished_at": None, "error": None, "stage": "", "stage_started_at": None, "stage_times": [], "total_stages": 7}

# ——— Poster 中介检查状态 ———
_poster_check_status = {"running": False, "found": 0, "suspected": 0, "personal": 0, "checked": 0, "total": 0, "started_at": None, "finished_at": None, "error": None}

# ——— 定时任务 ———
scheduler = AsyncIOScheduler()


async def scheduled_poster_check():
    """每6小时：访问发帖人闲鱼主页，检测中介（Playwright 自动化，不消耗 LLM）。"""
    from src.crawler.xianyu_async import check_posters_for_agents
    logger.info("scheduled_poster_check: 开始中介检测...")
    try:
        result = await check_posters_for_agents(headless=True)
        logger.info(
            f"scheduled_poster_check: 检查 {result['checked']} 个发帖人, "
            f"发现 {result['found']} 个中介, {result['suspected']} 个疑似, {result['personal']} 个个人"
        )
    except Exception as e:
        logger.error(f"scheduled_poster_check: 失败: {e}", exc_info=True)


async def scheduled_cleanup():
    """每6小时：URL 存活检测 + 删除过期房源 + 非租房重新验证。"""
    from src.cleanup.url_checker import cleanup_expired_listings
    logger.info("scheduled_cleanup: 开始过期房源检测...")
    try:
        stats = await cleanup_expired_listings(batch_size=50)
        logger.info(
            f"scheduled_cleanup: {stats['total']} checked, "
            f"{stats['deleted']} deleted, {stats['alive']} alive, {stats['skipped']} skipped"
        )
    except Exception as e:
        logger.error(f"scheduled_cleanup: 失败: {e}", exc_info=True)

    # H7: 定期重新验证所有标题匹配 _NON_RENTAL_TITLE_RE 的条目
    try:
        conn = get_conn()
        try:
            from src.pipeline import _NON_RENTAL_TITLE_RE
            rows = conn.execute(
                "SELECT id, title FROM listings WHERE title IS NOT NULL AND title != ''"
            ).fetchall()
            deleted = 0
            for row_id, title in rows:
                if _NON_RENTAL_TITLE_RE.search(title):
                    conn.execute("DELETE FROM listings WHERE id=?", (row_id,))
                    deleted += 1
            if deleted > 0:
                conn.commit()
                logger.info(f"scheduled_cleanup: 非租房重新验证删除 {deleted} 条")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"scheduled_cleanup: 非租房重新验证失败: {e}", exc_info=True)


async def scheduled_fetch():
    """每6小时：下沙专项抓取 + 导入宿主机闲鱼数据。"""
    logger.info("scheduled_fetch: 开始下沙专项抓取...")
    try:
        new_listings = await fetch_new_listings(
            limit_per_source=30,
            focus_area="下沙",
        )
        logger.info(f"scheduled_fetch: 新增 {len(new_listings)} 条房源")
    except Exception as e:
        logger.error(f"scheduled_fetch: 抓取失败: {e}", exc_info=True)

    # 检查宿主机闲鱼同步文件
    import_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "xianyu.json")
    if os.path.exists(import_file):
        try:
            with open(import_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if data:
                imported = await _import_listings(data)
                logger.info(f"scheduled_fetch: 闲鱼导入 {imported} 条")
            # 导入成功后才删除，防止崩溃丢数据
            os.remove(import_file)
            logger.debug("scheduled_fetch: 已删除 xianyu.json")
        except Exception as e:
            logger.error(f"scheduled_fetch: 闲鱼导入失败: {e}", exc_info=True)


# ——— App 生命周期 ———
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 启动预热: 用 asyncio.create_task 在后台运行，不阻塞请求
    # 抓取已改为手动触发（/api/fetch），不再定时执行
    scheduler.add_job(scheduled_cleanup, "interval", hours=6, id="cleanup",
                      coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(scheduled_poster_check, "interval", hours=6, id="poster_check",
                      coalesce=True, misfire_grace_time=3600)
    scheduler.start()
    # 启动增强缓存清理任务
    asyncio.create_task(_cleanup_enhance_cache())
    # 抓取已改为手动触发，不再需要启动预热
    yield
    scheduler.shutdown()
    await close_http_client()


async def _warmup_fetch():
    """启动预热：等 5 秒后大量抓取（后台，不阻塞请求）。"""
    await asyncio.sleep(5)
    logger.info("warmup: Starting initial bulk fetch...")
    try:
        new_listings = await fetch_new_listings(limit_per_source=200)
        logger.info(f"warmup: Initial fetch done: {len(new_listings)} new listings")
    except Exception as e:
        logger.error(f"warmup: Fetch failed: {e}", exc_info=True)


app = FastAPI(title="杭州租房搜索", lifespan=lifespan)


# ——— CSP 安全头中间件 ———
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    from fastapi.responses import Response
    response = await call_next(request)
    if isinstance(response, Response) and "text/html" in (response.headers.get("content-type", "") or ""):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://*.amap.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' https: data: blob:; "
            "connect-src 'self' https://*.amap.com https://*.is.autonavi.com; "
            "worker-src 'self' blob:"
        )
    return response


# ——— API 路由 ———
@app.get("/api/search")
async def search(
    request: Request,
    keyword: str = Query(default="", description="搜索关键词"),
    price_min: float = Query(default=None),
    price_max: float = Query(default=None),
    house_types: str = Query(default="", description="逗号分隔: 一居,两居"),
    rent_types: str = Query(default="", description="逗号分隔: 整租,合租,单间,转租"),
    landlord_types: str = Query(default="", description="逗号分隔: 个人,中介"),
    source_platforms: str = Query(default="", description="逗号分隔: 豆瓣,闲鱼"),
    hours_ago: int = Query(default=None, description="发布时间限制(小时)"),
    sort: str = Query(default="comprehensive"),
    limit: int = Query(default=20, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    ref_lng: float = Query(default=None, description="参考经度（距离排序用）"),
    ref_lat: float = Query(default=None, description="参考纬度（距离排序用）"),
    favorites_only: bool = Query(default=False, description="仅显示收藏房源"),
):
    """搜索房源。先查缓存，不足则后台抓取。

    LLM 增强（通过环境变量 LLM_RERANK / LLM_DEDUP 控制）：
    - 有关键词时，LLM 对结果做相关性重排，过滤不相关房源
    - 跨平台去重合并（同一房源在多个平台只展示一次）
    """
    # 速率限制 — 从反向代理头获取真实 IP
    if request:
        forwarded = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )
    else:
        client_ip = "unknown"
    if not _check_rate_limit(client_ip, _RATE_MAX_SEARCH):
        return JSONResponse({"error": "请求过于频繁，请稍后再试"}, status_code=429)

    # 生成请求 ID，关联本次搜索的所有日志
    rid = RequestIdFilter.new_request_id()
    t0 = _time.time()
    # 日志消毒：去除控制字符，防止日志注入
    safe_keyword = ''.join(c for c in keyword if c.isprintable() or c in (' ', '\t'))
    logger.info(f"search: keyword='{safe_keyword[:80]}', sort={sort}, limit={limit}, offset={offset}")

    filters = {
        "keyword": keyword,
        "price_min": price_min,
        "price_max": price_max,
        "house_types": _split(house_types),
        "rent_types": _split(rent_types),
        "landlord_types": _split(landlord_types),
        "source_platforms": _split(source_platforms),
        "hours_ago": hours_ago,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "min_results": 10,  # 低于此数量触发后台补数据
        "favorites_only": favorites_only,
    }
    if ref_lng is not None and ref_lat is not None:
        filters["ref_lng"] = ref_lng
        filters["ref_lat"] = ref_lat
    results = await search_and_fetch(filters, keyword_hint=keyword)

    # ——— LLM 增强管线（异步化）———
    # 先用 SQL scoring 返回结果，LLM 重排/去重在后台执行
    # 前端通过 /api/search/enhance 轮询获取增强结果
    enhance_key = None
    if keyword and results and LLM_RERANK:
        enhance_key = hashlib.md5(
            f"{keyword}|{','.join(str(r['id']) for r in results[:30])}".encode()
        ).hexdigest()
        # 在同一个锁保护区域内检查和写入，防止 TOCTOU 竞态
        async with _enhance_lock:
            cached = _enhance_cache.get(enhance_key)
            if cached and cached.get("ready"):
                results = cached["listings"]
                enhance_key = None  # 不需要再轮询
            else:
                # 写入占位条目（仅当不存在时才写，避免覆盖已有条目）
                if enhance_key not in _enhance_cache:
                    _enhance_cache[enhance_key] = {"ready": False, "ts": time.time()}
        # 在锁外启动后台任务
        if enhance_key:
            asyncio.create_task(_bg_llm_enhance(enhance_key, results, keyword))

    # 计算符合条件的真实总数（用于前端分页显示）
    conn = get_conn()
    try:
        total = count_listings(conn, filters)
    finally:
        conn.close()

    elapsed = int((_time.time() - t0) * 1000)
    logger.info(f"search done: {len(results)} results, total={total}, elapsed={elapsed}ms")

    # 记录搜索日志（异步，不阻塞返回）
    if keyword.strip():
        try:
            log_conn = get_conn()
            try:
                log_search(log_conn, keyword, total)
            finally:
                log_conn.close()
        except Exception:
            pass  # 日志记录失败不影响搜索结果

    return {"total": total, "listings": results, "enhance_key": enhance_key}


@app.get("/api/search/enhance")
async def poll_enhance(key: str = Query(..., description="enhance_key from /api/search")):
    """轮询 LLM 增强结果。前端在拿到 enhance_key 后轮询此端点。"""
    async with _enhance_lock:
        entry = _enhance_cache.get(key)
    if not entry:
        return {"ready": False}
    if entry.get("ready"):
        # 返回后清理
        async with _enhance_lock:
            _enhance_cache.pop(key, None)
        return {"ready": True, "listings": entry["listings"]}
    return {"ready": False}


@app.get("/api/geocode")
async def geocode_address(address: str = Query(..., description="地址文本")):
    """地址→经纬度（前端通勤距离搜索用）。"""
    # M14: 空地址不应返回城市中心坐标
    if not address or not address.strip():
        return JSONResponse({"error": "地址不能为空"}, status_code=400)
    try:
        coords = await geocode(address)
        if coords:
            return {"lng": coords[0], "lat": coords[1], "address": address}
        return JSONResponse({"error": "无法解析该地址"}, status_code=404)
    except Exception as e:
        logger.warning(f"geocode failed for '{address[:80]}': {e}")
        return JSONResponse({"error": "地理编码服务暂不可用，请稍后重试"}, status_code=503)


@app.get("/api/inputtips")
async def input_tips(keywords: str = Query(..., description="搜索关键词")):
    """高德输入提示 — 地址自动补全下拉菜单。带 24h 缓存减少 API 调用。"""
    # 过短关键词不查（也防止缓存被单字符垃圾撑满）
    kw = keywords.strip()
    if len(kw) < 2:
        return {"tips": []}

    # —— 查缓存 ——
    now = time.time()
    with _inputtips_cache_lock:
        cached = _inputtips_cache.get(kw)
        if cached and (now - cached[0]) < _INPUTTIPS_CACHE_TTL:
            return {"tips": cached[1], "cached": True}

    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://restapi.amap.com/v3/assistant/inputtips",
                params={
                    "key": os.getenv("AMAP_API_KEY", ""),
                    "keywords": kw,
                    "city": "杭州",
                    "citylimit": "true",
                },
                timeout=5.0,
            )
            data = resp.json()
            if data.get("status") == "1" and data.get("tips"):
                tips = []
                for t in data["tips"][:8]:
                    loc = t.get("location", "")
                    lng, lat = None, None
                    if loc and "," in loc:
                        parts = loc.split(",")
                        try:
                            lng, lat = float(parts[0]), float(parts[1])
                        except ValueError:
                            pass
                    tips.append({
                        "name": t.get("name", ""),
                        "district": t.get("district", ""),
                        "address": t.get("address", ""),
                        "lng": lng,
                        "lat": lat,
                    })
                # —— 写入缓存（限制大小，避免内存泄漏）——
                with _inputtips_cache_lock:
                    if len(_inputtips_cache) >= _INPUTTIPS_CACHE_MAX:
                        # 清理过期的缓存条目
                        expired = [k for k, v in _inputtips_cache.items() if (now - v[0]) >= _INPUTTIPS_CACHE_TTL]
                        for k in expired:
                            del _inputtips_cache[k]
                        # 如果还是满的，随机清理 10% 最旧的
                        if len(_inputtips_cache) >= _INPUTTIPS_CACHE_MAX:
                            sorted_keys = sorted(_inputtips_cache.keys(), key=lambda k: _inputtips_cache[k][0])
                            for k in sorted_keys[: max(1, len(sorted_keys) // 10)]:
                                del _inputtips_cache[k]
                    _inputtips_cache[kw] = (now, tips)
                return {"tips": tips}
            return {"tips": []}
    except Exception as e:
        logger.warning(f"inputtips failed for '{kw[:80]}': {e}")
        # 高德 API 失败时，尝试返回过期缓存作为降级
        with _inputtips_cache_lock:
            cached = _inputtips_cache.get(kw)
            if cached:
                return {"tips": cached[1], "cached": True, "stale": True}
        return JSONResponse({"tips": [], "error": "地址提示服务暂不可用"}, status_code=503)


@app.get("/api/listing/{listing_id}")
async def get_listing(listing_id: int):
    """获取单条房源详情（标记点击时按需加载卡片）。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    # 复用 row_to_dict 的 images 解析逻辑
    from src.db.schema import row_to_dict
    return row_to_dict(row)


# ——— 收藏 API ———

@app.get("/api/favorites")
async def list_favorites(limit: int = Query(default=200, ge=1, le=1000)):
    """获取所有收藏房源（按收藏时间倒序）。"""
    conn = get_conn()
    try:
        favs = get_favorites(conn, limit=limit)
        return {"favorites": favs, "total": len(favs)}
    finally:
        conn.close()


@app.post("/api/favorites/{listing_id}")
async def add_favorite_endpoint(listing_id: int):
    """添加收藏。"""
    conn = get_conn()
    try:
        # 验证房源存在
        row = conn.execute("SELECT id FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "房源不存在"}, status_code=404)
        ok = add_favorite(conn, listing_id)
        return {"success": True, "action": "added" if ok else "already_exists"}
    finally:
        conn.close()


@app.delete("/api/favorites/{listing_id}")
async def remove_favorite_endpoint(listing_id: int):
    """取消收藏。"""
    conn = get_conn()
    try:
        ok = remove_favorite(conn, listing_id)
        return {"success": True, "action": "removed" if ok else "not_found"}
    finally:
        conn.close()


@app.get("/api/favorites/ids")
async def list_favorite_ids():
    """获取所有收藏的 listing_id 列表（轻量，用于前端标记）。"""
    conn = get_conn()
    try:
        ids = get_favorite_ids(conn)
        return {"ids": ids}
    finally:
        conn.close()


@app.get("/api/fetch/status")
async def fetch_status():
    """查询手动抓取状态（供前端轮询）。"""
    return {
        "running": _fetch_status["running"],
        "new_count": _fetch_status["new_count"],
        "started_at": _fetch_status["started_at"],
        "finished_at": _fetch_status["finished_at"],
        "error": _fetch_status["error"],
        "stage": _fetch_status.get("stage", ""),
        "stage_started_at": _fetch_status.get("stage_started_at"),
        "stage_times": _fetch_status.get("stage_times", []),
        "total_stages": _fetch_status.get("total_stages", 7),
    }


@app.get("/api/fetch")
async def trigger_fetch(request: Request):
    """手动触发一次抓取。已在运行时返回 409。"""
    global _fetch_status
    if _fetch_status["running"]:
        return JSONResponse({"error": "抓取正在进行中", "started_at": _fetch_status["started_at"], "stage": _fetch_status.get("stage", "")}, status_code=409)

    # M12: 简单共享密钥认证，防止未授权触发资源密集型操作
    auth_token = os.getenv("FETCH_AUTH_TOKEN", "")
    if auth_token:
        provided = request.headers.get("X-Auth-Token", "")
        if provided != auth_token:
            return JSONResponse({"error": "未授权"}, status_code=401)

    # M11: 从 X-Forwarded-For 获取真实 IP（与 search endpoint 一致）
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "127.0.0.1"
    )
    if not _check_rate_limit(client_ip, _RATE_MAX_FETCH, "fetch"):
        return JSONResponse({"error": "请求过于频繁，请稍后再试"}, status_code=429)

    async def _on_progress(stage: str):
        global _fetch_status
        now = time.time()
        # 记录上一阶段的耗时
        prev_stage = _fetch_status.get("stage", "")
        prev_started = _fetch_status.get("stage_started_at")
        if prev_stage and prev_started:
            _fetch_status["stage_times"].append({
                "stage": prev_stage,
                "duration": round(now - prev_started, 1)
            })
        _fetch_status["stage"] = stage
        _fetch_status["stage_started_at"] = now

    _fetch_status = {"running": True, "new_count": 0, "started_at": time.time(), "finished_at": None, "error": None, "stage": "准备中...", "stage_started_at": time.time(), "stage_times": [], "total_stages": 7}

    def _record_final_stage(final_stage: str):
        """在最终状态写入前，记录当前运行阶段的耗时。"""
        global _fetch_status
        now = time.time()
        cur_stage = _fetch_status.get("stage", "")
        cur_started = _fetch_status.get("stage_started_at")
        if cur_stage and cur_started:
            _fetch_status["stage_times"].append({
                "stage": cur_stage,
                "duration": round(now - cur_started, 1)
            })
        _fetch_status["stage"] = final_stage
        _fetch_status["stage_started_at"] = None
        _fetch_status["stage_times"] = _fetch_status.get("stage_times", [])

    try:
        new_listings = await fetch_new_listings(limit_per_source=50, progress_callback=_on_progress)
        _record_final_stage("已完成")
        _fetch_status["running"] = False
        _fetch_status["new_count"] = len(new_listings)
        _fetch_status["finished_at"] = time.time()
        _fetch_status["error"] = None

        # 入库后自动触发一次 Poster 检查（fire-and-forget，不阻塞响应）
        # 每次都跑 — check_posters_for_agents 内部有 poster_checked_at 缓存，会自动跳过已检查的
        async def _auto_poster_check():
            await asyncio.sleep(2)
            try:
                from src.crawler.xianyu_async import check_posters_for_agents
                await check_posters_for_agents(headless=True)
            except Exception as e:
                logger.warning(f"auto_poster_check failed: {e}")
        asyncio.create_task(_auto_poster_check())

        return {"new_count": len(new_listings)}
    except Exception as e:
        _record_final_stage("出错")
        _fetch_status["running"] = False
        _fetch_status["new_count"] = 0
        _fetch_status["finished_at"] = time.time()
        _fetch_status["error"] = "抓取失败，请查看服务端日志"
        logger.error(f"trigger_fetch failed: {e}", exc_info=True)
        return JSONResponse({"error": "抓取服务暂时不可用，请稍后重试"}, status_code=503)


# ——— Poster 中介检查（Playwright 自动化） ———
@app.post("/api/poster-check")
async def trigger_poster_check(request: Request):
    """手动触发中介检测：访问发帖人闲鱼主页，数出租房数量。已在运行时返回 409。"""
    global _poster_check_status
    if _poster_check_status["running"]:
        return JSONResponse({
            "error": "中介检测正在进行中",
            "started_at": _poster_check_status["started_at"],
        }, status_code=409)

    auth_token = os.getenv("FETCH_AUTH_TOKEN", "")
    if auth_token:
        provided = request.headers.get("X-Auth-Token", "")
        if provided != auth_token:
            return JSONResponse({"error": "未授权"}, status_code=401)

    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "127.0.0.1"
    )
    if not _check_rate_limit(client_ip, _RATE_MAX_POSTER_CHECK, "poster_check"):
        return JSONResponse({"error": "请求过于频繁，请稍后再试"}, status_code=429)

    # ?reset=true 清除所有 poster_checked_at 缓存，全量重判
    _reset = request.query_params.get("reset", "").lower() == "true"
    if _reset:
        from src.db.schema import get_conn
        _conn_reset = get_conn()
        try:
            _conn_reset.execute("UPDATE listings SET poster_checked_at = NULL")
            _conn_reset.commit()
            logger.info("poster-check: 已清除所有 poster_checked_at 缓存，准备全量重判")
        finally:
            _conn_reset.close()

    _poster_check_status = {
        "running": True, "found": 0, "suspected": 0, "personal": 0, "checked": 0, "total": 0,
        "started_at": time.time(), "finished_at": None, "error": None,
    }

    try:
        from src.crawler.xianyu_async import check_posters_for_agents
        result = await check_posters_for_agents(headless=True)
        _poster_check_status["running"] = False
        _poster_check_status["found"] = result.get("found", 0)
        _poster_check_status["suspected"] = result.get("suspected", 0)
        _poster_check_status["personal"] = result.get("personal", 0)
        _poster_check_status["checked"] = result.get("checked", 0)
        _poster_check_status["total"] = result.get("total", 0)
        _poster_check_status["finished_at"] = time.time()
        _poster_check_status["error"] = result.get("error")
        return {"found": result.get("found", 0), "suspected": result.get("suspected", 0), "personal": result.get("personal", 0), "checked": result.get("checked", 0), "total": result.get("total", 0)}
    except Exception as e:
        _poster_check_status["running"] = False
        _poster_check_status["finished_at"] = time.time()
        _poster_check_status["error"] = "检测失败"
        logger.error(f"trigger_poster_check failed: {e}", exc_info=True)
        return JSONResponse({"error": "检测服务暂时不可用"}, status_code=503)


@app.get("/api/poster-check/status")
async def poster_check_status():
    """查询中介检测状态（供前端轮询）。"""
    return {
        "running": _poster_check_status["running"],
        "found": _poster_check_status["found"],
        "suspected": _poster_check_status["suspected"],
        "personal": _poster_check_status["personal"],
        "checked": _poster_check_status["checked"],
        "total": _poster_check_status["total"],
        "started_at": _poster_check_status["started_at"],
        "finished_at": _poster_check_status["finished_at"],
        "error": _poster_check_status["error"],
    }


async def _import_listings(data: list[dict]) -> int:
    """批量并行导入房源列表。

    注意：LLM 提取可并行（纯 HTTP I/O），但 SQLite 读写必须串行。
    每个 worker 使用独立 DB 连接做只读查询，写入在主连接串行执行。
    """
    # 主连接用于写入
    main_conn = get_conn()
    # 写入锁，防止与其他并发导入冲突
    write_lock = asyncio.Lock()

    async def process(item: dict) -> dict | None:
        content = item.get("content", "")
        if not content:
            return None
        extracted = await extract_listing(content)
        if not extracted:
            return None
        # 后提取验证 — 过滤非租房垃圾
        if not _is_valid_rental(extracted):
            return None
        extracted["source_url"] = item.get("url", "")
        extracted["source_platform"] = item.get("source", "闲鱼")
        if not extracted.get("is_sublet"):
            extracted["is_sublet"] = is_sublet_from_content(content)
        # publish_time：API 优先，归一化为 ISO 8601，过滤超 30 天
        if item.get("publish_time") and not extracted.get("publish_time"):
            extracted["publish_time"] = item["publish_time"]
        extracted["publish_time"] = _normalize_publish_time(extracted.get("publish_time"))
        if _is_too_old(extracted.get("publish_time")):
            return None
        # API 级 poster_id fallback：LLM 提取优先，API 结构化字段兜底
        api_poster = item.get("poster_id", "")
        if not extracted.get("poster_id") and api_poster:
            extracted["poster_id"] = api_poster
        # 中介检测：LLM 推理 + regex 混合判定
        detect_conn = get_conn()
        try:
            landlord_type, _, _detect_meta = detect_with_llm(
                content=content[:2500],
                poster_id=extracted.get("poster_id", ""),
                contact=extracted.get("contact"),
                conn=detect_conn,
                llm_agent_signals=extracted.get("agent_signals", []),
                llm_agent_confidence=extracted.get("agent_confidence", ""),
                llm_agent_reasoning=extracted.get("agent_reasoning", ""),
                seller_item_count=item.get("seller_item_count"),
            )
        finally:
            detect_conn.close()
        extracted["landlord_type"] = landlord_type
        # 优先使用 API 提供的结构化地址
        api_addr = item.get("api_address", "")
        if api_addr and "杭州" in str(api_addr) and len(str(api_addr)) > 3:
            extracted["address"] = api_addr
        if extracted.get("address") and "杭州" in str(extracted.get("address", "")):
            coords = await geocode(extracted["address"])
            if coords:
                extracted["lng"], extracted["lat"] = coords
        return extracted

    # 并行 LLM 提取 + geocode（纯 I/O，不涉及 SQLite 写入）
    sem = asyncio.Semaphore(_parse_llm_concurrency())
    async def limited(item):
        async with sem:
            return await process(item)

    results = await asyncio.gather(*[limited(item) for item in data], return_exceptions=True)

    # 写入阶段：串行 upsert，使用锁保护
    imported = 0
    async with write_lock:
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"import item failed: {r}")
                continue
            if r and upsert_listing(main_conn, r):
                imported += 1
        main_conn.commit()
    main_conn.close()
    return imported


@app.post("/api/import")
async def import_listings(data: list[dict], request: Request):
    """批量导入房源。宿主机 Playwright 抓取后 POST 到此处入库。"""
    # M11: 从 X-Forwarded-For 获取真实 IP
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "127.0.0.1"
    )
    if not _check_rate_limit(client_ip, _RATE_MAX_IMPORT, "import"):
        return JSONResponse({"error": "请求过于频繁，请稍后再试"}, status_code=429)
    # 基本校验：限制数组大小，防止超大 JSON DoS
    if len(data) > 500:
        return JSONResponse({"error": "单次最多导入 500 条"}, status_code=400)
    # 请求体大小限制：1 MB
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 1_000_000:
        return JSONResponse({"error": "请求体过大，最大 1 MB"}, status_code=413)
    try:
        imported = await _import_listings(data)
        return {"imported": imported, "message": f"成功导入 {imported} 条房源"}
    except Exception as e:
        logger.error(f"import_listings failed: {e}", exc_info=True)
        return JSONResponse({"error": "导入服务暂时不可用，请稍后重试"}, status_code=503)


def _split(s: str) -> list[str] | None:
    """逗号分隔字符串 → 列表，空字符串 → None"""
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_llm_concurrency(default: int = 40) -> int:
    """安全解析 LLM_CONCURRENCY 环境变量，异常时回退默认值。"""
    try:
        return int(os.getenv("LLM_CONCURRENCY", str(default)))
    except (ValueError, TypeError):
        logger.warning(f"LLM_CONCURRENCY 环境变量无效，使用默认值 {default}")
        return default


# ——— 静态文件 ———
import os as _os
_web_dir = _os.path.join(_os.path.dirname(__file__), "..", "web")
if _os.path.isdir(_web_dir):
    app.mount("/static", StaticFiles(directory=_web_dir), name="static")


_CACHED_HTML: str | None = None
_CACHED_HTML_MTIME: float = 0.0  # 缓存时的文件修改时间，变更后自动刷新


def _load_html() -> str:
    """加载并缓存 index.html，文件变更时自动刷新。"""
    global _CACHED_HTML, _CACHED_HTML_MTIME
    html_path = _os.path.join(_os.path.dirname(__file__), "..", "web", "index.html")
    mtime = _os.path.getmtime(html_path)
    if _CACHED_HTML is None or mtime != _CACHED_HTML_MTIME:
        with open(html_path, "r", encoding="utf-8") as f:
            _CACHED_HTML = f.read()
        _CACHED_HTML = _CACHED_HTML.replace("{{AMAP_JS_KEY}}", os.getenv("AMAP_JS_KEY", ""))
        _CACHED_HTML = _CACHED_HTML.replace("{{AMAP_SECURITY_CODE}}", os.getenv("AMAP_SECURITY_CODE", ""))
        _CACHED_HTML_MTIME = mtime
    return _CACHED_HTML


@app.get("/")
async def index():
    """前端首页，动态注入高德 API Key（模板文件变更时自动刷新缓存）。"""
    return HTMLResponse(content=_load_html())
