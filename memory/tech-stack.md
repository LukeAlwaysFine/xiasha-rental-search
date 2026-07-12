---
name: tech-stack
description: 技术选型与月成本估算
metadata:
  type: project
---

# 技术选型

| 层 | 选择 | 月成本 |
|------|------|:--:|
| 抓取 | Firecrawl 自部署 (Docker) | ¥0 |
| 提取 | 通义千问 / DeepSeek API | ¥20-50 |
| 地理编码 | 高德 API (免费 5000次/天) | ¥0 |
| 数据库 | SQLite | ¥0 |
| 后端 | Python FastAPI | ¥0 |
| 前端 | 单页 HTML + 高德 JS API | ¥0 |
| 部署 | 先本地，后 Railway/阿里云学生机 | ¥0-50 |

**MVP 月成本: ¥20-50**

**Why:** 技术小白 + 预算有限的 side project。尽最大可能用免费额度，唯一的硬成本是 LLM API。
**How to apply:** 新增依赖前先对照此表，避免引入需要付费的服务。优先用 stdlib 或已有依赖解决问题。

[[project-overview]] [[product-decisions]]
