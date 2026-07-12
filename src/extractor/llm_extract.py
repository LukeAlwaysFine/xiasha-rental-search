"""LLM 信息提取 — 从原始帖文提取结构化房源数据"""

import json
import os
from src.llm.client import get_client, LLM_CONFIG

LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # 密钥字符串，用于检查是否已配置
LLM_MODEL = LLM_CONFIG["model"]
LLM_MAX_TOKENS = LLM_CONFIG["max_tokens"]
LLM_TEMPERATURE = LLM_CONFIG["temperature"]

EXTRACT_PROMPT = """从以下帖文内容判断是否为租房信息，如果是则提取结构化数据。同时分析发帖人是否为中介。只返回 JSON，不要说其他内容。

帖文内容：
---
{content}
---

如果不是租房帖（比如卖东西、会员卡券、数码产品、二手车、活动组织、征友等），返回：
{{"is_rental": false}}

如果是租房相关的帖子（出租、转租、合租、找室友），返回：
{{
  "is_rental": true,
  "title": "简洁标题",
  "price": 数字(月租元),
  "house_type": "开间/一居/两居/三居/四居+/未知",
  "address": "完整地址（必须包含'杭州'）",
  "area": 数字(平方米)或null,
  "rent_type": "整租/合租/单间/转租/未知",
  "contact": "微信号或手机号，没有则为null",
  "poster_id": "发帖人ID/昵称，没有则为null",
  "publish_time": "发布时间 ISO8601或null",
  "is_sublet": true/false,
  "images": ["图片URL列表"],
  "agent_signals": ["中介信号列表，如无则空数组"],
  "agent_confidence": "高/中/低/无",
  "agent_reasoning": "判断理由简述"
}}

规则：
- **重要**：求租帖（发帖人在寻找房源/求合租/想租房/求室友）is_rental 必须填 false — 这不是出租信息
- 出租/转租/招合租（发帖人在提供房源）is_rental 填 true
- 必须包含租金（月租）且地址在杭州，否则 is_rental 填 false
- 价格只提取数字，去除"元/月""/月"等后缀
- 如果是转租（含"转租""个人转租""工作调动转租"等），rent_type 填"转租"，is_sublet 填 true
- 合租信息（找室友、求合租）rent_type 填"合租"
- 如果没有明确信息，对应字段填 null
- contact 包含房东留下的微信号、手机号等

中介判断规则（agent_signals / agent_confidence / agent_reasoning）：
仔细分析帖文内容，判断发帖人是个人房东还是中介：

个人房东特征（agent_confidence 填"无"或"低"）：
- 使用第一人称描述："我家的""自己住的""因工作调动转租""个人房源"
- 提到个人原因："换工作""离开杭州""回老家""换城市"
- 描述生活细节："住了两年""邻居很好""采光好我平时..."
- 明确否认中介："不是中介""非中介""个人房东""房东直租"
- 帖子数量少、描述具体、有个人故事感

中介特征（agent_confidence 填"高"或"中"）：
- 专业化用语："多套在租""户型齐全""多种户型可选"
- 公司化表述："本公司""专业托管""品牌公寓""欢迎同行合作"
- 营销话术："随时看房""免费看房""拎包入住""更多房源请咨询"
- 联系方式商业化："加微信详询""扫码看房""24小时在线"
- 帖子模板化、缺少个人细节、像复制粘贴的广告
- 发帖人ID含"租房""房源""公寓""房产""管家""托管"等商业词
- 价格异常低或异常高，明显不是个人房东定价

agent_signals: 列出帖文中实际检测到的中介信号关键词或特征（如 ["多套在租", "加微信详询", "ID含'公寓'"]），没有则为空数组
agent_confidence: 根据信号强度和数量综合判断 — "高"(明确中介) / "中"(疑似中介) / "低"(疑似个人) / "无"(明确个人)
agent_reasoning: 一句话简述判断逻辑（如 "多处中介话术+ID商业化" 或 "个人转租有具体生活细节"）"""


async def extract_listing(content: str) -> dict | None:
    """从原始内容提取结构化房源信息。非租房内容返回 None。"""
    client = get_client()
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(content=content[:2500])}],
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},  # 关闭 V4 默认思考，大幅提速
    )

    text = resp.choices[0].message.content
    try:
        # 清理可能的 markdown 代码块包裹
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text.strip())
        # 检查 LLM 判断是否为租房内容
        if not data.get("is_rental", False):  # 默认 False，仅 LLM 明确标记为租房时才处理
            return None
        # 后处理：清理 poster_id 字符串 "None" 值为真正的 None
        if data.get("poster_id") in ("None", "none", "null", ""):
            data["poster_id"] = None
        # 后处理：过滤"求租"帖 — 发帖人在寻找而非提供房源
        title = (data.get("title") or "").strip()
        if title and any(kw in title for kw in ["求租", "求合租", "想租", "找室友", "求室友"]):
            return None
        return data
    except json.JSONDecodeError:
        return None
