"""LLM 相关性重排 — 搜索后对结果按查询意图重新排序。

搜"下沙"时，SQL LIKE 只能匹配文本，但 LLM 能理解：
- "下沙金沙湖" → 高相关（在下沙）
- "拱宸桥东" → 低相关（不在下沙）
- "阿里西溪" → 不相关

LLM 打分后过滤低分结果，优先展示真正相关的房源。
"""

import os
import json
import logging
from src.llm.client import get_client, LLM_CONFIG

logger = logging.getLogger("rental.llm_rerank")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = LLM_CONFIG["model"]


RERANK_PROMPT = """你是一个租房搜索引擎的排序专家。用户搜索了"{query}"，以下是候选房源列表。

对每个房源评估与搜索词的**地理相关性**和**意图匹配度**：
- 房源地址是否在搜索词所指的区域？
- 房源描述是否与搜索意图匹配？
- 价格、户型是否符合该区域的普遍水平？

返回 JSON：
{{
  "ranked": [
    {{"id": <房源id>, "score": 1-10, "reason": "一句话理由"}},
    ...
  ],
  "remove_ids": [<不相关的房源id列表>]
}}

规则：
- score 9-10: 地址在目标区域，价格户型匹配
- score 6-8: 同区或邻近区域
- score 3-5: 同城但不在目标区域
- score 1-2: 弱相关（仅在同城）
- remove_ids: 只移除 score=1 或非租房信息（活动帖、团购、其他城市等）
- **重要**: 不要过度移除。杭州同城的房源即使不在搜索区域内也保留（score >= 2），
  只删除明确不是租房或不在杭州的内容。每页至少保留 10 条以上。
- 只返回 JSON，不要其他内容

候选房源：
{candidates}
"""


async def rerank_listings(listings: list[dict], query: str) -> list[dict]:
    """对搜索结果做 LLM 相关性重排。

    Args:
        listings: 房源列表，每个 dict 需含 id, title, address, price, house_type
        query: 用户搜索词

    Returns:
        重排后的房源列表（低相关度的被过滤），附带 _score 和 _reason 字段
    """
    if not LLM_API_KEY or not listings or not query.strip():
        return listings

    # 构建候选列表（只送关键字段给 LLM，省 token）
    candidates = []
    for l in listings:
        candidates.append({
            "id": l["id"],
            "title": l.get("title", ""),
            "address": l.get("address", ""),
            "price": l.get("price"),
            "house_type": l.get("house_type", ""),
            "rent_type": l.get("rent_type", ""),
            "source_platform": l.get("source_platform", ""),
        })

    # 最多送 30 条给 LLM 评估（省 token）
    to_eval = candidates[:30]
    candidates_json = json.dumps(to_eval, ensure_ascii=False, indent=2)

    client = get_client()
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": RERANK_PROMPT.format(
                query=query, candidates=candidates_json
            )}],
            temperature=0.1,
            max_tokens=2000,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )

        text = resp.choices[0].message.content
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())

        # 构建 id → listing 的映射
        id_map = {l["id"]: l for l in listings}
        remove_ids = set(result.get("remove_ids", []))

        # 按 score 排序，只移除 score=1 或在 remove_ids 中的
        ranked = result.get("ranked", [])
        reranked = []
        for item in sorted(ranked, key=lambda x: x.get("score", 0), reverse=True):
            lid = item["id"]
            score = item.get("score", 5)
            if lid in id_map and lid not in remove_ids and score > 1:
                entry = dict(id_map[lid])
                entry["_score"] = score
                entry["_reason"] = item.get("reason", "")
                reranked.append(entry)

        # 补充 LLM 没提到的房源（给默认中等分，排在后面但不删除）
        ranked_ids = {item["id"] for item in ranked}
        for l in listings:
            if l["id"] not in ranked_ids:
                entry = dict(l)
                entry["_score"] = 3
                entry["_reason"] = ""
                reranked.append(entry)

        return reranked

    except Exception as e:
        logger.warning(f"LLM rerank failed: {e}, returning original order")
        return listings
