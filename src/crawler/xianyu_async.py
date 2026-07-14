"""闲鱼 Async Playwright 抓取器 — 集成到 pipeline 的异步版本。

与同步版 xianyu_scraper.py 共用 cookie 文件。
首次使用需在 headful 模式下扫码登录保存 cookie，之后全自动运行。

用法:
    # 首次登录（headful 模式，手动扫码）
    python -m src.crawler.xianyu_async --login

    # 自动抓取（headless，使用已保存的 cookie）
    python -m src.crawler.xianyu_async --search
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger("rental.xianyu_async")

COOKIE_FILE = Path(__file__).parent / "xianyu_state.json"

# 下沙关键词矩阵：子区域 × 类型 + 核心小区/公寓
# ~165 关键词 × ~30 条/词 ≈ ~5000 raw → ~2000 去重 → ~1200 入库
_DISTRICTS = [
    "下沙", "金沙湖", "高沙", "文泽路", "文海南路", "下沙江滨",
    "云水", "大学城", "大学城北", "物美", "沿江", "下沙西",
    "七格", "元成", "新加坡科技园",
]
_RENT_TYPES = ["租房", "转租", "整租", "合租", "单间", "一室", "两室", "公寓"]
_TOP_COMPOUNDS = [
    "伊萨卡国际城", "世茂江滨花园", "保利东湾", "梦琴湾", "多蓝水岸",
    "野风海天城", "金沙学府", "清雅苑", "月雅苑", "景冉佳园",
    "阳光华城", "福雷德广场", "和达城", "宝龙城市广场", "盛泰名都",
    "观澜时代", "杭曜之城", "朗诗万科城", "北银公寓", "天元公寓",
    "新雁公寓", "东沙铭城", "学林铭城", "铭都雅苑", "德信早城",
    "碧桂园", "东郡国际", "四季风景苑", "文汇苑", "大都文苑",
    "云水苑", "高沙小区", "东岸嘉园", "香榭里花园", "金沙湖壹号",
    "龙湖滟澜星", "海天城", "头格月雅城", "中豪七格", "松合幸福里",
]

def _build_keywords() -> list[str]:
    """生成关键词矩阵：下沙子区域 × 类型 + 核心小区/公寓直搜"""
    kw = [
        "杭州下沙租房", "杭州下沙转租", "杭州下沙合租",
        "杭州下沙整租", "杭州下沙单间",
    ]
    for d in _DISTRICTS:
        for t in _RENT_TYPES:
            kw.append(f"{d} {t}")
    for c in _TOP_COMPOUNDS:
        kw.append(f"{c} 租房")
    return kw

SEARCH_KEYWORDS = _build_keywords()

# 并发抓取 tab 数（可通过环境变量调整，默认 5）
XIANYU_CONCURRENT_TABS = int(os.getenv("XIANYU_CONCURRENT_TABS", "5"))

ANTI_DETECT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
    window.chrome = { runtime: {} };
"""

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def has_valid_cookies() -> bool:
    """检查是否有已保存的 cookie 文件。"""
    return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 100


