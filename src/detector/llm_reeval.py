"""LLM 全库中介重评 — 用 LLM 推理能力重新评估所有房源的房东类型。

用法:
    python -m src.detector.llm_reeval          # 全库重评（耗时较长）
    python -m src.detector.llm_reeval --limit 100   # 仅评估前 100 条
    python -m src.detector.llm_reeval --dry-run     # 仅报告，不写入
    python -m src.detector.llm_reeval --sample 50   # 随机抽样 50 条评估

原理：
1. 从 DB 读取房源内容和现有标签
2. 用 LLM（DeepSeek V4 Flash）分析内容，提取中介信号
3. 混合判定（LLM + regex）后更新 landlord_type
4. 支持高并发（LLM_CONCURRENCY 控制）
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
# 从项目根目录向上查找 .env（适配不同部署环境）
_project_root = Path(__file__).resolve().parent.parent.parent
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()  # fallback: 从当前目录查找

logger = logging.getLogger("rental.llm_reeval")

from src.db.schema import get_conn
from src.llm.client import get_client, LLM_CONFIG
from src.detector.agent_detector import detect_with_llm, reevaluate_all

AGENT_CHECK_PROMPT = """分析以下租房帖文，判断发帖人是"个人房东"还是"中介"。只返回 JSON。

帖文内容：
---
{content}
---

返回格式：
{{
  "agent_signals": ["检测到的中介信号1", "信号2"],
  "agent_confidence": "高/中/低/无",
  "agent_reasoning": "判断理由"
}}

中介判断标准：
- 个人房东：第一人称（"我家的""我因工作调动"）、个人生活细节、明确否认中介、帖子有故事感
- 中介：专业化用语（"多套在租""户型齐全"）、公司化表述（"品牌公寓""专业托管"）、营销话术（"随时看房""加微信详询"）、模板化内容、ID 含商业词（"租房""房源""公寓""管家"）

注意：
- agent_signals 列出具体的关键词或特征（如 "多套在租", "ID含'公寓'", "公司直租"），没有则为空数组
- agent_confidence 根据信号强度判断 — 有明显中介特征填"高"，仅一两个模糊信号填"中"，仅个人特征填"低"，明确个人填"无"
- agent_reasoning 用一句话简述判断逻辑"""


async def llm_check_agent(content: str) -> dict | None:
    """用 LLM 分析单条内容的中介信号。"""
    if not content or len(content) < 6:
        return None

    client = get_client()
    try:
        resp = await client.chat.completions.create(
            model=LLM_CONFIG["model"],
            messages=[{"role": "user", "content": AGENT_CHECK_PROMPT.format(content=content[:2000])}],
            temperature=0.1,
            max_tokens=200,
            extra_body={"thinking": {"type": "disabled"}},
        )
        import json
        text = resp.choices[0].message.content
        if not text:
            return None
        # Clean markdown wrappers
        text = text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"LLM agent check failed [{type(e).__name__}]: {e}")
        return None


async def llm_reevaluate_all(
    limit: int = 0,
    dry_run: bool = False,
    sample: int = 0,
) -> dict:
    """用 LLM 重新评估全库房源的房东类型。

    Args:
        limit: 限制处理条数（0=全部）
        dry_run: 仅分析不写入
        sample: 随机抽样条数（0=不抽样）

    Returns:
        {"total": int, "checked": int, "changed": int, "errors": int, "duration_s": float}
    """
    import random

    t0 = time.time()
    conn = get_conn()

    # 查询所有房源
    if sample > 0:
        rows = conn.execute(
            "SELECT id, title, poster_id, contact, landlord_type FROM listings "
            "WHERE title IS NOT NULL AND title != '' "
            "ORDER BY RANDOM() LIMIT ?",
            (sample,),
        ).fetchall()
    elif limit > 0:
        rows = conn.execute(
            "SELECT id, title, poster_id, contact, landlord_type FROM listings "
            "WHERE title IS NOT NULL AND title != '' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, poster_id, contact, landlord_type FROM listings "
            "WHERE title IS NOT NULL AND title != ''"
        ).fetchall()

    total = len(rows)
    if total == 0:
        conn.close()
        logger.warning("No listings with content_text found")
        return {"total": 0, "checked": 0, "changed": 0, "errors": 0, "duration_s": 0}

    logger.info(f"LLM reevaluate: {total} listings to check...")

    # 中介检测用较低并发——DeepSeek 免费版容易限流
    sem = asyncio.Semaphore(min(int(os.getenv("LLM_CONCURRENCY", "40")), 10))
    checked = [0]
    changed = [0]
    errors = [0]

    async def _check_one(row):
        listing_id, title, poster_id, contact, old_type = row
        async with sem:
            try:
                llm_result = await llm_check_agent(title or "")
            except Exception as e:
                errors[0] += 1
                if errors[0] <= 3:  # 只打印前几个错误
                    logger.warning(f"Check #{listing_id} error: {type(e).__name__}: {e}")
                return
            if llm_result is None:
                errors[0] += 1
                return

            # 混合判定
            new_type, hits, meta = detect_with_llm(
                content=title or "",
                poster_id=poster_id or "",
                contact=contact,
                conn=conn,
                llm_agent_signals=llm_result.get("agent_signals", []),
                llm_agent_confidence=llm_result.get("agent_confidence", ""),
                llm_agent_reasoning=llm_result.get("agent_reasoning", ""),
            )

            checked[0] += 1
            if new_type != old_type:
                changed[0] += 1
                reason = llm_result.get("agent_reasoning", "")[:60]
                old_label = old_type or "未知"
                logger.info(
                    f"[{checked[0]}/{total}] #{listing_id} {old_label}→{new_type} | "
                    f"score={meta['hybrid_score']} | {reason}"
                )
                if not dry_run:
                    conn.execute(
                        "UPDATE listings SET landlord_type=? WHERE id=?",
                        (new_type, listing_id),
                    )

            if checked[0] % 50 == 0:
                logger.info(f"  progress: {checked[0]}/{total} ({changed[0]} changed)")
                if not dry_run:
                    conn.commit()

    await asyncio.gather(*[_check_one(r) for r in rows], return_exceptions=True)

    if not dry_run:
        conn.commit()
        # 再做一次基于 DB 模式的批量修正（同 poster/同 contact）
        re_result = reevaluate_all(conn)
        if re_result["updated"] > 0:
            logger.info(f"reevaluate_all 补充修正: {re_result['updated']} 条")

    conn.close()

    stats = {
        "total": total,
        "checked": checked[0],
        "changed": changed[0],
        "errors": errors[0],
        "duration_s": round(time.time() - t0, 1),
    }
    logger.info(
        f"LLM reevaluate DONE: {checked[0]} checked, "
        f"{changed[0]} changed, {errors[0]} errors, {stats['duration_s']}s"
    )
    return stats


# ——— CLI ———
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LLM 全库中介重评")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条数")
    parser.add_argument("--sample", type=int, default=0, help="随机抽样条数")
    parser.add_argument("--dry-run", action="store_true", help="仅分析不写入")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _main():
        result = await llm_reevaluate_all(
            limit=args.limit,
            dry_run=args.dry_run,
            sample=args.sample,
        )
        print()
        print("=" * 50)
        print(f"  LLM 全库中介重评完成")
        print(f"  总数:     {result['total']}")
        print(f"  已检查:   {result['checked']}")
        print(f"  已修正:   {result['changed']}")
        print(f"  错误:     {result['errors']}")
        print(f"  耗时:     {result['duration_s']}s")
        print("=" * 50)

    asyncio.run(_main())
