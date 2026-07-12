"""中介识别 — 多维度判定发帖人是否为中介。LLM 推理 + 规则混合。"""

import re
import logging
from src.db.schema import count_by_poster, count_by_contact

logger = logging.getLogger("rental.agent_detector")

# 品牌名词库
AGENCY_BRANDS = [
    "自如", "贝壳", "贝壳找房", "链家", "我爱我家", "相寓", "泊寓",
    "冠寓", "蛋壳", "青客", "魔方公寓", "YOU+", "优客逸家", "城家公寓",
    "窝趣", "乐乎", "安歆", "美丽屋", "巴乐兔", "蘑菇租房", "房天下",
    "安居客", "58同城", "赶集网",
]

# 中介话术特征
AGENCY_PATTERNS = [
    r"多套在租",
    r"加微信详[询询]",
    r"公司直租",
    r"拎包入住",
    r"随时看房",
    r"免费看房",
    r"无中介费.*无服务费",
    r"全城.*房源",
    r"更多房源",
    r"扫码.*看房",
    r"全天.*看房",
    r"品牌公寓",
    r"专业.*托管",
    r"房屋管家",
    r"不收中介费",  # 中介也常说
    r"房源真实",
    r"实拍图.*实价",
    # 中介模板标题特征
    r"独门独户",
    r"首次出租",
    r"房东直租.*无中介",  # 中介常伪装
    r"户型方正.*采光好",  # 中介模板套话
    r"交通便利.*配套齐全",  # 中介模板套话
    r"押一付[一三]",
    r"长租.*优惠",
    r"看房.*方便",
    r"随时.*入住",
    r"包物业.*包宽带",
    r"民用水电",
    r"可短租.*可月付",
    r"房东.*委托",  # 托管公司
    r"统一.*管理",
    r"公寓.*直租",
    r"无.*中介费",  # 中介伪装说辞
    r"真实.*照片",
    r"原户型.*实拍",
]

# 发帖人名称关键词 — 名称本身暴露中介身份
AGENCY_NAME_PATTERNS = [
    r"租房",
    r"房源",
    r"公寓",
    r"找房",
    r"好房",
    r"房屋",
    r"租售",
    r"房产",
    r"中介",
    r"托管",
    r"管家",
    r"置业",
    r"地产",
]


def detect(content: str, poster_id: str, contact: str = None, conn=None) -> tuple[str, list[str]]:
    """
    判定房东类型。

    返回: (标签, [命中规则列表])
    标签: "个人" / "中介" / "未知"
    """
    hits = []

    # 1. 品牌名匹配
    content_lower = content.lower()
    for brand in AGENCY_BRANDS:
        if brand.lower() in content_lower:
            hits.append(f"品牌名: {brand}")

    # 2. 中介话术
    for pattern in AGENCY_PATTERNS:
        if re.search(pattern, content):
            hits.append(f"话术: {pattern}")

    # 2.5. 发帖人名称含中介关键词
    if poster_id:
        for pattern in AGENCY_NAME_PATTERNS:
            if re.search(pattern, poster_id):
                hits.append(f"名称关键词: {pattern}")
                break  # 一个命中即够

    # 3. 同账号多房源 (需要 DB 连接)
    if conn and poster_id:
        count = count_by_poster(conn, poster_id)
        if count >= 3:
            hits.append(f"同账号{count}条房源")

    # 4. 联系方式重复 (需要 DB 连接)
    if conn and contact:
        count = count_by_contact(conn, contact)
        if count >= 2:
            hits.append(f"同联系方式{count}条房源")

    # 判定
    if len(hits) >= 2 or any(h.startswith("品牌名") for h in hits):
        return "中介", hits
    elif hits:
        return "未知", hits
    else:
        return "个人", []


