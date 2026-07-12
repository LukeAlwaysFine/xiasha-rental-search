"""SQLite schema and basic CRUD for rental listings."""

import sqlite3
import json
from datetime import datetime, timezone, timedelta

import os

DB_PATH = os.getenv("DB_PATH", "rental_data.db")


def init_db(path: str = None) -> sqlite3.Connection:
    global DB_PATH
    if path is None:
        path = DB_PATH
    else:
        DB_PATH = path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL,
            house_type TEXT,           -- 户型: 开间/一居/两居...
            address TEXT,
            lng REAL,                  -- 高德经度
            lat REAL,                  -- 高德纬度
            area REAL,                 -- 面积 ㎡
            rent_type TEXT,            -- 整租/合租/单间/转租
            landlord_type TEXT,        -- 个人/中介/未知
            images TEXT,               -- JSON array of URLs
            source_url TEXT UNIQUE,    -- 原文链接，用于去重
            source_platform TEXT,      -- 豆瓣/闲鱼
            contact TEXT,              -- 联系方式(原文)
            poster_id TEXT,            -- 发帖人ID，用于中介检测
            publish_time TEXT,         -- ISO 8601
            fetched_at TEXT DEFAULT (datetime('now')),
            is_sublet INTEGER DEFAULT 0  -- 是否转租
        );

        CREATE INDEX IF NOT EXISTS idx_listings_lng_lat ON listings(lng, lat);
        CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
        CREATE INDEX IF NOT EXISTS idx_listings_publish_time ON listings(publish_time);
        CREATE INDEX IF NOT EXISTS idx_listings_poster ON listings(poster_id);
        CREATE INDEX IF NOT EXISTS idx_listings_contact ON listings(contact);

        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_query TEXT NOT NULL,
            result_count INTEGER,
            clicked_listing_ids TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_search_log_query ON search_log(search_query);
        CREATE INDEX IF NOT EXISTS idx_search_log_timestamp ON search_log(timestamp);

        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_favorites_listing ON favorites(listing_id);
        CREATE INDEX IF NOT EXISTS idx_favorites_created ON favorites(created_at);
    """)
    # 迁移：添加 LLM 评分列（兼容旧数据库）
    for col, col_type in [("_llm_score", "REAL"), ("_llm_reason", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    return conn


def log_search(conn: sqlite3.Connection, query: str, result_count: int):
    """记录搜索行为到 search_log 表（用于热门搜索和排序优化）。"""
    conn.execute(
        "INSERT INTO search_log (search_query, result_count) VALUES (?, ?)",
        (query, result_count),
    )
    conn.commit()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_listing(conn: sqlite3.Connection, data: dict) -> int | None:
    """Insert or update a listing (dedup by source_url). Returns row id or None.

    对已存在的记录（同 source_url）：
    - 使用 COALESCE 渐进式填充缺失字段（坐标、图片、户型、面积等）
    - landlord_type: 从"未知"升级到"个人"/"中介"，但中介不会被降级为个人
    - publish_time: 回填 NULL 值

    注意：此函数不调用 conn.commit()。调用方应在批量操作后统一 commit。
    """
    try:
        conn.execute("""
            INSERT INTO listings
                (title, price, house_type, address, lng, lat, area, rent_type,
                 landlord_type, images, source_url, source_platform, contact,
                 poster_id, publish_time, is_sublet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                lng = CASE WHEN listings.lng IS NULL OR listings.lng = 0.0 THEN excluded.lng ELSE listings.lng END,
                lat = CASE WHEN listings.lat IS NULL OR listings.lat = 0.0 THEN excluded.lat ELSE listings.lat END,
                area = CASE WHEN listings.area IS NULL OR listings.area = 0 THEN excluded.area ELSE listings.area END,
                house_type = COALESCE(listings.house_type, excluded.house_type),
                rent_type = COALESCE(listings.rent_type, excluded.rent_type),
                price = CASE WHEN listings.price IS NULL OR listings.price = 0 THEN excluded.price ELSE listings.price END,
                images = CASE
                    WHEN listings.images IS NULL OR listings.images = '[]'
                    THEN excluded.images ELSE listings.images
                END,
                landlord_type = CASE
                    WHEN listings.landlord_type = '未知' AND excluded.landlord_type != '未知'
                    THEN excluded.landlord_type
                    ELSE listings.landlord_type
                END,
                publish_time = CASE
                    WHEN listings.publish_time IS NULL THEN excluded.publish_time
                    ELSE listings.publish_time
                END
        """, (
            data.get("title"),
            data.get("price"),
            data.get("house_type"),
            data.get("address"),
            data.get("lng"),
            data.get("lat"),
            data.get("area"),
            data.get("rent_type"),
            data.get("landlord_type", "未知"),
            json.dumps(data.get("images", []), ensure_ascii=False),
            data["source_url"],
            data.get("source_platform"),
            data.get("contact"),
            data.get("poster_id"),
            data.get("publish_time"),
            1 if data.get("is_sublet") else 0,
        ))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.IntegrityError:
        return None


def _build_query(filters: dict, keyword_mode: str = "exact") -> tuple[str, list, str, str]:
    """Build WHERE clause, params, ORDER BY, and distance SELECT expression.
    keyword_mode: 'exact' | 'loose' | 'none'.
    Returns (where_clause, params, order, distance_expr)."""
    where = ["1=1"]
    params = []

    keyword = filters.get("keyword", "").strip()
    if keyword_mode == "exact" and keyword:
        where.append("(title LIKE ? OR address LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw])
    elif keyword_mode == "loose" and keyword:
        # 把关键词拆成单字，任意匹配即可（地址或标题）
        chars = [c for c in keyword if c.strip() and c not in ('的','了','在','是','我','有','和','就','不','人','都','一','个','上','也','很','到','说','要','去','你','会','着','没','看','好','这','他','她','它')]
        if chars:
            clauses = []
            for ch in chars[:8]:  # 最多取前8个字
                clauses.append("(title LIKE ? OR address LIKE ?)")
                params.extend([f"%{ch}%", f"%{ch}%"])
            where.append(f"({' OR '.join(clauses)})")

    if filters.get("price_min") is not None:
        where.append("price >= ?")
        params.append(filters["price_min"])
    if filters.get("price_max") is not None:
        where.append("price <= ?")
        params.append(filters["price_max"])

    if filters.get("house_types"):
        placeholders = ",".join("?" * len(filters["house_types"]))
        where.append(f"house_type IN ({placeholders})")
        params.extend(filters["house_types"])

    if filters.get("rent_types"):
        placeholders = ",".join("?" * len(filters["rent_types"]))
        where.append(f"rent_type IN ({placeholders})")
        params.extend(filters["rent_types"])

    if filters.get("landlord_types"):
        placeholders = ",".join("?" * len(filters["landlord_types"]))
        where.append(f"landlord_type IN ({placeholders})")
        params.extend(filters["landlord_types"])

    if filters.get("source_platforms"):
        placeholders = ",".join("?" * len(filters["source_platforms"]))
        where.append(f"source_platform IN ({placeholders})")
        params.extend(filters["source_platforms"])

    if filters.get("hours_ago"):
        since = (datetime.now(timezone.utc) - timedelta(hours=filters["hours_ago"])).isoformat()
        where.append("publish_time >= ?")
        params.append(since)

    if filters.get("favorites_only"):
        where.append("id IN (SELECT listing_id FROM favorites)")

    # 排序
    order = "publish_time DESC"
    sort = filters.get("sort", "comprehensive")
    if sort == "newest":
        order = "publish_time DESC"
    elif sort == "price_asc":
        order = "price ASC"
    elif sort == "price_desc":
        order = "price DESC"
    elif sort == "distance" and filters.get("ref_lng") and filters.get("ref_lat"):
        import math
        ref_lng, ref_lat = filters["ref_lng"], filters["ref_lat"]
        # 防御 NaN/Infinity 注入：FastAPI float 类型接受 NaN/Infinity，
        # 直接拼入 SQL 会导致语法错误 → HTTP 500
        if not (math.isfinite(ref_lng) and math.isfinite(ref_lat)):
            order = "publish_time DESC"  # 回退到默认排序
        else:
            # 浮点数已验证为 finite，安全插值
            # NULL 坐标排到最后：无坐标房源用户看不到距离，不应混入排序结果
            order = f"CASE WHEN lng IS NULL OR lat IS NULL THEN 1 ELSE 0 END, ((lng - {ref_lng})*(lng - {ref_lng}) + (lat - {ref_lat})*(lat - {ref_lat})) ASC"
    elif sort == "comprehensive":
        order = """
            CASE WHEN _llm_score IS NOT NULL THEN _llm_score ELSE 0 END DESC,
            CASE WHEN images IS NOT NULL AND images != '[]' AND images != '' THEN 10 ELSE 0 END +
            CASE WHEN area IS NULL THEN 0 ELSE 3 END +
            CASE WHEN house_type IS NULL THEN 0 ELSE 3 END +
            CASE WHEN landlord_type = '个人' THEN 5 ELSE 0 END -
            CASE WHEN publish_time < datetime('now', '-7 days') THEN 10 ELSE 0 END
            DESC, publish_time DESC
        """

    # 距离计算列（仅在通勤模式下注入 SELECT）
    distance_expr = ""
    if filters.get("ref_lng") is not None and filters.get("ref_lat") is not None:
        import math
        rlng, rlat = filters["ref_lng"], filters["ref_lat"]
        if math.isfinite(rlng) and math.isfinite(rlat):
            # equirectangular 近似，<10km 误差 <1%
            distance_expr = (
                f"ROUND(SQRT(POW((lng - {rlng}) * 111320 * COS({rlat} * 3.14159265 / 180), 2) + "
                f"POW((lat - {rlat}) * 110540, 2))) AS distance"
            )

    where_clause = " AND ".join(where)
    return where_clause, params, order, distance_expr


def search_listings(conn: sqlite3.Connection, filters: dict) -> list[dict]:
    """搜索房源，支持三级降级（结果不足时自动放宽）：
    1. 精确匹配关键词 → 2. 宽松单字匹配 → 3. 去掉关键词显示全部
    每级结果 ≥ MIN_DEGRADE 才停止，否则继续降级并合并去重。

    注意：内部使用 OFFSET 0 获取各级结果，去重后再应用用户的 offset/limit。
    这避免了各级使用不同 offset 导致的数据错位问题。
    """
    MIN_DEGRADE = 20  # 少于此数则降级到下一级
    limit = min(filters.get("limit", 50), 1000)
    keyword = filters.get("keyword", "").strip()
    offset_val = filters.get("offset", 0)

    # 为支持分页，多取一些行（offset + limit）以在去重后切片
    fetch_limit = offset_val + limit + 20  # +20 buffer for dedup loss

    results: list[dict] = []
    seen_ids: set[int] = set()

    def _add_rows(rows):
        for row in rows:
            d = row_to_dict(row)
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                results.append(d)

    # 第一轮: 精确匹配（无关键词时直接跳到 none）
    # 内部使用 OFFSET 0，避免各级 offset 错位
    if keyword:
        where_clause, params, order, dist_expr = _build_query(filters, "exact")
        select_cols = f"*, {dist_expr}" if dist_expr else "*"
        query = f"SELECT {select_cols} FROM listings WHERE {where_clause} ORDER BY {order} LIMIT ?"
        rows = conn.execute(query, params + [fetch_limit]).fetchall()
        _add_rows(rows)
        if len(results) >= MIN_DEGRADE and len(results) > offset_val:
            return results[offset_val:offset_val + limit]

    # 降级到宽松匹配（单字 OR）
    if keyword:
        where_clause, params, order, dist_expr = _build_query(filters, "loose")
        select_cols = f"*, {dist_expr}" if dist_expr else "*"
        query = f"SELECT {select_cols} FROM listings WHERE {where_clause} ORDER BY {order} LIMIT ?"
        rows = conn.execute(query, params + [fetch_limit]).fetchall()
        _add_rows(rows)
        if len(results) >= MIN_DEGRADE and len(results) > offset_val:
            return results[offset_val:offset_val + limit]

    # 降级到无关键词（保留其他筛选条件）
    where_clause, params, order, dist_expr = _build_query(filters, "none")
    select_cols = f"*, {dist_expr}" if dist_expr else "*"
    query = f"SELECT {select_cols} FROM listings WHERE {where_clause} ORDER BY {order} LIMIT ?"
    rows = conn.execute(query, params + [fetch_limit]).fetchall()
    _add_rows(rows)
    return results[offset_val:offset_val + limit]


def count_listings(conn: sqlite3.Connection, filters: dict) -> int:
    """统计符合条件的房源总数。

    与 search_listings 三级降级对齐：精确 → 单字宽松 → 无关键词。
    每级结果 ≥ MIN_DEGRADE 才停止，否则继续降级。
    """
    keyword = filters.get("keyword", "").strip()
    MIN_DEGRADE = 20  # 与 search_listings 保持一致

    def _try_count(mode: str) -> int:
        where_clause, params, _, _ = _build_query(filters, mode)
        query = f"SELECT COUNT(*) FROM listings WHERE {where_clause}"
        row = conn.execute(query, params).fetchone()
        return row[0] if row else 0

    if keyword:
        cnt = _try_count("exact")
        if cnt >= MIN_DEGRADE:
            return cnt
        cnt = _try_count("loose")
        if cnt >= MIN_DEGRADE:
            return cnt
        return _try_count("none")
    return _try_count("none")


def count_by_poster(conn: sqlite3.Connection, poster_id: str) -> int:
    """Count distinct listings by a poster (for agent detection)."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT source_url) FROM listings WHERE poster_id = ?", (poster_id,)
    ).fetchone()
    return row[0] if row else 0


