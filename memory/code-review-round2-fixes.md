---
name: code-review-round2-fixes
description: 第二轮代码审查 21 项问题修复记录
metadata:
  type: project
---

2026-07-11 完成第二轮代码审查 21 项问题修复。

**Why:** 3 个专项 agent 并行静态分析发现 4 CRITICAL / 5 HIGH / 6 MEDIUM / 6 LOW 问题。

**How to apply:**
- 速率限制 `_check_rate_limit` 空桶时必须先写入 `[now]` 再返回 True，否则限流形同虚设
- `str.format()` 在值中含花括号是安全的（只解析模板占位符），N2 为误报
- `upsert_listing` 在 `asyncio.gather` 后串行 for 循环中调用，N5 为误报
- SQL 拼接必须用参数化查询（`?` 占位符 + 参数列表），严禁 f-string 拼入 LIKE 模式
- COALESCE 不区分 NULL 和零值，价格/坐标/面积需用 `CASE WHEN IS NULL OR = 0`
- geocode 变体生成避免盲目遍历全部 9 个区，仅对地址中匹配到的区名追加前缀
- `process_listing_item()` 为 pipeline.py 和 deep_dive.py 共享入口
- DB 连接必须 `try/finally` 保护 `conn.close()`
- `_is_too_old()` 支持 8+ 种日期格式，无法解析不拒绝（可能是相对时间）
- 前端全局定时器用 ID 追踪 + clearTimeout 防竞态覆盖
- `escapeHtml` 需剥离控制字符（与 `escapeAttr` 对齐）

关联: [[architecture-decisions]] [[platform-strategy]]