def detect_with_llm(
    content: str,
    poster_id: str = "",
    contact: str = None,
    conn=None,
    llm_agent_signals: list[str] | None = None,
    llm_agent_confidence: str = "",
    llm_agent_reasoning: str = "",
    seller_item_count: int | None = None,
) -> tuple[str, list[str], dict]:
    """混合判定：LLM 推理 + regex 规则融合。

    LLM 信号优先级高于 regex：
    - LLM 高置信 → 直接"中介"
    - LLM 无 → 直接"个人"（除非 regex 命中品牌名/多房源硬证据）
    - LLM 中/低 → regex 辅助判定
    - seller_item_count ≥ 3 → 平台级强信号（卖家在闲鱼发布 ≥3 条 → 中介）

    返回: (标签, 命中规则列表, 元数据)
    元数据包含: llm_confidence, llm_signals, llm_reasoning, hybrid_score
    """
    meta = {
        "llm_confidence": llm_agent_confidence,
        "llm_signals": llm_agent_signals or [],
        "llm_reasoning": llm_agent_reasoning,
        "regex_hits": [],
        "hybrid_score": 0,  # 0=明确个人, 10=明确中介
    }

    # ——— LLM 信号评分 ———
    llm_score = 0
    if llm_agent_confidence == "高":
        llm_score = 10
    elif llm_agent_confidence == "中":
        llm_score = 6
    elif llm_agent_confidence == "低":
        llm_score = 2
    # "无" → llm_score = 0

    # ——— Regex 规则评分（作为 fallback 和补充） ———
    regex_hits = []
    regex_score = 0

    # 1. 品牌名 → 强信号
    content_lower = content.lower()
    for brand in AGENCY_BRANDS:
        if brand.lower() in content_lower:
            regex_hits.append(f"品牌名: {brand}")
            regex_score += 8  # 品牌名是硬证据

    # 2. 中介话术
    for pattern in AGENCY_PATTERNS:
        if re.search(pattern, content):
            regex_hits.append(f"话术: {pattern}")
            regex_score += 3

    # 3. 名称关键词
    if poster_id:
        for pattern in AGENCY_NAME_PATTERNS:
            if re.search(pattern, poster_id):
                regex_hits.append(f"名称关键词: {pattern}")
                regex_score += 4
                break

    # 4. 同账号多房源（DB 内统计）
    if conn and poster_id:
        count = count_by_poster(conn, poster_id)
        if count >= 3:
            regex_hits.append(f"同账号{count}条房源")
            regex_score += 6

    # 4.5. 平台级卖家房源数（从 MTOP API 直接提取，无需 DB 积累）
    if seller_item_count is not None and seller_item_count >= 3:
        regex_hits.append(f"闲鱼卖家{seller_item_count}条房源")
        regex_score += 10  # 平台级强信号，与 LLM 高置信等同

    # 5. 同联系方式多房源
    if conn and contact:
        count = count_by_contact(conn, contact)
        if count >= 2:
            regex_hits.append(f"同联系方式{count}条房源")
            regex_score += 6

    meta["regex_hits"] = regex_hits
    # 混合评分：取 LLM 和 regex 中的最高分
    meta["hybrid_score"] = max(llm_score, regex_score)

    # ——— 综合判定 ———
    all_hits = (llm_agent_signals or []) + regex_hits

    # LLM 高置信 → 直接中介（除非 regex 有反证，极少见）
    if llm_agent_confidence == "高":
        return "中介", all_hits, meta

    # 模板标题检测：纯结构描述+无个人语言 → 至少标"未知"
    # 必须在"LLM 无 → 个人"判断之前执行，否则会死逻辑
    TITLE_TEMPLATE_RE = re.compile(
        r'^[\w一-鿿]+(?:花园|公寓|大厦|小区|城|苑|府|庭|湾|星|里|园)'
        r'(?:\d{2,4}方)?.*(?:整租|合租|单间|转租|出租|租房|[一二两三四五六七八九十]居|[一二两三四五六七八九十]室)',
    )
    HAS_PERSONAL_LANG = re.compile(r'[我自]|个人|房东直租|工作调动|离开杭州|回老家|换工作|急转')

    if TITLE_TEMPLATE_RE.search(content.strip()):
        if not HAS_PERSONAL_LANG.search(content):
            all_hits.insert(0, "模板标题+无个人语言")
            meta["hybrid_score"] = max(meta["hybrid_score"], 4)
            return "未知", all_hits, meta

    # LLM 明确说无 + regex 也无强信号 → 个人
    if llm_agent_confidence == "无" and regex_score < 6:
        return "个人", all_hits, meta

    # 品牌名或硬证据 → 中介
    if regex_score >= 8:
        return "中介", all_hits, meta

    # 混合评分 >= 6 → 中介
    if meta["hybrid_score"] >= 6:
        return "中介", all_hits, meta

    # 混合评分 >= 3 → 疑似（前端显示"未知"）
    if meta["hybrid_score"] >= 3:
        return "未知", all_hits, meta

    # 默认个人
    return "个人", all_hits, meta


def is_sublet_from_content(content: str) -> bool:
    """从内容判断是否为转租帖。"""
    sublet_keywords = [
        "转租", "个人转租", "工作调动", "换工作", "离开杭州",
        "回老家", "因工作", "急转", "原价转", "剩余租期",
        "不是中介", "非中介",
    ]
    return any(kw in content for kw in sublet_keywords)


def reevaluate_all(conn) -> dict:
    """全库重新评估中介状态。利用已有数据做批量修正。

    规则（入库后运行，有完整数据）：
    1. 同 poster_id >= 3 条 → 全部标记为中介
    2. 同 contact >= 2 条 → 全部标记为中介
    3. poster_id 含中介名称关键词 → 标记为中介

    返回 {"updated": int}
    """
    updated = 0

    # R1: 同 poster >= 3 条（与 detect_with_llm 阈值一致，DISTINCT 去重防同一 URL 多次入库）
    rows = conn.execute("""
        SELECT poster_id FROM listings
        WHERE poster_id IS NOT NULL AND poster_id != '' AND poster_id != 'None'
        GROUP BY poster_id HAVING COUNT(DISTINCT source_url) >= 3
    """).fetchall()
    for (pid,) in rows:
        cur = conn.execute(
            "UPDATE listings SET landlord_type='中介' WHERE poster_id=? AND landlord_type!='中介'",
            (pid,),
        )
        updated += cur.rowcount

    # R2: 同 contact 多条（DISTINCT 去重）
    rows = conn.execute("""
        SELECT contact FROM listings
        WHERE contact IS NOT NULL AND contact != ''
        GROUP BY contact HAVING COUNT(DISTINCT source_url) >= 2
    """).fetchall()
    for (c,) in rows:
        cur = conn.execute(
            "UPDATE listings SET landlord_type='中介' WHERE contact=? AND landlord_type!='中介'",
            (c,),
        )
        updated += cur.rowcount

    # R3: poster_id 名称含中介关键词（参数化查询，排除"非中介"/"无中介"等反模式）
    like_clauses = " OR ".join(["poster_id LIKE ?" for _ in AGENCY_NAME_PATTERNS])
    like_params = [f"%{p}%" for p in AGENCY_NAME_PATTERNS]
    # 排除明确声称非中介的用户（如 "我不是中介"、"无中介房源"）
    exclude_clauses = " AND poster_id NOT LIKE ? AND poster_id NOT LIKE ?"
    like_params.extend(["%非中介%", "%无中介%"])
    cur = conn.execute(
        f"UPDATE listings SET landlord_type='中介' WHERE ({like_clauses}){exclude_clauses} AND landlord_type!='中介'",
        like_params
    )
    updated += cur.rowcount

    conn.commit()
    return {"updated": updated}
