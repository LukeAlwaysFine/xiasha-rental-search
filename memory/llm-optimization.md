---
name: llm-optimization
description: DeepSeek V4 LLM 提取的优化配置和教训
metadata:
  type: project
---

# LLM 提取优化

## 关键教训: AsyncOpenAI 是强制要求

**绝不使用同步 `OpenAI` 客户端。** 同步调用 `client.chat.completions.create()` 会阻塞 `asyncio.gather` 的事件循环。表面上代码是并行的（semaphore + gather），实际完全串行。

验证数据: 20 条房源，同步版 37 秒，异步版 2.2 秒（17 倍差距）。

## 当前最优配置

| 参数 | 值 | 原因 |
|------|-----|------|
| 客户端 | `AsyncOpenAI` | 真正异步 |
| 模型 | `deepseek-v4-flash` | V4 最快最便宜 |
| thinking | disabled (`extra_body={"thinking": {"type": "disabled"}}`) | V4 默认开启推理链，提取任务不需要 |
| max_tokens | 400 | JSON 输出约 200 token |
| 输入截断 | 2500 字符 | 房源关键信息在前 1000 字 |
| 并发 | 40（`LLM_CONCURRENCY` 环境变量） | 按 DeepSeek 付费 500 RPM 配置 |
| temperature | 0.1 | 确定输出 |
| response_format | json_object | 结构化输出 |

## 不需要的参数

- `reasoning_effort` — V4 只在 thinking=enabled 时有效，提取任务不开启 thinking
- `deepseek-chat` (V3) — 将于 2026-07-24 废弃，已迁移到 V4

## 实现位置

- `src/extractor/llm_extract.py`: AsyncOpenAI 单例 + `extract_listing()` async 函数
- `src/api/server.py`: `_import_listings()` 用 semaphore(40) + gather
- `src/pipeline.py`: `fetch_new_listings()` 同样并行

**Why:** 从同步 OpenAI 切换到 AsyncOpenAI 是单次最大性能提升（17x）。配置通过环境变量暴露，方便调优。

**How to apply:** 新增 LLM 调用必须用 `await extract_listing()`，禁止同步调用。修改并发数改 `.env` 的 `LLM_CONCURRENCY`。
