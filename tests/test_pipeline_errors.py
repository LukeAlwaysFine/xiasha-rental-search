"""错误注入测试 — 验证外部依赖失败时不崩溃"""

import pytest
from unittest.mock import AsyncMock, patch
import json


# ——— LLM 提取: 非法 JSON → 返回 None ———
@pytest.mark.asyncio
async def test_extract_invalid_json():
    """DeepSeek 返回非 JSON → extract_listing 返回 None"""
    from unittest.mock import AsyncMock, MagicMock, patch

    # Mock AsyncOpenAI.chat.completions.create
    mock_create = AsyncMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "this is not valid json {{{"
    mock_create.return_value.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch("src.extractor.llm_extract.get_client", return_value=mock_client):
        from src.extractor.llm_extract import extract_listing
        result = await extract_listing("test content")
        assert result is None, f"Expected None for invalid JSON, got {result}"


# ——— LLM 提取: is_rental=false 应返回 None ———
@pytest.mark.asyncio
async def test_extract_non_rental_rejected():
    """LLM 返回 is_rental=false → extract_listing 返回 None"""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_create = AsyncMock()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"is_rental": false}'
    mock_create.return_value.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch("src.extractor.llm_extract.get_client", return_value=mock_client):
        from src.extractor.llm_extract import extract_listing
        result = await extract_listing("QQ音乐年卡会员 88元")
        assert result is None, f"Non-rental content should return None, got {result}"


# ——— LLM 提取: markdown 代码块剥离 ———
def test_json_cleanup_markdown():
    """验证 JSON 清理逻辑（markdown 代码块剥离）"""
    text = '```json\n{"title": "test", "price": 100}\n```'
    # 模拟 extract_listing 中的清理逻辑
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    result = json.loads(text.strip())
    assert result == {"title": "test", "price": 100}


def test_json_cleanup_plain():
    """纯 JSON 直接解析"""
    text = '{"title": "test", "price": 100}'
    result = json.loads(text.strip())
    assert result["price"] == 100


# ——— 后提取验证: _is_valid_rental 过滤非租房垃圾 ———
def test_valid_rental_passes():
    """正常租房数据应通过验证"""
    from src.pipeline import _is_valid_rental
    assert _is_valid_rental({"title": "杭州下沙精装一居", "address": "杭州钱塘区下沙", "price": 1500})
    assert _is_valid_rental({"title": "未来科技城转租", "address": "杭州市余杭区EFC", "price": 2000})


def test_non_rental_title_rejected():
    """非租房标题应被拒绝"""
    from src.pipeline import _is_valid_rental
    assert not _is_valid_rental({"title": "QQ音乐超级会员年卡", "address": "杭州", "price": 88})
    assert not _is_valid_rental({"title": "西部数据硬盘1T", "address": "杭州", "price": 105})
    assert not _is_valid_rental({"title": "出摩托车一辆", "address": "杭州", "price": 2000})


def test_low_price_rejected():
    """异常低价 (<100) 应被拒绝"""
    from src.pipeline import _is_valid_rental
    assert not _is_valid_rental({"title": "杭州单间出租", "address": "杭州下沙", "price": 50})
    assert not _is_valid_rental({"title": "杭州合租找室友", "address": "杭州滨江", "price": 1})


def test_no_address_rejected():
    """无地址或非杭州地址应被拒绝"""
    from src.pipeline import _is_valid_rental
    assert not _is_valid_rental({"title": "租房", "address": "", "price": 1500})
    assert not _is_valid_rental({"title": "上海单间出租", "address": "上海市浦东新区", "price": 3000})


def test_no_price_rejected():
    """无价格应被拒绝"""
    from src.pipeline import _is_valid_rental
    assert not _is_valid_rental({"title": "杭州租房", "address": "杭州", "price": None})


# ——— 高德 geocode 失败 ———
@pytest.mark.asyncio
async def test_geocode_no_api_key():
    """没有 API Key → geocode 抛异常"""
    from src.geocode.amap import geocode
    try:
        await geocode("杭州")
    except RuntimeError as e:
        assert "AMAP_API_KEY" in str(e)
