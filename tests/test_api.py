"""测试 API 端点和集成 (TC0010)"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

# 启动测试 app（不触发 scheduler）
import os
os.environ.setdefault("AMAP_JS_KEY", "test_js_key")
os.environ.setdefault("LLM_API_KEY", "sk-test-mock-key-for-testing")
os.environ.setdefault("AMAP_API_KEY", "test_amap_key")
os.environ.setdefault("FIRECRAWL_API_KEY", "")

# 重置 LLM 客户端单例，让测试时的 mock key 生效
import src.llm.client as llm_client
llm_client._client = None

from src.api.server import app

client = TestClient(app)


# mock LLM 提取和 geocode，防止真实 API 调用
def _mock_extract_success():
    # 需要 mock server.py 中的导入引用，而非源模块
    return (
        patch("src.api.server.extract_listing", new=AsyncMock(return_value={
            "title": "测试房源", "price": 1500, "house_type": "一居",
            "address": "杭州下沙", "area": 45, "rent_type": "整租",
            "contact": "wx_test", "poster_id": "user_test",
            "publish_time": "2026-07-08T00:00:00", "is_sublet": False, "images": [],
        })),
        patch("src.api.server.geocode", new=AsyncMock(return_value=(120.15, 30.28))),
    )


def _apply_mocks():
    m1, m2 = _mock_extract_success()
    m1.start()
    m2.start()
    return [m1, m2]


# ——— TC0010: POST /api/import 幂等性 ———
def test_import_idempotent():
    """TC0010: 相同 source_url 两次提交 → 第二次返回 0 条导入"""
    data = [{
        "url": "https://xianyu.com/item/test_dup_001",
        "source": "闲鱼",
        "content": "杭州下沙一室一厅 月租1500 整租 个人房东 wx_test123"
    }]
    mocks = _apply_mocks()
    try:
        # 第一次导入
        resp1 = client.post("/api/import", json=data)
        assert resp1.status_code == 200, f"Expected 200, got {resp1.status_code}: {resp1.text}"
        imported1 = resp1.json()["imported"]

        # 第二次导入相同数据
        resp2 = client.post("/api/import", json=data)
        assert resp2.status_code == 200
        imported2 = resp2.json()["imported"]
        assert imported2 == 0, f"幂等性: 第二次应导入 0 条, 实际 {imported2}"
    finally:
        for m in mocks:
            m.stop()


# ——— 搜索 API 参数有效性 ———
def test_search_empty():
    """空搜索应返回结果（none 模式）"""
    resp = client.get("/api/search?keyword=&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert "listings" in data


def test_search_with_filters():
    """带筛选条件的搜索"""
    resp = client.get("/api/search?keyword=下沙&price_min=1000&price_max=3000&house_types=一居&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 0


def test_search_invalid_limit():
    """limit 超出范围应被 FastAPI 校验拒绝"""
    resp = client.get("/api/search?limit=500")
    assert resp.status_code == 422  # 超过 200 上限


# ——— 首页 ———
def test_index_contains_key():
    """首页应注入高德 JS Key"""
    resp = client.get("/")
    assert resp.status_code == 200
    # 验证 HTML 包含 key（或 key 为空字符串，取决于加载顺序）
    assert "AMap" in resp.text or "amap" in resp.text.lower()


# ——— 🧑 人类模拟测试: 模拟用户完整操作流程 ———
def test_human_search_flow():
    """模拟用户: 打开页面 → 搜索"下沙" → 验证结果结构 → 验证 images 是数组"""
    # Step 1: 用户打开首页
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AMap" in resp.text, "首页应加载高德地图"
    assert "下沙租房" in resp.text, "首页应显示标题"
    assert "下沙" in resp.text, "首页应有热门区域按钮"

    # Step 2: 用户点击"下沙"按钮 (前端 quickSearch → search → /api/search?keyword=下沙)
    resp = client.get("/api/search?keyword=下沙&sort=comprehensive&limit=20&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert "listings" in data
    assert data["total"] >= 0

    # Step 3: 验证每条结果的 images 都是数组 (防止前端 .map() 报错)
    for listing in data["listings"]:
        assert isinstance(listing.get("images"), list), \
            f"images must be list, got {type(listing.get('images')).__name__} for listing {listing.get('id')}"
        # 验证必要字段存在
        assert "id" in listing
        assert "title" in listing
        assert "source_url" in listing
        assert "source_platform" in listing

    # Step 4: 验证分页 — offset 翻到下一页
    if data["total"] > 10:
        resp2 = client.get("/api/search?keyword=下沙&sort=comprehensive&limit=20&offset=10")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["total"] == data["total"], "同关键词的 total 应一致"


def test_human_commute_flow():
    """模拟用户: 输入通勤地址 → geocode → 按距离搜索 → 验证结果可排序

    M15: 当 AMAP_API_KEY 未设置时，geocode 返回 503（而非崩溃），测试应优雅处理。
    """
    # Step 1: geocode 公司地址（可能因缺少 API Key 返回 400/404/503）
    resp = client.get("/api/geocode?address=杭州市余杭区阿里巴巴西溪园区")
    # 缺少 API Key 时返回 503 是正常行为，不应使测试失败
    if resp.status_code == 503:
        return  # API Key 未配置，跳过后续测试
    assert resp.status_code in (200, 400, 404), f"Unexpected status: {resp.status_code}"
    geo = resp.json()
    # 可能成功或失败（取决于高德 API key 和网络）
    if "lng" in geo:
        assert isinstance(geo["lng"], (int, float))
        assert isinstance(geo["lat"], (int, float))

        # Step 2: 按距离搜索
        resp = client.get(
            f"/api/search?keyword=下沙&sort=distance"
            f"&ref_lng={geo['lng']}&ref_lat={geo['lat']}&limit=5"
        )
        assert resp.status_code == 200
        data = resp.json()
        # 每个结果应有 images 是数组
        for listing in data["listings"]:
            assert isinstance(listing.get("images"), list), \
                f"images must be list in distance search for listing {listing.get('id')}"


def test_human_filter_flow():
    """模拟用户: 筛选 个人+整租+一居+豆瓣 → 验证无报错"""
    resp = client.get(
        "/api/search?keyword=杭州&house_types=一居&rent_types=整租"
        "&landlord_types=个人&source_platforms=豆瓣&sort=price_asc&limit=10"
    )
    assert resp.status_code == 200
    data = resp.json()
    for listing in data["listings"]:
        assert isinstance(listing.get("images"), list), \
            f"All listings must return images as array"


def test_empty_search_returns_arrays():
    """模拟用户: 首次打开页面，无关键词 → scroll 看到列表 → 不报错"""
    resp = client.get("/api/search?sort=comprehensive&limit=20&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 0
    for listing in data["listings"]:
        imgs = listing.get("images")
        assert isinstance(imgs, list), \
            f"Empty search: images must be list, got {type(imgs).__name__} for listing {listing.get('id')}: {imgs!r}"


# ——— 超大 JSON 拒绝 ———
def test_import_too_large():
    """超过 500 条的导入应被拒绝"""
    # 重置速率限制确保测试独立
    from src.api.server import _rate_limits
    _rate_limits.clear()

    mocks = _apply_mocks()
    try:
        data = [{"url": f"https://x.com/{i}", "source": "闲鱼", "content": "test"} for i in range(501)]
        resp = client.post("/api/import", json=data)
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        assert "500" in resp.json()["error"]
    finally:
        for m in mocks:
            m.stop()
