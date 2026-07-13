# 后端架构

## 技术栈

- **抓取**: Playwright（闲鱼 MTOP 拦截 + 微博 Ajax）+ httpx + BS4（豆瓣 SSR 直连）
- **提取**: DeepSeek V4 Flash（AsyncOpenAI，thinking=disabled，40 并发）
- **地理编码**: 高德 API 多策略重试 + Input Tips 兜底（每日上限 30 次，月配额仅 5000）；批量补齐（地址去重 + 2并发 + write_lock）。候选地址只追加命中区名。闲鱼 MTOP 自带 GPS 覆盖 99.8%
- **数据库**: SQLite WAL 模式，UPSERT 零值覆盖 + `COALESCE` 回填。favorites 表 FK 关联 listings，级联删除
- **后端**: FastAPI + APScheduler（6h URL 清理 + 6h poster 主页检测 + 1h 中介重评（纯SQL））

## 数据流

```
搜索 → search_and_fetch → SQLite 缓存（三级降级）→ 手动抓取补数据
→ AsyncOpenAI 并行提取（40并发，is_rental 默认 false）
→ API 坐标优先（闲鱼 MTOP 6 路径探测）→ 高德 geocode 兜底
→ LLM+Regex 混合中介检测 → 入库 → batch_geocode_missing + reevaluate_all（1h SQL 批量修正）
→ poster 主页检测（6h，Playwright 进闲鱼主页数出租房+总物品数，四档判定）
→ LLM 重排/去重 → 前端地图 + 列表
→ 抓取成功后自动链式 poster 检查（新增>0时触发，2s延迟）
```

## 闲鱼抓取

Playwright 拦截 MTOP API，从 JSON 直接提取结构化字段。MTOP 分页因 sign 校验不可用，策略：**~165 个下沙专项关键词 × ~30 条/词**。

绕过 LLM 的字段：`poster_id`（userNickName）、`api_address`（杭州+area+location）、`api_images`（多字段名合并去重）、`api_lng/api_lat`（6 路径探测，覆盖率 99.8%）。首次 MTOP 响应 dump 所有层级 keys 含 `exContent`，便于排查字段缺失。

⚠️ 反爬：headless 触发 CAPTCHA，依赖首次导航的 API 拦截窗口期。导航用 `wait_until="domcontentloaded"` 超时 15s。

## 中介检测（LLM + Regex 混合）

LLM 提取时同步分析 agent_signals/confidence/reasoning（零额外调用），`detect_with_llm()` 混合判定：
- 评分：品牌名 +8 / 话术 +3 / 名称关键词 +4 / 同 poster ≥3 条 +6 / 同 contact ≥2 条 +6 / MTOP 卖家房源数 ≥3 → +10
- 模板标题正则：纯结构描述+无个人语言 → "疑似中介"
- 判定：≥6 中介 / <6 疑似中介。**LLM 仅辅助判断中介可疑度，不作"个人"判断** — 只有 Playwright 进主页核实后才标"个人"

**Poster 主页检测**（6h 定时 + 前端手动触发）：Playwright 进闲鱼主页数出租房+总物品数。**5 并发 tab（Semaphore + asyncio.gather），~6min 完成 500+ poster**。判定：≥3→中介，2→疑似中介，1+无其他→疑似中介，1+有其他→个人。

`reevaluate_all()` 每小时 SQL 批量修正：同 poster ≥3 / 同 contact ≥2 / 名称 LIKE。

## 数据入库管线

`fetch_new_listings()` → `process_listing_item()`（pipeline/deep_dive 共享）→ `extract_listing()` → `_is_valid_rental()` → API 坐标优先 / API 地址覆盖 → geocode 兜底 → `detect_with_llm()` + upsert → `batch_geocode_missing()` + `reevaluate_all()`。

过滤：`_NON_RENTAL_TITLE_RE`（30+ 非居住关键词），价格 ¥300-50000。过期：`_is_too_old()` 支持 8+ 种日期格式。豆瓣详情 2 次重试。

## 数据库

单表 `listings`，`source_url` UNIQUE。搜索三级降级 + 分页。排序：`_llm_score` > 面积 +3 > 户型 +3 > 个人 +5 > 超 7 天 -10。UPSERT 用 `CASE WHEN IS NULL OR = 0` 防 LLM 幻觉零值锁定，`poster_id`/`contact` 用 `COALESCE` 回填空值。

`favorites` 表 `listing_id` UNIQUE，FK 关联 listings 级联删除。

## API 路由

| 路由 | 说明 |
|------|------|
| `GET /` | 前端单页，注入 `AMAP_JS_KEY` |
| `GET /api/search` | 搜索（多维度筛选 + 距离排序 + `favorites_only`） |
| `GET /api/search/enhance` | LLM 增强结果缓存（TTL 120s） |
| `GET /api/listing/{id}` | 单条详情 |
| `GET /api/geocode` / `/api/inputtips` | 地址→坐标 / 输入提示 |
| `GET /api/fetch` / `/api/fetch/status` | 手动抓取（409 防重入）/ 状态轮询 |
| `POST /api/poster-check` / `/api/poster-check/status` | 中介检测 / 状态轮询 |
| `POST /api/import` | 批量导入（2/min） |
| `GET /api/favorites` / `/ids` | 收藏列表 / 仅 ID |
| `POST /api/favorites/{id}` | 添加收藏 |
| `DELETE /api/favorites/{id}` | 取消收藏 |

速率限制：token bucket 按 endpoint 分离（search 30/min, fetch/import 2/min），60s 窗口，`threading.Lock` 保护临界区。

## 模块结构

```
src/
  crawler/       xianyu_async.py / douban_http.py / weibo_crawler.py / xhs_crawler.py / deep_dive.py
  extractor/     llm_extract.py      # is_rental 默认 false
  geocode/       amap.py             # 多策略重试 + Input Tips + 批量补齐(write_lock)
  detector/      agent_detector.py   # R3 参数化 LIKE + 反模式排除 + 默认"疑似中介"
  filter/        llm_rerank.py / llm_dedup.py
  db/            schema.py           # 三级降级 + UPSERT 零值覆盖 + COALESCE 回填
  api/           server.py           # 速率限制 + inputtips缓存
  web/           index.html          # 前端单页
```

## 平台可行性

| 闲鱼 ✅ | 豆瓣 ✅ | 微博 ✅ | 小红书 🔴 反爬 | 58 ❌ | 贝壳 🔴 法律风险 |

## Python 约定

- Python 3.11+，每模块一个文件；**必须用 AsyncOpenAI**，V4 关掉 thinking
- 网络调用 try/except 保护；`publish_time` 从 API 传递，不依赖 LLM
- 新入库自动过滤 >30 天；每批完成后 `reevaluate_all()`
- `process_listing_item()` 为 pipeline/deep_dive 共享入口；DB 连接 `try/finally` 保护
- SQL 拼接优先参数化；速率限制空桶先写时间戳再返回，`threading.Lock` 保护临界区
