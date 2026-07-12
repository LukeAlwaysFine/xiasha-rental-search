# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 租房搜索聚合平台（下沙专版）

> Bug 记录与修复历史见 [docs/bugs.md](docs/bugs.md)

面向杭州下沙的房源搜索工具。从闲鱼、豆瓣、微博抓取信息，LLM 提取结构化数据，高德地图筛选排序。只做聚合展示，不做交易。小红书代码已完成但容器 headless 被反爬拦截。

## 常用命令

```bash
python3 run.py [port]                  # 启动服务 (默认 8000)
python3 scripts/deep_dive.py --area 下沙  # 下沙深潜抓取
python3 -m src.cleanup.url_checker --dry-run / --batch 50
python3 -m src.crawler.xianyu_async --login  # 首次扫码登录
python3 -m src.detector.llm_reeval --sample 50
# 批量 geocode / 中介重评（简写见文档末尾）
export LD_LIBRARY_PATH=/tmp/chromium-libs/lib:$LD_LIBRARY_PATH  # 容器 Chromium 依赖
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|:--:|------|
| `LLM_API_KEY` | ✅ | DeepSeek API Key |
| `LLM_MODEL` | 可选 | 默认 `deepseek-v4-flash` |
| `LLM_CONCURRENCY` | 可选 | 并行 LLM 请求数，默认 40 |
| `LLM_RERANK` / `LLM_DEDUP` | 可选 | 重排/去重开关，默认 `true` |
| `AMAP_API_KEY` | ✅ | 高德 Web 服务 Key |
| `AMAP_JS_KEY` | ✅ | 高德 JS API Key（前端地图） |
| `DB_PATH` | 可选 | SQLite 路径，默认 `rental_data.db` |
| `UVICORN_RELOAD` | 可选 | 热重载开关，默认 `true`，生产设 `false` |

## 技术栈

- **抓取**: Playwright（闲鱼 MTOP 拦截 + 微博 Ajax）+ httpx + BS4（豆瓣 SSR 直连）
- **提取**: DeepSeek V4 Flash（AsyncOpenAI，thinking=disabled，40 并发）
- **地理编码**: 高德 API 多策略重试 + Input Tips 回退；批量补齐（地址去重 + 2并发 + write_lock）。候选地址只追加命中区名，避免 9 区盲目遍历。闲鱼 MTOP 自带 GPS 覆盖 99.8%
- **数据库**: SQLite WAL 模式，UPSERT 零值覆盖保护（`CASE WHEN IS NULL OR = 0`）。favorites 表 FK 关联 listings，级联删除。
- **后端**: FastAPI + APScheduler（6h 深潜 + 6h URL 清理 + 1h 中介重评）
- **前端**: 单页 HTML + 高德 JS API 2.0 + taste-skill 设计体系。收藏按钮（圆形 ♥）乐观更新 + API 同步，头部"我的收藏"入口。

## 数据流

```
搜索 → search_and_fetch 无关键词默认"下沙" → SQLite 缓存（三级降级）→ 不足则后台三源抓取
→ AsyncOpenAI 并行提取（40并发，is_rental 默认 false）
→ API 坐标优先（闲鱼 MTOP 6 路径探测）→ 高德 geocode 兜底
→ LLM+Regex 混合中介检测 → 入库 → batch_geocode_missing + reevaluate_all
→ LLM 重排/去重 → 前端地图（全量 markers ≤1000）+ 列表（50条分页）→ 30s 轮询新数据
```

## 架构决策

### 闲鱼抓取

Playwright 拦截 MTOP API，从 JSON 直接提取结构化字段。MTOP 分页因 sign 校验不可用，策略：**199 个下沙专项关键词 × ~30 条/词**。

⚠️ 反爬：headless 触发 CAPTCHA，依赖首次导航的 API 拦截窗口期。80 关键词 warmup ~7s/词。容器需 Chromium libs 预装至 `/tmp/chromium-libs/lib`，导航用 `wait_until="domcontentloaded"` 超时 15s。

绕过 LLM 的字段：`api_address`（杭州+area+location）、`poster_id`（userNickName，用于中介检测）、`api_images`（多字段名合并去重）、`api_lng/api_lat`（6 路径探测，覆盖率 99.8%）。`publish_time` 入库前归一化为 ISO 8601（微博 HTTP date → UTC）。

### 中介检测（LLM + Regex 混合）

LLM 提取时同步分析 agent_signals/confidence/reasoning（零额外调用），`detect_with_llm()` 混合判定：
- 评分：品牌名 +8 / 话术 +3 / 名称关键词 +4 / 同 poster ≥3 条 +6 / 同 contact ≥2 条 +6
- 判定：≥6 中介 / 3-5 未知 / <3 个人
- 入库后 `reevaluate_all()` 批量修正（R1: 同 poster ≥3，COUNT(DISTINCT)；R2: 同 contact ≥2；R3: 参数化 LIKE + 反模式排除）

### 数据入库管线

`fetch_new_listings()` → `process_listing_item()`（pipeline/deep_dive 共享）→ `extract_listing()` → `_is_valid_rental()` → API 坐标优先 / API 地址覆盖 → geocode 兜底 → `detect_with_llm()` + upsert → `batch_geocode_missing()` + `reevaluate_all()`。

过滤：`_NON_RENTAL_TITLE_RE`（30+ 非居住关键词），价格 ¥300-50000。过期：`_is_too_old()` 支持 8+ 种日期格式，无法解析不拒绝。豆瓣详情 2 次重试，403 计数在任意非 403 响应时重置。

### 数据库

单表 `listings`，`source_url` UNIQUE。搜索三级降级 + 分页。排序：`_llm_score` > 面积 +3 > 户型 +3 > 个人 +5 > 超 7 天 -10。UPSERT 用 `CASE WHEN IS NULL OR = 0` 防 LLM 幻觉零值锁定。

`favorites` 表 `listing_id` UNIQUE，FK 关联 listings 级联删除。收藏 API 验证房源存在后写入，前端乐观更新 + API 同步（失败回滚）。搜索支持 `favorites_only` 子查询过滤。

### API 路由

| 路由 | 说明 |
|------|------|
| `GET /` | 前端单页，注入 `AMAP_JS_KEY`，HTML 模板 mtime 自动刷新 |
| `GET /api/search` | 搜索（多维度筛选 + 距离排序 + `favorites_only` 收藏过滤） |
| `GET /api/search/enhance` | LLM 增强结果缓存（TTL 120s） |
| `GET /api/listing/{id}` | 单条详情 |
| `GET /api/geocode` / `/api/inputtips` | 地址→坐标 / 输入提示 |
| `GET /api/fetch` / `POST /api/import` | 手动抓取 / 批量导入（2/min） |
| `GET /api/favorites` / `/ids` | 收藏列表（完整数据 / 仅 ID） |
| `POST /api/favorites/{id}` | 添加收藏（UNIQUE 防重复） |
| `DELETE /api/favorites/{id}` | 取消收藏 |

速率限制：token bucket 按 endpoint 分离（search 30/min, fetch/import 2/min），60s 窗口，`threading.Lock` 保护临界区，空桶先记录时间戳再放行。

### 模块结构

```
src/
  crawler/       xianyu_async.py / douban_http.py / weibo_crawler.py / xhs_crawler.py / deep_dive.py
  extractor/     llm_extract.py      # is_rental 默认 false
  geocode/       amap.py             # 多策略重试 + Input Tips + 批量补齐(write_lock)
  detector/      agent_detector.py   # R3 参数化 LIKE + 反模式排除
  filter/        llm_rerank.py / llm_dedup.py
  db/            schema.py           # 三级降级 + UPSERT 零值覆盖
  api/           server.py           # 速率限制+threading.Lock + 缓存 mtime 刷新
  web/           index.html          # escapeHtml 控制字符剥离 + 状态栏竞态防护