def count_by_contact(conn: sqlite3.Connection, contact: str) -> int:
    """Count listings sharing the same contact."""
    if not contact:
        return 0
    row = conn.execute(
        "SELECT COUNT(DISTINCT source_url) FROM listings WHERE contact = ?", (contact,)
    ).fetchone()
    return row[0] if row else 0


# ——— 收藏 ———

def add_favorite(conn: sqlite3.Connection, listing_id: int) -> bool:
    """添加收藏。返回 True 表示新增，False 表示已存在。"""
    try:
        conn.execute(
            "INSERT INTO favorites (listing_id) VALUES (?)",
            (listing_id,),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_favorite(conn: sqlite3.Connection, listing_id: int) -> bool:
    """取消收藏。返回 True 表示删除成功，False 表示不存在。"""
    cur = conn.execute(
        "DELETE FROM favorites WHERE listing_id = ?",
        (listing_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def get_favorite_ids(conn: sqlite3.Connection) -> list[int]:
    """获取所有收藏的 listing_id 列表。"""
    rows = conn.execute(
        "SELECT listing_id FROM favorites ORDER BY created_at DESC"
    ).fetchall()
    return [row[0] for row in rows]


def get_favorites(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """获取收藏房源完整数据。"""
    rows = conn.execute(
        """SELECT l.* FROM listings l
           INNER JOIN favorites f ON l.id = f.listing_id
           ORDER BY f.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def is_favorited(conn: sqlite3.Connection, listing_id: int) -> bool:
    """检查某房源是否已收藏。"""
    row = conn.execute(
        "SELECT 1 FROM favorites WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    return row is not None


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("images"):
        try:
            d["images"] = json.loads(d["images"])
            # 防止双重编码: 如果解析结果仍是 string 则再解析一层
            if isinstance(d["images"], str):
                d["images"] = json.loads(d["images"])
        except (json.JSONDecodeError, TypeError):
            d["images"] = []
    else:
        d["images"] = []  # H5: 显式处理 NULL/空，防止前端 TypeError
    return d