async def do_login():
    """Headful 模式：打开浏览器让用户扫码登录，保存 cookie。"""
    from playwright.async_api import async_playwright

    print("\n>> Opening browser for QR code login...")
    print("   Scan the QR code with your 闲鱼 APP, then press Enter.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DESKTOP_UA,
            locale="zh-CN",
        )
        await context.add_init_script(ANTI_DETECT_SCRIPT)

        page = await context.new_page()
        await page.goto("https://www.goofish.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        try:
            await page.click('text=登录', timeout=5000)
        except Exception:
            pass

        input("👉 扫码完成后按 Enter...")

        cookies = await context.cookies()
        state = {"cookies": cookies, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        COOKIE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print(f"[OK] 登录态已保存 ({len(cookies)} 个 cookie)")

        await browser.close()


def _extract_item_from_mtop(wrapper: dict, _mtop_keys_logged_ref: list[bool]) -> dict | None:
    """从 MTOP API 单条响应中提取结构化房源数据。

    抽取为独立函数，供单页捕获和翻页 fetch 共用，避免代码重复。
    _mtop_keys_logged_ref: [False] → 首次提取时 dump 字段名，帮助排查卖家统计字段。
    """
    item_data = wrapper.get("data", {}).get("item", {})
    main = item_data.get("main", {})
    ex = main.get("exContent", {})
    click = main.get("clickParam", {}).get("args", {})

    # 首次响应：dump 所有字段名帮助发现卖家统计字段
    if not _mtop_keys_logged_ref[0]:
        _mtop_keys_logged_ref[0] = True
        _wd = wrapper.get("data", {})
        logger.info(f"MTOP keys [wrapper]: {sorted(wrapper.keys())}")
        logger.info(f"MTOP keys [wrapper.data]: {sorted(_wd.keys())}")
        logger.info(f"MTOP keys [item_data]: {sorted(item_data.keys())}")
        logger.info(f"MTOP keys [main]: {sorted(main.keys())}")
        logger.info(f"MTOP keys [exContent]: {sorted(ex.keys())}")
        for _fn in ["userItemCount","sellerItemCount","userId","sellerId",
                     "memberId","userTotalItems","sellerTotal","publishCount",
                     "userStats","sellerStats","userInfo","sellerInfo"]:
            _v = wrapper.get(_fn) or _wd.get(_fn) or item_data.get(_fn) or main.get(_fn)
            if _v is not None:
                logger.info(f"MTOP debug: {_fn}={repr(_v)[:200]}")

    if not ex:
        return None

    title = (ex.get("title") or "").strip()
    if not title:
        ts = ex.get("titleSpan", {})
        title = (ts.get("content") or "").strip()

    price_str = click.get("displayPrice", "") or click.get("price", "")
    if not price_str:
        price_arr = ex.get("price", [])
        for p in price_arr:
            if p.get("type") == "integer":
                price_str = p.get("text", "")
                break

    location = (ex.get("want") or "").strip()
    area = (ex.get("area") or "").strip()
    api_address = "杭州"
    if area and area != location:
        api_address += area
    if location:
        api_address += location
    api_address = api_address.strip()

    pic_url = ex.get("picUrl") or ex.get("imgUrl") or ex.get("mainPic") or ""
    poster = (ex.get("userNickName") or "").strip()

    _user_id = (ex.get("userId") or main.get("userId") or main.get("sellerId")
                or item_data.get("userId") or item_data.get("sellerId") or "")
    if not _user_id:
        _user_id = ""

    _seller_item_count = None
    for _sf in ["userItemCount", "sellerItemCount", "publishCount", "totalItemCount"]:
        _sv = ex.get(_sf) or main.get(_sf) or item_data.get(_sf) or wrapper.get("data", {}).get(_sf)
        if _sv is not None and isinstance(_sv, (int, float)):
            _seller_item_count = int(_sv)
            break

    pub_ts = click.get("publishTime", "")
    pub_time = ""
    if pub_ts and pub_ts.isdigit():
        from datetime import datetime, timezone
        pub_time = datetime.fromtimestamp(
            int(pub_ts) / 1000, tz=timezone.utc
        ).isoformat()

    # 经纬度提取
    api_lng = None
    api_lat = None
    lng_raw = ex.get("longitude") or ex.get("lng") or click.get("longitude") or click.get("lng")
    lat_raw = ex.get("latitude") or ex.get("lat") or click.get("latitude") or click.get("lat")
    if lng_raw is not None and lat_raw is not None:
        try:
            api_lng, api_lat = float(lng_raw), float(lat_raw)
        except (ValueError, TypeError):
            pass
    if api_lng is None:
        loc_obj = ex.get("location") or {}
        if isinstance(loc_obj, dict):
            try:
                api_lng = float(loc_obj.get("lng") or loc_obj.get("longitude") or 0)
                api_lat = float(loc_obj.get("lat") or loc_obj.get("latitude") or 0)
                if api_lng == 0 and api_lat == 0:
                    api_lng = api_lat = None
            except (ValueError, TypeError):
                pass

    # 图片收集
    api_images = []
    _img_hit_fields = []
    if pic_url:
        api_images.append(pic_url)
        _img_hit_fields.append("picUrl/imgUrl/mainPic")
    for img_field in ["imgs", "images", "imageList", "imgList", "headPic", "picList", "pics",
                      "imageUrls", "imgUrls", "picUrls", "photoList", "thumbPics",
                      "itemImgs", "itemImages", "goodsImgs", "detailImgs",
                      "imageInfoList", "imgInfoList", "pictUrl", "sellerImgs"]:
        imgs = ex.get(img_field) or main.get(img_field) or []
        if isinstance(imgs, list):
            for img in imgs:
                if isinstance(img, str) and img not in api_images:
                    api_images.append(img)
                elif isinstance(img, dict):
                    for k in ("url", "picUrl", "imgUrl", "src", "imageUrl", "path"):
                        v = img.get(k, "")
                        if v and isinstance(v, str) and v not in api_images:
                            api_images.append(v)
            if imgs:
                _img_hit_fields.append(img_field)
        elif isinstance(imgs, str) and imgs not in api_images:
            api_images.append(imgs)
            _img_hit_fields.append(img_field)

    parts = [title]
    if price_str:
        parts.append(f"价格: {price_str}元/月")
    if location:
        parts.append(f"位置: {location}")
    if area:
        parts.append(f"区域: {area}")
    if poster:
        parts.append(f"发布者: {poster}")
    if api_images:
        parts.append(f"图片: {', '.join(api_images[:5])}")
    content = "\n".join(parts)

    item_id = str(ex.get("itemId", ""))

    return {
        "item_id": item_id,
        "url": f"https://www.goofish.com/item?id={item_id}",
        "content": content[:4000],
        "publish_time": pub_time,
        "poster_id": poster,
        "user_id": str(_user_id) if _user_id else "",
        "seller_item_count": _seller_item_count,
        "api_address": api_address,
        "api_images": api_images,
        "api_lng": api_lng,
        "api_lat": api_lat,
    }


async def crawl_xianyu_via_api(
    keywords: list[str],
    limit_per_keyword: int = 200,
    max_pages: int = 10,
    headless: bool = True,
) -> list[dict]:
    """使用 Playwright 拦截闲鱼 MTOP 搜索 API，获取结构化房源数据。

    与 crawl_xianyu_async 不同，本函数：
    - 拦截 h5api.m.goofish.com/mtop.taobao.idlemtopsearch.pc.search 响应
    - 直接从 JSON 解析 title/price/location/images/poster
    - 支持翻页，每页 30 条，最多翻 max_pages 页
    - 并行处理关键词（Semaphore 限流），默认 5 个 tab 并发
    - 返回格式与旧函数兼容：{"url", "source", "content"}

    Returns:
        [{"url": str, "source": "闲鱼", "content": str}, ...]
    """
    if not has_valid_cookies():
        logger.warning("闲鱼 cookie 未就绪，请先运行: python -m src.crawler.xianyu_async --login")
        return []

    if keywords is None:
        keywords = SEARCH_KEYWORDS

    from playwright.async_api import async_playwright

    state = json.loads(COOKIE_FILE.read_text())
    cookies = state.get("cookies", [])
    logger.info(
        f"Xianyu API: loaded {len(cookies)} cookies, "
        f"searching {len(keywords)} keywords (API mode, {XIANYU_CONCURRENT_TABS} concurrent)"
    )

    # 共享状态（并行 worker 通过 Lock 保护）
    all_items: list[dict] = []
    global_seen_ids: set[str] = set()
    seen_lock = asyncio.Lock()
    items_lock = asyncio.Lock()
    _mtop_keys_logged = [False]  # list 作为 mutable ref，跨 nested function 共享

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DESKTOP_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        await context.add_cookies(cookies)
        await context.add_init_script(ANTI_DETECT_SCRIPT)

        sem = asyncio.Semaphore(XIANYU_CONCURRENT_TABS)
        completed = 0
        completed_lock = asyncio.Lock()

        async def _search_one(kw_idx: int, keyword: str):
            """处理单个关键词：开新 page → 拦截 MTOP → 翻页 fetch → 提取。"""
            nonlocal all_items, completed
            async with sem:
                page = await context.new_page()
                try:
                    api_responses: list[list[dict]] = []
                    api_request_info = {}

                    def _capture_response(response):
                        url = response.url
                        if ('mtop.taobao.idlemtopsearch.pc.search' in url
                                and '.shade' not in url and '/1.0/' in url):
                            # 异步读取并追加
                            asyncio.ensure_future(_read_response(response))

                    async def _read_response(response):
                        try:
                            body = await response.text()
                            data = json.loads(body)
                            items = data.get("data", {}).get("resultList", [])
                            if items:
                                api_responses.append(items)
                        except Exception:
                            pass

                    def _capture_request(request):
                        url = request.url
                        if ('mtop.taobao.idlemtopsearch.pc.search' in url
                                and '.shade' not in url and '/1.0/' in url):
                            if not api_request_info:
                                api_request_info["url"] = url
                                api_request_info["post_data"] = request.post_data or ""

                    page.on("response", _capture_response)
                    page.on("request", _capture_request)

                    try:
                        try:
                            await page.goto(
                                f"https://www.goofish.com/search?q={keyword}",
                                wait_until="domcontentloaded",
                                timeout=15000,
                            )
                        except Exception:
                            logger.warning(f"  [{kw_idx+1}/{len(keywords)}] Page timeout: {keyword}")
                            return

                        # 等待 MTOP 响应到达
                        await page.wait_for_timeout(3000)

                        # ——— 翻页 fetch ———
                        if api_request_info:
                            from urllib.parse import unquote
                            post_data_raw = unquote(api_request_info.get("post_data", ""))
                            if post_data_raw.startswith("data="):
                                post_data_raw = post_data_raw[5:]

                            for pn in range(2, max_pages + 1):
                                try:
                                    new_body = post_data_raw.replace(
                                        '"pageNumber":1', f'"pageNumber":{pn}'
                                    ).replace(
                                        '"pageNumber%22%3A1', f'"pageNumber%22%3A{pn}'
                                    )
                                    # 合并 count 探测 + item 拉取为单次 fetch
                                    items_json = await page.evaluate("""
                                        async (args) => {
                                            try {
                                                const resp = await fetch(args.url, {
                                                    method: 'POST',
                                                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                                    body: 'data=' + encodeURIComponent(args.body),
                                                    credentials: 'include'
                                                });
                                                const text = await resp.text();
                                                const data = JSON.parse(text);
                                                const items = data?.data?.resultList || [];
                                                const hasNext = data?.data?.resultInfo?.hasNextPage;
                                                return {count: items.length, hasNext: !!hasNext, text: text};
                                            } catch(e) {
                                                return {count: 0, hasNext: false, text: '', error: e.message};
                                            }
                                        }
                                    """, {"url": api_request_info["url"], "body": new_body})
                                    if not items_json.get("count"):
                                        break
                                    resp_data = json.loads(items_json["text"])
                                    items = resp_data.get("data", {}).get("resultList", [])
                                    if items:
                                        api_responses.append(items)
                                    if not items_json.get("hasNext"):
                                        break
                                except Exception as e:
                                    logger.debug(f"  [{kw_idx+1}] Page {pn} failed: {e}")
                                    break
                    finally:
                        page.remove_listener("response", _capture_response)
                        page.remove_listener("request", _capture_request)

                    # ——— 解析提取 ———
                    keyword_items = []
                    for page_items in api_responses:
                        for wrapper in page_items:
                            if len(keyword_items) >= limit_per_keyword:
                                break
                            extracted = _extract_item_from_mtop(wrapper, _mtop_keys_logged)
                            if not extracted:
                                continue
                            item_id = extracted.pop("item_id")
                            if not item_id:
                                continue
                            async with seen_lock:
                                if item_id in global_seen_ids:
                                    continue
                                global_seen_ids.add(item_id)
                            extracted["source"] = "闲鱼"
                            keyword_items.append(extracted)
                        if len(keyword_items) >= limit_per_keyword:
                            break

                    async with items_lock:
                        all_items.extend(keyword_items)

                    async with completed_lock:
                        completed += 1
                        logger.info(
                            f"  [{completed}/{len(keywords)}] {keyword}: "
                            f"{len(api_responses)} pages, {len(keyword_items)} new "
                            f"(total: {len(all_items)})"
                        )
                finally:
                    await page.close()

        # 并行运行所有关键词
        results = await asyncio.gather(
            *[_search_one(i, kw) for i, kw in enumerate(keywords)],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Keyword worker exception: {r}")

        await browser.close()

    logger.info(f"Xianyu API done: {len(all_items)} items from {len(keywords)} keywords")
    items_with_images = sum(1 for it in all_items if it.get("api_images"))
    logger.info(f"Xianyu image hit rate: {items_with_images}/{len(all_items)} "
                f"({items_with_images*100//max(len(all_items),1)}%)")
    return all_items


async def crawl_xianyu_async(
    keywords: list[str] | None = None,
    limit_per_keyword: int = 80,
    headless: bool = True,
) -> list[dict]:
    """多关键词搜索闲鱼 → 详情页抓取 → 返回结构化列表。

    优先使用 API 拦截模式（crawl_xianyu_via_api），
    失败时回退到 DOM 卡片提取模式。

    Returns:
        [{"url": str, "source": "闲鱼", "content": str}, ...]
    """
    # 使用 API 拦截模式获取结构化数据
    return await crawl_xianyu_via_api(
        keywords=keywords,
        limit_per_keyword=limit_per_keyword,
        max_pages=max(1, limit_per_keyword // 30),
        headless=headless,
    )


async def check_posters_for_agents(
    poster_ids: list[str] | None = None,
    headless: bool = True,
) -> dict:
    """独立的中介检测任务：访问发帖人闲鱼主页，数出租房。

    对每个 poster：打开其一条帖子 → 找到 /personal?userId= 链接 →
    进主页 → 数出租房（textContent 匹配关键词）→ ≥3 套则标为中介。

    Args:
        poster_ids: 指定要检查的 poster。None 则自动查 DB 中 <3 条的全部 poster。
        headless: Playwright 是否 headless 模式。

    Returns:
        {"found": int, "suspected": int, "personal": int, "checked": int, "total": int, "error": str | None}
    """
    from src.db.schema import get_conn as _get_conn

    if not has_valid_cookies():
        return {"found": 0, "suspected": 0, "personal": 0, "checked": 0, "total": 0, "error": "Cookies not ready — 请先执行 python -m src.crawler.xianyu_async --login"}

    # 解析要检查的 poster 列表
    _to_check: list[tuple[str, str]] = []  # [(poster_id, sample_url), ...]
    _conn = _get_conn()
    try:
        if poster_ids:
            for pid in poster_ids:
                row = _conn.execute(
                    "SELECT source_url FROM listings WHERE poster_id=? LIMIT 1",
                    (pid,),
                ).fetchone()
                if row:
                    _to_check.append((pid, row[0]))
        else:
            rows = _conn.execute("""
                SELECT poster_id, MIN(source_url) as sample_url
                FROM listings
                WHERE poster_id IS NOT NULL AND poster_id != '' AND poster_id != 'None'
                  AND (poster_checked_at IS NULL OR poster_checked_at < datetime('now', '-24 hours'))
                GROUP BY poster_id
                HAVING COUNT(DISTINCT source_url) < 3
                ORDER BY RANDOM()
            """).fetchall()
            for r in rows:
                _to_check.append((r[0], r[1]))

            # 追加：没有 poster_id 的闲鱼房源，直接用 source_url 去页面提取
            _rows_no_poster = _conn.execute("""
                SELECT source_url
                FROM listings
                WHERE (poster_id IS NULL OR poster_id = '' OR poster_id = 'None')
                  AND source_platform = '闲鱼'
                  AND (poster_checked_at IS NULL OR poster_checked_at < datetime('now', '-24 hours'))
            """).fetchall()
            for r in _rows_no_poster:
                _to_check.append(('', r[0]))  # poster_id 留空，从页面提取
    finally:
        _conn.close()

    if not _to_check:
        return {"found": 0, "suspected": 0, "personal": 0, "checked": 0, "total": 0, "error": None}

    # 加载 cookie
    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8")).get("cookies", [])
    except Exception:
        return {"found": 0, "suspected": 0, "personal": 0, "checked": 0, "total": 0, "error": "Cookie 文件损坏"}

    from playwright.async_api import async_playwright
    found = 0
    suspected = 0
    personal = 0
    checked = 0
    total = len(_to_check)
    db_lock = asyncio.Lock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DESKTOP_UA,
            locale="zh-CN",
        )
        await context.add_cookies(cookies)
        await context.add_init_script(ANTI_DETECT_SCRIPT)

        sem = asyncio.Semaphore(5)

        async def _check_one(_poster, _item_url):
            nonlocal found, suspected, personal, checked
            page = await context.new_page()
            try:
                # Step 1: 获取 profile URL
                # 已知数字 poster_id 时直接拼 URL，昵称型则从物品页提取
                if _poster and _poster.isdigit():
                    _profile_url = f"https://www.goofish.com/personal?userId={_poster}"
                else:
                    # 无 poster_id 或昵称型：打开帖子页，从 DOM 里找 profile 链接
                    await page.goto(_item_url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(1500)

                    _profile_url = await page.evaluate("""() => {
                        const links = document.querySelectorAll('a[href*="/personal?userId="]');
                        return links.length > 0 ? links[0].href : '';
                    }""")

                    # 从 profile URL 提取数字 userId
                    if _profile_url:
                        import re as _re
                        _m = _re.search(r'userId=([^&]+)', _profile_url)
                        if _m:
                            _new_id = _m.group(1)
                            if _new_id and _new_id != _poster:
                                _conn_patch = _get_conn()
                                try:
                                    _conn_patch.execute(
                                        "UPDATE listings SET poster_id=? WHERE poster_id=?",
                                        (_new_id, _poster),
                                    )
                                    _conn_patch.commit()
                                finally:
                                    _conn_patch.close()
                                _poster = _new_id
                        elif not _poster:
                            _poster = _m.group(1) if _m else ""

                if not _profile_url:
                    async with db_lock:
                        checked += 1
                        _conn_noprofile = _get_conn()
                        try:
                            if _poster:
                                _conn_noprofile.execute(
                                    "UPDATE listings SET poster_checked_at=datetime('now') WHERE poster_id=?",
                                    (_poster,),
                                )
                            else:
                                _conn_noprofile.execute(
                                    "UPDATE listings SET poster_checked_at=datetime('now') WHERE source_url=?",
                                    (_item_url,),
                                )
                            _conn_noprofile.commit()
                        finally:
                            _conn_noprofile.close()
                    return

                # Step 2: 进卖家主页
                await page.goto(_profile_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1500)

                # Step 3: 数出租房和总物品数
                _counts = await page.evaluate("""() => {
                    const links = document.querySelectorAll('a[href*="/item?id="]');
                    const seen = new Set();
                    const rentalKW = /\\/月|出租|租房|整租|合租|转租|单间|一室|两室|三室|1室|2室|3室|一居|两居|三居|㎡|平方|无中介|拎包|房东|民水|民电/;
                    let rentalCount = 0;
                    let totalCount = 0;
                    for (const a of links) {
                        const id = a.href.split('id=')[1]?.split('&')[0];
                        if (!id || seen.has(id)) continue;
                        seen.add(id);
                        totalCount++;
                        if (rentalKW.test(a.textContent || '')) rentalCount++;
                    }
                    return { rental: rentalCount, total: totalCount };
                }""")
                _rental_count = _counts.get("rental", 0)
                _total_count = _counts.get("total", 0)

                async with db_lock:
                    checked += 1
                    # 确定 poster 检查结果
                    if _rental_count >= 3:
                        poster_result = "中介"
                    elif _rental_count >= 2:
                        poster_result = "疑似中介"
                    elif _rental_count == 1:
                        poster_result = "疑似中介" if _total_count == 1 else "个人"
                    else:
                        # 无出租房 → 标记已检查，不再重试
                        _conn_empty = _get_conn()
                        try:
                            _conn_empty.execute(
                                "UPDATE listings SET poster_checked_at=datetime('now') WHERE poster_id=?",
                                (_poster,),
                            )
                            _conn_empty.commit()
                        finally:
                            _conn_empty.close()
                        return

                    # 读取 pipeline 判定（detect_with_llm 的初始结果）
                    _conn2 = _get_conn()
                    try:
                        row = _conn2.execute(
                            "SELECT landlord_type FROM listings WHERE poster_id=? LIMIT 1",
                            (_poster,),
                        ).fetchone()
                        pipeline_result = row[0] if row else "疑似中介"

                        # 融合规则：Poster 决定性 + Pipeline 辅助
                        if poster_result == "中介":
                            final = "中介"
                        elif poster_result == "疑似中介" and pipeline_result == "中介":
                            final = "中介"       # pipeline 升级
                        elif poster_result == "个人" and pipeline_result == "中介":
                            final = "疑似中介"    # pipeline 拉低信任
                        else:
                            final = poster_result

                        if final != pipeline_result:
                            _conn2.execute(
                                "UPDATE listings SET landlord_type=? WHERE poster_id=? AND landlord_type!=?",
                                (final, _poster, final),
                            )
                            _conn2.commit()

                        # 记录检查时间。"个人"缓存 24h，"疑似中介"缓存 6h
                        _cache_hours = 24 if final == "个人" else 6
                        _conn2.execute(
                            f"UPDATE listings SET poster_checked_at=datetime('now', '-{24 - _cache_hours} hours') WHERE poster_id=?",
                            (_poster,),
                        )
                        _conn2.commit()

                        if final == "中介":
                            found += 1
                        elif final == "疑似中介":
                            suspected += 1
                        else:
                            personal += 1

                        logger.info(
                            f"Poster check [{_poster}]: poster={poster_result} pipeline={pipeline_result} → {final}"
                        )
                    finally:
                        _conn2.close()
            except Exception:
                async with db_lock:
                    checked += 1
            finally:
                await page.close()

        async def _worker(item):
            async with sem:
                await _check_one(item[0], item[1])

        await asyncio.gather(*[_worker(item) for item in _to_check])

        await browser.close()

    return {"found": found, "suspected": suspected, "personal": personal, "checked": checked, "total": total, "error": None}


async def _enrich_via_new_tabs(items: list[dict], context, max_concurrent: int = 5):
    """用新标签页并行加载详情页，补齐卡片文本较短的商品数据。"""
    if not items:
        return
    sem = asyncio.Semaphore(max_concurrent)

    async def _fetch_one(item):
        async with sem:
            page = await context.new_page()
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                text = await page.inner_text("body")
                if text and len(text) > 200:
                    item["content"] = text[:4000]
            except Exception:
                pass
            finally:
                await page.close()

    await asyncio.gather(*[_fetch_one(item) for item in items])


# ——— CLI ———
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="闲鱼 Async Playwright 抓取器")
    parser.add_argument("--login", action="store_true", help="扫码登录并保存 cookie")
    parser.add_argument("--search", action="store_true", help="自动搜索抓取")
    parser.add_argument("--keywords", type=str, nargs="*", help="自定义搜索关键词")
    parser.add_argument("--limit", type=int, default=80, help="每个关键词最大商品数")
    parser.add_argument("--headed", action="store_true", help="可见浏览器模式（调试用）")
    parser.add_argument("--no-headless", action="store_true", help=argparse.SUPPRESS)  # 已废弃，使用 --headed
    parser.add_argument("--output", type=str, help="输出 JSON 文件")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.login:
        asyncio.run(do_login())
    elif args.search:
        keywords = args.keywords if args.keywords else SEARCH_KEYWORDS

        async def _run():
            results = await crawl_xianyu_async(
                keywords=keywords,
                limit_per_keyword=args.limit,
                headless=not (args.headed or args.no_headless),
            )
            output = json.dumps(results, ensure_ascii=False, indent=2)
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(output)
                print(f"Saved {len(results)} items to {args.output}", file=sys.stderr)
            else:
                print(output)

        asyncio.run(_run())
    else:
        parser.print_help()
