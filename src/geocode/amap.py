"""高德地图 API 封装 — 地址 → 经纬度"""

import os
import time
from src.http_client import get_http_client

AMAP_KEY = os.getenv("AMAP_API_KEY", "")
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"

# —— geocode inputtips 兜底每日上限（免费额度 5000/日，留给前端自动补全用） ——
_tips_call_count = 0
_tips_call_date = ""
_TIPS_DAILY_MAX = 30  # geocode 兜底每天最多 30 次（月配额仅 5000）


async def geocode(address: str, city: str = "杭州") -> tuple[float, float] | None:
    """将地址转为经纬度，失败返回 None。自动尝试加前缀重试 + Input Tips 兜底。"""
    if not AMAP_KEY:
        raise RuntimeError("AMAP_API_KEY not set in .env")

    client = get_http_client()

    # —— 策略 1: 直接 geocode，多种前缀变体重试 ——
    candidates: list[str] = []
    seen: set[str] = set()

    def _add_candidate(addr: str):
        if addr not in seen:
            seen.add(addr)
            candidates.append(addr)

    _add_candidate(address)
    # 去掉"浙江"/"杭州"前缀后重试（有时不带前缀反而能命中）
    stripped = address
    for prefix in ["浙江", "杭州"]:
        if stripped.startswith(prefix):
            s = stripped[len(prefix):].strip()
            if s:
                _add_candidate(s)
    if "杭州" not in address:
        _add_candidate(f"杭州{address}")
        # 仅对地址中含有的区名追加"杭州+区名"前缀，避免遍历全部 9 个区
        for district in ["下沙", "钱塘", "滨江", "萧山", "西湖", "拱墅", "上城", "余杭", "临平"]:
            if district in address:
                # 防止"杭州下沙下沙..."重复前缀
                if not address.startswith(district):
                    _add_candidate(f"杭州{district}{address}")
                break  # 一个区名命中即可，不混合多个区

    for addr in candidates:
        resp = await client.get(GEOCODE_URL, params={
            "key": AMAP_KEY,
            "address": addr,
            "city": city,
        })
        data = resp.json()
        if data.get("status") == "1" and data.get("geocodes"):
            loc = data["geocodes"][0]["location"]
            lng_str, lat_str = loc.split(",")
            return float(lng_str), float(lat_str)

    # —— 策略 2: Input Tips API 兜底（每日上限保护） ——
    # 用短关键词查 Input Tips，拿到完整地址或直接获得坐标
    today = time.strftime("%Y-%m-%d")
    global _tips_call_count, _tips_call_date
    if _tips_call_date != today:
        _tips_call_count = 0
        _tips_call_date = today
    if _tips_call_count >= _TIPS_DAILY_MAX:
        return None  # 达到每日上限，跳过 inputtips 兜底

    short = address
    for prefix in ["浙江", "浙江省", "杭州"]:
        if short.startswith(prefix):
            short = short[len(prefix):].strip()
    if not short or len(short) < 2:
        return None

    try:
        tips_resp = await client.get("https://restapi.amap.com/v3/assistant/inputtips", params={
            "key": AMAP_KEY,
            "keywords": short,
            "city": city,
            "citylimit": "true",
        })
        _tips_call_count += 1  # 计入每日配额
        tips_data = tips_resp.json()
        if tips_data.get("status") == "1":
            for tip in tips_data.get("tips", [])[:3]:
                # 优先用直接返回的坐标
                loc = tip.get("location")
                if loc and "," in str(loc):
                    try:
                        lng_str, lat_str = str(loc).split(",")
                        return float(lng_str), float(lat_str)
                    except (ValueError, TypeError):
                        pass
                # 用 Input Tips 返回的正式名称再 geocode 一次
                formal_name = tip.get("name", "")
                if formal_name and formal_name != short:
                    try:
                        resp2 = await client.get(GEOCODE_URL, params={
                            "key": AMAP_KEY,
                            "address": formal_name,
                            "city": city,
                        })
                        data2 = resp2.json()
                        if data2.get("status") == "1" and data2.get("geocodes"):
                            loc = data2["geocodes"][0]["location"]
                            lng_str, lat_str = loc.split(",")
                            return float(lng_str), float(lat_str)
                    except Exception:
                        continue
    except Exception:
        pass

    return None


async def batch_geocode_missing() -> int:
    """批量 geocode DB 中有地址但无坐标的房源。返回新增坐标数。

    先按地址去重，每个不同地址只 geocode 一次，结果批量更新所有同地址房源。
    降低并发避免高德 API 限流。共享一个写入连接而非每次创建/销毁。
    """
    import asyncio
    import logging
    from src.db.schema import get_conn

    log = logging.getLogger("rental.geocode")
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, address FROM listings WHERE lng IS NULL AND address IS NOT NULL AND address != ''"
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    # 去重：相同地址只需 geocode 一次
    addr_map: dict[str, list[int]] = {}
    for row_id, addr in rows:
        addr_map.setdefault(addr, []).append(row_id)

    log.info(f"batch_geocode: {len(rows)} listings, {len(addr_map)} distinct addresses to geocode...")

    sem = asyncio.Semaphore(2)  # 极低并发 + 串行间隔，避免 Input Tips 额外请求触发限流
    updated = [0]
    failed = [0]
    lock = asyncio.Lock()
    write_lock = asyncio.Lock()  # 序列化 SQLite 写入
    delay_lock = asyncio.Lock()
    last_call = [0.0]
    # 共享写入连接，批量积累后统一写入
    write_conn = get_conn()
    try:
        async def _geocode_addr(addr: str, ids: list[int]):
            # 串行间隔：确保两次 geocode 调用之间至少间隔 300ms
            async with delay_lock:
                import time as _time
                elapsed = _time.monotonic() - last_call[0]
                if elapsed < 0.3:
                    await asyncio.sleep(0.3 - elapsed)
                last_call[0] = _time.monotonic()
            async with sem:
                for attempt in range(3):
                    try:
                        coords = await geocode(addr)
                        if coords:
                            async with write_lock:
                                write_conn.executemany(
                                    "UPDATE listings SET lng=?, lat=? WHERE id=?",
                                    [(coords[0], coords[1], rid) for rid in ids],
                                )
                                write_conn.commit()
                            async with lock:
                                updated[0] += len(ids)
                            return
                        else:
                            # geocode 返回 None（非限流，地址确实无法解析）
                            break
                    except Exception:
                        if attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))  # 指数退避
                        else:
                            async with lock:
                                failed[0] += len(ids)

        await asyncio.gather(*[_geocode_addr(addr, ids) for addr, ids in addr_map.items()])
    finally:
        write_conn.close()
    log.info(f"batch_geocode: {updated[0]} updated, {failed[0]} failed")
    return updated[0]


async def reverse_geocode(lng: float, lat: float) -> str | None:
    """经纬度 → 地址描述。"""
    if not AMAP_KEY:
        raise RuntimeError("AMAP_API_KEY not set in .env")

    client = get_http_client()
    resp = await client.get("https://restapi.amap.com/v3/geocode/regeo", params={
        "key": AMAP_KEY,
        "location": f"{lng},{lat}",
    })
    data = resp.json()

    if data.get("status") != "1":
        return None
    return data["regeocode"]["formatted_address"]
