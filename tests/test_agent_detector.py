"""测试中介检测 (TC0004-TC0007) + C2: detect_with_llm 生产函数覆盖"""

import pytest
from src.detector.agent_detector import detect, detect_with_llm, is_sublet_from_content


# ——— TC0004: 品牌名命中一次即判中介 ———
def test_detect_brand_name_direct_agent():
    """TC0004: 帖文含'自如' → 直接判中介"""
    content = "自如整租，近地铁，精装修拎包入住"
    label, hits = detect(content, "poster1", None, None)
    assert label == "中介", f"Expected 中介, got {label}"
    assert any("自如" in h for h in hits)


# ——— TC0005: 两个话术特征命中 + 无品牌名 → 判中介 ———
def test_detect_two_patterns_is_agent():
    """TC0005: 两个话术特征命中 → 判中介"""
    content = "多套在租，随时看房，精装修。微信联系"
    label, hits = detect(content, "poster2", None, None)
    assert label == "中介", f"Expected 中介, got {label}"
    assert len(hits) >= 2


# ——— TC0006: 仅一个话术命中 → 只输出未知 ———
def test_detect_one_pattern_unknown():
    """TC0006: 仅一个话术命中 → 未知标签"""
    content = "拎包入住，近地铁，房东本人直租"
    label, hits = detect(content, "poster3", None, None)
    assert label == "未知", f"Expected 未知, got {label}"
    assert len(hits) == 1


# ——— TC0007: 含'工作调动' → True，含'房东直租' → 不应判定为转租 ———
def test_sublet_keywords():
    """TC0007: '工作调动' → True"""
    assert is_sublet_from_content("因工作调动，转租下沙一室") == True

def test_landlord_direct_not_sublet():
    """TC0007 补充: '房东直租' 不应判定为转租"""
    # 该词已从 sublet_keywords 移除
    assert is_sublet_from_content("房东直租，无中介费，精装修") == False


# ——— 额外: 所有转租关键词 ———
def test_all_sublet_keywords():
    """正向验证所有转租关键词"""
    keywords = ["转租", "个人转租", "工作调动", "换工作", "离开杭州",
                "回老家", "因工作", "急转", "原价转", "剩余租期",
                "不是中介", "非中介"]
    for kw in keywords:
        assert is_sublet_from_content(f"xxx {kw} xxx") == True, f"Keyword '{kw}' should trigger sublet"


# ——— 额外: 同账号多房源 ———
def test_detect_poster_count(in_memory_db):
    """同 poster_id ≥ 3 条 → 判中介"""
    from tests.conftest import make_listing
    from src.db.schema import upsert_listing

    conn = in_memory_db
    poster = "agent007"
    for i in range(4):
        upsert_listing(conn, make_listing(
            source_url=f"https://xianyu.com/item/{i}",
            poster_id=poster,
            title=f"杭州租房第{i}套"
        ))
    conn.commit()

    content = "精装修拎包入住"  # 只有 1 个话术
    label, hits = detect(content, poster, None, conn)
    # 1 话术 + 同账号 ≥3 → >=2 hits → 中介
    assert label == "中介", f"Expected 中介, got {label}"


# ——— 额外: 正常个人房东 ———
def test_detect_personal():
    """无品牌名、无话术、少房源 → 个人"""
    content = "本人房东，下沙一室一厅，月租2000"
    label, hits = detect(content, "normal_user", None, None)
    assert label == "个人", f"Expected 个人, got {label}"
    assert hits == []


# ——— C2: detect_with_llm 生产函数测试覆盖 ———

def test_detect_with_llm_high_confidence_agent():
    """(a) LLM 高置信 → 直接判中介"""
    content = "精装修拎包入住，近地铁"
    label, hits, meta = detect_with_llm(
        content=content, poster_id="user1",
        llm_agent_signals=["疑似中介账号"],
        llm_agent_confidence="高",
        llm_agent_reasoning="该用户发布多条类似房源",
    )
    assert label == "中介", f"LLM 高置信应判中介，实际: {label}"
    assert meta["hybrid_score"] >= 6


