---
name: sprint1-retro-phase1-3
description: Sprint 1 Phase 1-3 团队复盘记录
metadata:
  type: project
---

# Sprint 1 Phase 1-3 复盘

**日期**: 2026-07-10 | **完成**: 3/7 阶段

## 已完成任务

### Phase 1: 数据安全修复 ✅
| 修复 | 文件 | 改动 |
|------|------|------|
| SQL 注入 | `src/db/schema.py` | 距离排序 `ORDER BY` 改为 `?` 参数化查询 |
| SQLite 并发写入 | `src/api/server.py` | `_import_listings` 并行 LLM 提取 + 串行写入 + asyncio.Lock + 每 worker 独立连接 |
| 同上 | `src/pipeline.py` | `fetch_new_listings` 每 worker 独立 detect_conn |

### Phase 2: 前端安全 + 数据质量 ✅
| 修复 | 文件 | 改动 |
|------|------|------|
| XSS | `src/web/index.html` | 移除 inline onclick，改用 `data-url` + `handleCardClick` + `escapeAttr` |
| 死 Chip | `src/web/index.html` | 移除 58同城 和 Wellcee 来源 chip |
| 假阳性 | `src/detector/agent_detector.py` | 从 `sublet_keywords` 移除"房东直租" |

### Phase 3: 技术债务清理 ✅
| 修复 | 文件 | 改动 |
|------|------|------|
| LLM 客户端共享 | 新建 `src/llm/client.py` | 单例 AsyncOpenAI, timeout=30s, max_retries=2, 统一 base_url |
| 三模块统一 | `llm_extract.py`, `llm_rerank.py`, `llm_dedup.py` | 全部改用 `from src.llm.client import get_client` |
| upsert 批量 commit | `src/db/schema.py` | 移除 `upsert_listing` 内 `conn.commit()` |
| 调用方补 commit | `src/pipeline.py` | `fetch_new_listings` 循环后 `conn.commit()` |
| httpx 连接池 | 新建 `src/http_client.py` | 共享 AsyncClient, limits(max_keepalive=20, max_connections=50) |
| 连接池应用 | `firecrawl_client.py`, `amap.py` | 改用 `get_http_client()` |
| 生命周期清理 | `src/api/server.py` | lifespan shutdown 调用 `close_http_client()` |
| 死代码清理 | `src/filter/llm_dedup.py` | `batch_dedup_all` 移除无操作 UPDATE，暂存为 ponytail |

## 改进方案

1. **测试验证不足**：Phase 1-3 只做了 import 验证，未编写单元测试。Phase 6 必须补上。
2. **依赖关系**：LLM 共享客户端作为基础设施改动，后续所有 LLM 调用模块都需遵守此模式。
3. **向后兼容**：`llm_extract.py` 保留了 `LLM_MODEL`, `LLM_MAX_TOKENS` 等变量导出，确保 `pipeline.py` 等模块不报 import 错误。

## 下一阶段

Phase 4 (UX闭环) + Phase 5 (可靠性提升) 将并行推进。

[[sprint1-retro-phase4-5]] [[product-upgrade-decisions]]
