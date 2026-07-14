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
    "代找房", "找房服务", "帮找房",
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
    标签: "个人" / "中介" / "疑似中介"
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
        return "疑似中介", hits
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
    """本地规则判定发帖人类型。不调 LLM。

    强信号 → 直接"中介"：
    - 品牌名（自如/贝壳/链家…）
    - poster 昵称含商业词（租房/公寓/管家…）
    - 同 poster ≥3 条
    - API 卖家房源 ≥3

    弱信号（中介话术）→ "疑似中介"
    无信号 → "疑似中介"（不做个人判断）

    返回: (标签, 命中规则列表, 元数据)
    """
    hits: list[str] = []

    # ——— 强信号：命中即中介 ———
    content_lower = content.lower()

    # 1. 品牌名
    for brand in AGENCY_BRANDS:
        if brand.lower() in content_lower:
            hits.append(f"品牌名: {brand}")
            return "中介", hits, {"hybrid_score": 10, "regex_hits": hits}

    # 2. poster 昵称含商业词
    if poster_id:
        for pattern in AGENCY_NAME_PATTERNS:
            if re.search(pattern, poster_id):
                hits.append(f"名称关键词: {pattern}")
                return "中介", hits, {"hybrid_score": 10, "regex_hits": hits}

    # 3. 同 poster ≥3 条（DB 内统计）
    if conn and poster_id:
        count = count_by_poster(conn, poster_id)
        if count >= 3:
            hits.append(f"同账号{count}条房源")
            return "中介", hits, {"hybrid_score": 10, "regex_hits": hits}

    # ——— 弱信号：话术 → 疑似中介 ———
    for pattern in AGENCY_PATTERNS:
        if re.search(pattern, content):
            hits.append(f"话术: {pattern}")

    # ——— 模板标题：纯结构描述+无个人语言 → 疑似中介 ———
    TITLE_TEMPLATE_RE = re.compile(
        r'^[\w一-鿿]+(?:花园|公寓|大厦|小区|城|苑|府|庭|湾|星|里|园)'
        r'(?:\d{2,4}方)?.*(?:整租|合租|单间|转租|出租|租房|[一二两三四五六七八九十]居|[一二两三四五六七八九十]室)',
    )
    HAS_PERSONAL_LANG = re.compile(r'[我自]|个人|房东直租|工作调动|离开杭州|回老家|换工作|急转')

    if TITLE_TEMPLATE_RE.search(content.strip()):
        if not HAS_PERSONAL_LANG.search(content):
            hits.insert(0, "模板标题+无个人语言")

    meta = {"hybrid_score": 3 if hits else 0, "regex_hits": hits}
    return "疑似中介", hits, meta


def is_sublet_from_content(content: str) -> bool:
    """从内容判断是否为转租帖。"""
    sublet_keywords = [
        "转租", "个人转租", "工作调动", "换工作", "离开杭州",
        "回老家", "因工作", "急转", "原价转", "剩余租期",
        "不是中介", "非中介",
    ]
    return any(kw in content for kw in sublet_keywords)