def test_detect_with_llm_no_signal_personal():
    """(b) LLM 无信号 + regex 无命中 → 个人"""
    content = "本人房东，下沙自住两室一厅，因工作调动转租"
    label, hits, meta = detect_with_llm(
        content=content, poster_id="normal_user",
        llm_agent_signals=[],
        llm_agent_confidence="无",
        llm_agent_reasoning="",
    )
    assert label == "个人", f"LLM 无信号+无regex命中应判个人，实际: {label}"


def test_detect_with_llm_hybrid_scoring():
    """(c) LLM 中等置信 + regex 话术命中 → 混合评分判中介"""
    content = "多套在租，拎包入住，随时看房"
    label, hits, meta = detect_with_llm(
        content=content, poster_id="user3",
        llm_agent_signals=["疑似中介"],
        llm_agent_confidence="中",
        llm_agent_reasoning="",
    )
    # LLM 中等=6 + regex 话术≥6 → 应判中介
    assert label == "中介", f"混合评分应判中介，实际: {label}"
    assert meta["hybrid_score"] >= 6


def test_detect_with_llm_sublet_override(in_memory_db):
    """(d) is_sublet=true 时 landlord_type 应为'个人'（由调用方覆盖）"""
    # 注意: detect_with_llm 本身不处理 is_sublet，由 pipeline 处理
    # 这里验证 detect_with_llm 正确返回元数据供 pipeline 使用
    content = "自如整租，多套在租，随时看房"  # 强中介信号
    label, hits, meta = detect_with_llm(
        content=content, poster_id="agent_user",
        llm_agent_confidence="高",
        llm_agent_signals=["品牌公寓"],
        llm_agent_reasoning="含品牌名",
    )
    # detect_with_llm 返回"中介"，pipeline 会根据 is_sublet 覆盖
    assert label == "中介"
    # 验证元数据结构完整
    assert "hybrid_score" in meta
    assert "llm_confidence" in meta
    assert "regex_hits" in meta


def test_detect_with_llm_poster_contact_counts(in_memory_db):
    """(e) 同 poster ≥ 3 条时 regex 评分 +6"""
    from tests.conftest import make_listing
    from src.db.schema import upsert_listing

    conn = in_memory_db
    poster = "agent_multi"
    for i in range(4):
        upsert_listing(conn, make_listing(
            source_url=f"https://xianyu.com/item/c2_{i}",
            poster_id=poster,
            title=f"杭州精装出租第{i}套"
        ))
    conn.commit()

    content = "精装修拎包入住"  # 仅1个话术，不足以判中介
    label, hits, meta = detect_with_llm(
        content=content, poster_id=poster, conn=conn,
        llm_agent_confidence="无",
    )
    # 同账号≥3(+6) + 话术(+3) = 9 → 中介
    assert label == "中介", f"同账号多条应判中介，实际: {label}"
    assert meta["hybrid_score"] >= 6


def test_detect_with_llm_template_title():
    """模板标题 + 无个人语言 → 至少标'未知'"""
    content = "金沙湖花园90方精装修整租一室一厅，交通便利配套齐全"
    label, hits, meta = detect_with_llm(
        content=content, poster_id="unknown_user",
        llm_agent_confidence="无",
    )
    # 模板标题 + 无个人语言 → 未知
    assert label in ("未知", "中介"), f"模板标题应至少标未知，实际: {label}"


def test_detect_with_llm_brand_name():
    """品牌名命中 → 直接判中介"""
    content = "自如整租，近地铁，精装修"
    label, hits, meta = detect_with_llm(
        content=content, poster_id="brand_user",
        llm_agent_confidence="无",
    )
    assert label == "中介", f"品牌名命中应判中介，实际: {label}"


def test_detect_with_llm_metadata_completeness():
    """验证元数据结构完整性 — 所有字段必须存在"""
    content = "普通租房信息"
    _, _, meta = detect_with_llm(content=content, poster_id="user_x")
    required_keys = ["llm_confidence", "llm_signals", "llm_reasoning", "regex_hits", "hybrid_score"]
    for key in required_keys:
        assert key in meta, f"元数据缺少字段: {key}"
    assert isinstance(meta["hybrid_score"], (int, float))
    assert isinstance(meta["regex_hits"], list)
    assert isinstance(meta["llm_signals"], list)
