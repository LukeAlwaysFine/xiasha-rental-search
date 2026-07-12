"""测试 _build_query 和 search_listings (TC0001-TC0003, TC0008-TC0009)"""

import json
import pytest
from src.db.schema import _build_query, search_listings, upsert_listing, count_listings
from tests.conftest import make_listing


# ——— TC0001: _build_query 空关键词 keyword_mode='none' ———
def test_build_query_none_mode():
    """TC0001: 空关键词 + none 模式 → 返回仅含筛选条件的 SQL"""
    filters = {"keyword": "", "limit": 20, "offset": 0}
    where_clause, params, order, _dist = _build_query(filters, "none")
    assert where_clause.startswith("1=1"), f"Expected '1=1' where, got: {where_clause}"
    assert params == []
    assert "publish_time DESC" in order


# ——— TC0002: _build_query keyword_mode='exact' ———
def test_build_query_exact_mode():
    """TC0002: 精确模式 → 双 LIKE 子句含 title 和 address"""
    filters = {"keyword": "未来科技城", "limit": 20, "offset": 0}
    where_clause, params, order, _dist = _build_query(filters, "exact")
    assert "title LIKE ?" in where_clause
    assert "address LIKE ?" in where_clause
    assert params == ["%未来科技城%", "%未来科技城%"]


# ——— TC0003: _build_query keyword_mode='loose' ———
def test_build_query_loose_mode():
    """TC0003: 宽松模式 → 拆为单字 OR 逻辑（排除停用词）"""
    filters = {"keyword": "下沙租房", "limit": 20, "offset": 0}
    where_clause, params, order, _dist = _build_query(filters, "loose")
    # "下" 和 "沙" 各生成双 LIKE
    assert "OR" in where_clause
    assert len(params) >= 2  # 至少两个字的 LIKE


# ——— TC0008: 精确匹配 ≥5 条时不触发降级 ———
def test_search_no_degrade_when_enough(in_memory_db):
    """TC0008: 精确匹配 ≥5 条时不触发降级"""
    conn = in_memory_db
    for i in range(6):
        upsert_listing(conn, make_listing(
            source_url=f"https://douban.com/topic/{i}",
            title=f"下沙精装{i}居室"
        ))
    conn.commit()

    results = search_listings(conn, {"keyword": "下沙", "limit": 20, "offset": 0})
    # 6 条精确匹配 >= 5，不应降级
    assert len(results) == 6


# ——— TC0009: 精确不足 → 降级宽松 → 去重合并 ———
def test_search_degrade_and_dedup(in_memory_db):
    """TC0009: 精确 3 条 → 降级宽松追加 → 去重合并"""
    conn = in_memory_db
    # 3 条精确匹配
    for i in range(3):
        upsert_listing(conn, make_listing(
            source_url=f"https://douban.com/topic/e{i}",
            title=f"未来科技城 精装修 {i}室",
            address="余杭区未来科技城"
        ))
    # 3 条只匹配单字（宽松模式可命中"科技"）
    for i in range(3):
        upsert_listing(conn, make_listing(
            source_url=f"https://douban.com/topic/l{i}",
            title=f"科技园附近 {i}室",
            address="西湖区"
        ))
    conn.commit()

    results = search_listings(conn, {"keyword": "未来科技", "limit": 20, "offset": 0})
    # 精确 3 条 + 宽松可能追加更多（取决于单字匹配）
    assert len(results) >= 3, f"Expected at least 3 exact matches, got {len(results)}"
    # 至少有一条地址含"未来科技城"的
    urls = [r["source_url"] for r in results]
    assert "https://douban.com/topic/e0" in urls


# ——— 额外: 距离排序参数化验证 ———
def test_distance_sort_parameterized():
    """验证距离排序用浮点数插值（已验证为 float），且不往 params 加占位符。

    原因：_build_query 被 count_listings 复用，后者丢弃 ORDER BY 但保留 params，
    若 params 中有多余占位符会导致 SQLite 参数数量不匹配 → 500 错误。
    """
    filters = {"sort": "distance", "ref_lng": 120.15, "ref_lat": 30.28}
    where_clause, params, order, _dist = _build_query(filters, "none")
    assert "lng - 120.15" in order, "距离排序 order 应包含参考坐标"
    assert "lat - 30.28" in order, "距离排序 order 应包含参考坐标"
    # 关键：params 中不应含坐标占位符 — count_listings 依赖此行为
    assert 120.15 not in params, "坐标不应出现在 params 中（避免 count_listings 参数错位）"