.claude/agents/  qa-team / *-tester / code-reviewer / e2e-tester
```

### 平台可行性

| 闲鱼 ✅ | 豆瓣 ✅ | 微博 ✅ | 小红书 🔴 反爬 | 58 ❌ | 贝壳 🔴 法律风险 |

## 开发约定

### UI 设计

taste-skill 体系：珊瑚红 `#ff6b6b`，深蓝灰 `#1a1a2e`。Geist 字体。圆角 12px。阴影 `0 1px 3px rgba(0,0,0,.06)` → hover `0 8px 24px rgba(0,0,0,.12)`。动效 `cubic-bezier(.4,0,.2,1)`，支持 `prefers-reduced-motion`。

### 前端交互

- 所有筛选点击即搜索；芯片 `data-group` 分组匹配；无坐标房源 SQL `CASE WHEN lng IS NULL THEN 1` 排最后
- 地址搜索：`/api/inputtips` 下拉 + localStorage 最近 10 条历史
- 地图：初始聚焦下沙 `[120.38, 30.31]` zoom 15；全量 markers（≤1000）网格聚类（~16m）；6 层暖→冷六色渐变距离圈（200m-5km）；AMap.Scale 比例尺；金色参考标记（📍）；距离模式 zoom 14 聚焦通勤点
- 标记三层覆盖：DB 预存 → 批量 geocode（2并发+逐条 gen 检查）→ 点击 fallback（`_offsetCoord` 去重叠 + `_statusTimeoutId` 防竞态）
- 房源更新通知：状态栏脉冲动画 + 30s 轮询新数据
- 安全：`escapeHtml`/`escapeAttr` 剥离控制字符，`safeUrl` 白名单阻止伪协议，外链 `rel="noopener"`
- 收藏：卡片圆形 ♥ 按钮乐观更新 → API 同步（失败回滚）；头部"♥ 我的收藏"按钮切换收藏模式；`_favIds` 数组驱动 UI 标记
- 竞态控制：三轮询互斥，reset 清除旧 interval

### Python 约定

- Python 3.11+，每模块一个文件；**必须用 AsyncOpenAI**，V4 关掉 thinking
- 网络调用 try/except 保护；`publish_time` 从 API 传递，不依赖 LLM
- 新入库自动过滤 >30 天；每批完成后 `reevaluate_all()`
- `process_listing_item()` 为 pipeline/deep_dive 共享入口；DB 连接 `try/finally` 保护
- SQL 拼接优先参数化；前端定时器 ID 追踪 + `clearTimeout` 防竞态
- 速率限制空桶先写时间戳再返回，`threading.Lock` 保护临界区
