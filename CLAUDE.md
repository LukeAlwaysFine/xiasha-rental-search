# CLAUDE.md

租房搜索聚合平台（下沙专版）— 从闲鱼、豆瓣、微博抓取，LLM 提取 + 高德地图筛选。

详细文档：
- [后端架构](docs/backend.md) — 抓取、提取、中介检测、数据库、API
- [抓取与过滤机制](docs/pipeline-detail.md) — 完整抓取流程、过滤规则、中介判定
- [前端架构](docs/frontend.md) — UI 设计、地图、筛选、交互
- [部署 & 环境](docs/deployment.md) — 环境变量、常用命令、容器
- [Bug 记录](docs/bugs.md)

## 技术栈速览

| 层 | 技术 | 要点 |
|------|------|------|
| 抓取 | Playwright + httpx + BS4 | 闲鱼 MTOP 拦截，豆瓣直连，微博 Ajax |
| 提取 | DeepSeek V4 Flash | AsyncOpenAI，thinking=disabled，40 并发 |
| 地理编码 | 高德 API | 多策略重试 + Input Tips，2并发 batch |
| 数据库 | SQLite WAL | `listings` + `favorites`，UPSERT 零值保护 + COALESCE 回填 |
| 后端 | FastAPI + APScheduler | 速率限制，6h poster 检测 + 6h 清理 |
| 前端 | Vanilla JS + 高德 JS API 2.0 | taste-skill 设计体系，暗色模式 |

## 数据流

```
搜索 → 抓取（~165关键词）→ LLM 提取（40并发，不判中介）→ API坐标优先
→ geocode兜底 → Regex 强信号直判中介（默认"疑似中介"）
→ 入库 → poster主页检测（5并发，有缓存）→ LLM重排/去重 → 前端
```

## 关键决策

- **poster_id**: MTOP `userNickName` 存入，Poster 检查时非数字 poster_id 从物品页提取数字 userId 并回写 DB。MTOP 首响应 dump exContent keys 便于排查
- **中介判定**: 零 LLM 调用。三条强信号直判中介（品牌名/昵称商业词/同poster≥3）。"个人"唯一来源是 Playwright 进主页核实（1出租+有其他物品）。Poster 与 pipeline 融合，缓存：个人24h/疑似中介6h
- **Poster 检查**: 5 并发 tab（Semaphore + asyncio.gather）。判定四档（≥3→中介，2→疑似中介，1+无其他→疑似中介，1+有其他→个人）。手动触发始终全量重判（先清 poster_checked_at）。抓取后自动触发 + 每 6h 定时
- **UPSERT**: 零值覆盖防 LLM 幻觉，poster_id/contact COALESCE 回填
- **前端竞态**: `_posterCheckPollId` 主动 clearTimeout 防残留

## 平台状态

| 闲鱼 ✅ | 豆瓣 ✅ | 微博 ✅ | 小红书 🔴 | 58 ❌ | 贝壳 🔴 |