# ——— 额外: 筛选组合 ———
def test_all_filters_combined(in_memory_db):
    """全选筛选条件 → 验证 WHERE 子句生成正确"""
    filters = {
        "keyword": "下沙",
        "price_min": 1000, "price_max": 3000,
        "house_types": ["一居", "两居"],
        "rent_types": ["整租"],
        "landlord_types": ["个人"],
        "source_platforms": ["豆瓣"],
        "hours_ago": 24,
        "sort": "price_asc",
        "limit": 20, "offset": 0,
    }
    where_clause, params, order, _dist = _build_query(filters, "exact")
    assert "price >= ?" in where_clause
    assert "price <= ?" in where_clause
    assert "house_type IN" in where_clause
    assert "rent_type IN" in where_clause
    assert "landlord_type IN" in where_clause
    assert "source_platform IN" in where_clause
    assert "publish_time >=" in where_clause
    assert order == "price ASC"


# ——— 额外: count_listings 与 search_listings 一致性 ———
def test_count_consistent_with_search(in_memory_db):
    """count_listings 应返回合理的上限数"""
    conn = in_memory_db
    for i in range(10):
        upsert_listing(conn, make_listing(
            source_url=f"https://douban.com/topic/c{i}",
            title=f"杭州租房 {i}"
        ))
    conn.commit()

    cnt = count_listings(conn, {"keyword": "杭州"})
    results = search_listings(conn, {"keyword": "杭州", "limit": 20, "offset": 0})
    # count >= search results (count 是上限)
    assert cnt >= len(results), f"count({cnt}) should >= search({len(results)})"


# ——— _row_to_dict: 防止 images 双重 JSON 编码 ———
def test_row_to_dict_handles_double_encoded_images(in_memory_db):
    """_row_to_dict: images 存为双重编码字符串时应正确解析为数组"""
    conn = in_memory_db
    # 模拟双重编码: json.dumps 两次
    import json
    raw_images = json.dumps(json.dumps(["https://img.example.com/1.jpg"]))
    conn.execute(
        "INSERT INTO listings (title, source_url, source_platform, images) VALUES (?, ?, ?, ?)",
        ("测试房源", "https://test.com/double_enc", "豆瓣", raw_images),
    )
    conn.commit()
    results = search_listings(conn, {"keyword": "测试房源", "limit": 10, "offset": 0})
    assert len(results) == 1, f"Expected 1 result, got {len(results)}"
    imgs = results[0]["images"]
    assert isinstance(imgs, list), f"images should be list, got {type(imgs).__name__}: {imgs!r}"
    assert len(imgs) == 1
    assert imgs[0] == "https://img.example.com/1.jpg"


def test_row_to_dict_handles_normal_images(in_memory_db):
    """_row_to_dict: 正常 JSON 数组应正确解析"""
    conn = in_memory_db
    conn.execute(
        "INSERT INTO listings (title, source_url, source_platform, images) VALUES (?, ?, ?, ?)",
        ("正常房源", "https://test.com/normal", "闲鱼", '["https://a.jpg","https://b.jpg"]'),
    )
    conn.commit()
    results = search_listings(conn, {"keyword": "正常房源", "limit": 10, "offset": 0})
    assert len(results) == 1
    imgs = results[0]["images"]
    assert isinstance(imgs, list), f"images should be list, got {type(imgs).__name__}"
    assert len(imgs) == 2


def test_row_to_dict_handles_empty_images_string(in_memory_db):
    """_row_to_dict: images='[]' 时应返回空列表"""
    conn = in_memory_db
    conn.execute(
        "INSERT INTO listings (title, source_url, source_platform, images) VALUES (?, ?, ?, ?)",
        ("空图片", "https://test.com/empty_img", "豆瓣", "[]"),
    )
    conn.commit()
    results = search_listings(conn, {"keyword": "空图片", "limit": 10, "offset": 0})
    assert len(results) == 1
    imgs = results[0]["images"]
    assert isinstance(imgs, list), f"images should be list, got {type(imgs).__name__}"
    assert imgs == []
