"""LLM 跨平台去重 — 识别同一房源在不同平台的多条发帖。

场景: 同一套"下沙金沙湖一室一厅"可能同时在豆瓣和闲鱼发布。
LLM 比较标题、地址、价格、面积，判断是否为同一房源，合并为一条展示。
"""

import os
import json
import asyncio
import logging
from src.llm.client import get_client, LLM_CONFIG

logger = logging.getLogger("rental.llm_dedup")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = LLM_CONFIG["model"]


DEDUP_PROMPT = """判断以下房源列表中哪些是**同一套房源**的重复发布（同一房源在不同平台或重复发帖）。

返回 JSON：
{{
  "groups": [
    {{
      "keep_id": <保留的房源id（信息最完整的那个）>,
      "ids": [<该组所有房源id>],
      "merged_title": "合并后的最佳标题",
      "reason": "判断理由"
    }}
  ]
}}

判断标准（满足以下 3 条及以上即为重复）：
1. 地址指向同一小区/楼栋/地铁站（允许轻微表述差异，如"下沙金沙湖" vs "金沙湖地铁站"——
   这两个明显是同一个地方！）
2. 价格接近（差异 < 20%）
3. 户型相同
4. 面积接近（差异 < 30%）
5. 租金方式相同（整租/合租/转租）

**注意**: 地址在地铁站附近的，"XX地铁站"和"XX地铁口"、"XX旁"是同一地点。
同一小区/同一地铁站 + 价格基本相同 + 户型相同 = 同一房源。
宁可多合并也不要漏掉。保留信息最完整、发布时间最新的作为 keep_id。
只返回 JSON，不要其他内容。

候选房源：
{candidates}
"""


async def find_duplicates(listings: list[dict]) -> list[dict]:
    """在房源列表中找出跨平台重复，返回去重分组。

    每个分组保留信息最完整的一条，合并各平台的 contact 和 source。

    Returns:
        [{group_id, ids, keep_id, merged_title, sources, contacts, reason}]
    """
    if not LLM_API_KEY or len(listings) < 2:
        return []

    candidates = []
    for l in listings:
        candidates.append({
            "id": l["id"],
            "title": l.get("title", ""),
            "address": l.get("address", ""),
            "price": l.get("price"),
            "area": l.get("area"),
            "house_type": l.get("house_type", ""),
            "rent_type": l.get("rent_type", ""),
            "source_platform": l.get("source_platform", ""),
            "contact": l.get("contact", ""),
        })

    # 分组：按价格区间 + 地址相似度预分组，减少 LLM token
    # ponytail: 简化版直接送前 30 条给 LLM
    batch = candidates[:30]
    candidates_json = json.dumps(batch, ensure_ascii=False, indent=2)

    client = get_client()
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": DEDUP_PROMPT.format(candidates=candidates_json)}],
            temperature=0.1,
            max_tokens=1500,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )

        text = resp.choices[0].message.content
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        return result.get("groups", [])

    except Exception as e:
        logger.warning(f"LLM dedup failed: {e}")
        return []


async def merge_duplicates(listings: list[dict]) -> list[dict]:
    """搜索后处理：LLM 去重 + 合并。

    Returns:
        去重合并后的房源列表。被合并的房源带有 _merged_from 和 _merged_sources 字段。
    """
    groups = await find_duplicates(listings)
    if not groups:
        return listings

    # 构建 id→listing 映射
    id_map = {l["id"]: l for l in listings}
    all_dup_ids = set()
    merged_results = []

    for group in groups:
        keep_id = group["keep_id"]
        dup_ids = set(group["ids"])
        all_dup_ids.update(dup_ids)

        if keep_id in id_map:
            base = dict(id_map[keep_id])
            # 合并来源和联系方式
            sources = set()
            contacts = set()
            for did in dup_ids:
                if did in id_map:
                    sources.add(id_map[did].get("source_platform", ""))
                    c = id_map[did].get("contact")
                    if c:
                        contacts.add(c)
            base["_merged_sources"] = list(sources - {None, ""})
            base["_merged_contacts"] = list(contacts - {None, ""})
            base["_merged_title"] = group.get("merged_title", base.get("title", ""))
            base["_dedup_reason"] = group.get("reason", "")
            merged_results.append(base)

    # 添加未被合并的独立房源
    for l in listings:
        if l["id"] not in all_dup_ids and l["id"] not in {g["keep_id"] for g in groups}:
            merged_results.append(l)

    return merged_results


async def batch_dedup_all(conn) -> int:
    """定时任务：对所有房源做全量去重。

    按行政区或价格区间分批比较，识别跨平台重复并标记。
    返回发现的重复组数。

    注意：此函数当前已禁用（TODO: 需实现 dedup_groups 表后再启用），
    直接返回 0，不浪费 LLM token。
    """
    # TODO: disabled — results were discarded (no dedup_groups table yet)
    return 0
    rows = conn.execute("""  # pragma: no cover
        SELECT id, title, price, area, house_type, address, rent_type,
               source_platform, contact, source_url
        FROM listings
        ORDER BY price
    """).fetchall()

    listings = [dict(r) for r in rows]
    if len(listings) < 2:
        return 0

    # 按价格区间分批（每批 20 条），减少 LLM token 消耗
    BATCH_SIZE = 20
    total_groups = 0

    for i in range(0, len(listings), BATCH_SIZE):
        batch = listings[i:i + BATCH_SIZE]
        groups = await find_duplicates(batch)
        total_groups += len(groups)

        # ponytail: 去重结果暂不写入 DB（后续阶段实现 dedup_groups 表）
        # 当前仅通过 per-request merge_duplicates 做内存去重
        pass

    return total_groups
