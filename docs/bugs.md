# Bug 记录与修复

## 2026-07-12：关键词矩阵优化 — 聚焦下沙、扩大覆盖

### 现象

80 个关键词中 24/25 区域与下沙无关（临平、闲林、良渚…），类型仅 3 个（租房/转租/整租），缺少合租、单间、一室、两室、公寓等高频词，没有任何小区/公寓名直搜。大量 poster 在 DB 中仅 1 条记录。

### 修复

**完全重写关键词生成**（`src/crawler/xianyu_async.py`）：
- 区域：25 个杭州各区 → 15 个下沙子区域（金沙湖、高沙、文泽路、下沙江滨…）
- 类型：3 → 8（+合租/单间/一室/两室/公寓）
- 新增 40 个核心小区/公寓直搜（伊萨卡国际城、世茂江滨花园…）
- 总量：80 → 165 个关键词

**配套优化**：
- poster check 移除上限 + 超时，每次扫全部 <3 条 poster，`ORDER BY RANDOM()`
- 进度条显示预计剩余时间（`已耗时/已完成 × 剩余`）
- 操作状态栏持久化（localStorage），成功/失败均记录，页面刷新恢复
- 按钮文案：「🔄 抓取房源」「🔍 检查是否中介」
- 抓取成功后自动链式执行中介检查（新增>0时，2s延迟）
- 页面刷新恢复检测：操作进行中恢复进度条，刚好完成补记录

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/crawler/xianyu_async.py` | `_DISTRICTS` + `_RENT_TYPES` + `_TOP_COMPOUNDS` 重写；移除 `max_posters`；加 `ORDER BY RANDOM()`；返回 `total` |
| `src/api/server.py` | `_poster_check_status` 新增 `total`；调用移除 `max_posters` |
| `src/web/index.html` | 按钮重命名 + 状态栏持久化 + 剩余时间估算 |
| `CLAUDE.md` | 关键词数 199→165，按钮文案同步 |

## 2026-07-12：Poster 扩展解耦 — 从抓取流程中拆分独立定时任务

### 现象

Poster 扩展（进闲鱼主页数出租房）嵌在抓取流程里，每批最多检查 8 个 poster。80 个关键词产出上百个不同 poster，绝大多数漏检。320 个单条 poster 标记为"个人"实际可能是中介。

### 根因

抓取和中介检查耦合在同一次 HTTP 请求（超时 480s），每个 poster 主页检查 ~20s，无法大量检查。

### 修复

**解耦为独立任务**：
- 从 `crawl_xianyu_via_api()` 移除 poster 扩展代码
- 提取为独立函数 `check_posters_for_agents()`，创建自己的 Playwright browser
- 新增 APScheduler 定时任务：6h，扫描全部 DB 记录 <3 条的 poster（`ORDER BY RANDOM()`）
- 新增 API：`POST /api/poster-check`（手动触发，409 防重入），`GET /api/poster-check/status`（含 total/checked 进度）
- 前端 Header 新增 "🔍 检查是否中介" 按钮，显示预计剩余时间 + 进度 + 完成记录持久化

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/crawler/xianyu_async.py` | 移除 poster 扩展代码块，新增 `check_posters_for_agents()` 独立函数 |
| `src/api/server.py` | 新增 `scheduled_poster_check` 定时任务 + 2 个 API 端点 |
| `src/web/index.html` | 新增按钮 `posterCheckBtn` + `triggerPosterCheck` 等 JS 函数 |

## 2026-07-12：中介漏判 — 单条 poster 跨房源规则失效 + Poster 扩展抓取

### 现象

`t桃年11`、`西汉守时的使者` 等 poster 在闲鱼上发布了大量出租房源，但库里只抓到 1 条，被判为"个人"。

### 根因

三层问题叠加：

1. **爬虫覆盖面不足** — MTOP 分页不可用，80 个关键词（后优化至 165）× ~30 条/词 只能覆盖部分房源。451 个 poster 在库中仅 1 条记录，跨房源规则 `COUNT(DISTINCT source_url) >= 3` 无从触发。

2. **模板标题正则太窄** — `TITLE_TEMPLATE_RE` 强制要求 `\d{2,4}方`（面积数字），但大量中介标题不含面积。如 `杭州下沙大学城北碧桂园精装两室` 无法命中模板检测，"两" 也不在数字字符集 `[一二三四五六七八九十]` 中。

3. **没有利用闲鱼用户主页** — 系统只能在本地 DB 内统计同 poster 数量，不会去闲鱼主页查看实际发布量。

### 修复

**模板标题正则放宽**（`src/detector/agent_detector.py`）：
- `\d{2,4}方` → `(?:\d{2,4}方)?`（面积可选）
- 数字字符集添加 `两`，租赁术语添加 `租房`
- 效果：`杭州下沙大学城北碧桂园精装两室` → 命中模板 → 判"未知"

**Poster 扩展**（`src/crawler/xianyu_async.py`）：
- 主关键词爬完后，对 DB 中 ≤2 条的 poster，用 Playwright 模拟人工操作：
  1. 打开一条他的帖子 → 找 `/personal?userId=` 链接
  2. 进主页 → 数 `<a href="/item?id=">` 链接
  3. 用关键词（`/月`、`整租`、`合租`、`一室`、`㎡` 等）判断是否为出租房
  4. 出租房 ≥3 套 → 直接标中介
- 已验证：`般若星泡菜味的法夏`（20/20 出租→中介），`通天代建模设计工作室`（1/20 出租→个人）
- 每轮最多查 8 个 poster，超时 15s/个

**`reevaluate_all` 频率提升**（`src/api/server.py`）：6h → 1h

**平台级卖家房源数信号**（`src/detector/agent_detector.py`）：
- `detect_with_llm` 新增 `seller_item_count` 参数
- 如果 MTOP API 直接返回卖家总房源数且 ≥3 → +10 分 → 直接中介

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/detector/agent_detector.py` | 模板标题正则放宽 + `seller_item_count` 参数和评分 |
| `src/crawler/xianyu_async.py` | Poster 扩展（Playwright 模拟点主页）+ MTOP userId 提取 + 字段探测定 |
| `src/pipeline.py` | 透传 `seller_item_count` |
| `src/api/server.py` | `reevaluate_all` 间隔 6h→1h，fetch 超时 180s→480s |

### 小修复（审查报告 P1/P2）

| 编号 | 问题 | 文件 |
|------|------|------|
| H3 | API 错误消息泄露 `str(e)` | `server.py` |
| M5 | 通勤下拉框 Enter 键被吞 | `index.html` |
| M6 | `escapeAttr` 不转义单引号 | `index.html` |
| M7 | Chip 缺少 `aria-pressed` | `index.html` |
| M8 | `LLM_CONCURRENCY` 非整数崩溃 | `llm_reeval.py` |
| M10 | `safeUrl` 双重控制字符清理 | `index.html` |

## 2026-07-12：详情面板死代码 — C1 严重缺陷

### 现象

点击房源卡片无任何反应。房源详情只能通过地图标记间接查看。

### 根因

`handleCardClick(id)` 搜索 `document.getElementById('detail-' + id)`，但 `renderResults` 从未创建该 ID 的元素。整个 `.detail-panel` CSS 块和旧 `handleCardClick` 函数都是死代码。后续降级修复改为了切换卡片内 `.detail-info`（仅显示排序理由），但 `.detail-info.open` 无 CSS 规则，展开效果为零。图片从未在 UI 展示。

### 修复

实现完整的详情抽屉面板（仅 `src/web/index.html` ~360 行新增）：

- **交互**：桌面端右侧 420px 抽屉滑入（`translateX`），移动端底部 80vh 弹出（`translateY`）
- **内容**：图片画廊（横向 scroll-snap，空时虚线占位）、基本信息双列 grid、标签、AI 分析（`_llm_reason`）、联系方式+发布者、发布时间
- **操作**：♥ 收藏（与 `toggleFav` 双向同步）+ 查看原帖
- **关闭**：✕ 按钮 / 遮罩点击 / Escape 键
- **状态**：骨架屏 → 内容 → 错误+重试（AbortController 防重复请求）
- **暗色模式 + `prefers-reduced-motion`** 适配
- **集成点**：`handleCardClick`、集群 marker 点击、`highlightMarker` 动态卡片插入、`toggleFav` 收藏同步

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/web/index.html` | 删除无用 CSS + 新增 ~250 行 CSS + ~110 行 JS + HTML 容器。后端无需修改。 |

## 2026-07-12：UI 优化 — 交互 Bug 修复

### 地址下拉菜单被裁切
- **现象**：通勤地址输入框的历史记录/自动补全下拉被筛选面板裁切，无法完整显示
- **根因**：filter-body 改为 grid 动画后添加了 `overflow: hidden`，下拉菜单的 `position: absolute` 被 clip
- **修复**：去掉 filter-body 的 `overflow: hidden`，`grid-template-rows: 0fr` 本身已能隐藏折叠内容

### 历史记录删除 ✕ 无反应
- **现象**：点击下拉菜单中历史记录右侧的 ✕ 按钮没有效果
- **根因**：`_deleteHistory()` 定义在模块级作用域，而其依赖的 `_showHistory()` 在 `setupCommuteAutocomplete` 闭包内，跨作用域调用导致 ReferenceError
- **修复**：将 `_deleteHistory` 移入 `setupCommuteAutocomplete` 闭包内

### 历史记录删除后列表不刷新
- **现象**：删除最后一条历史后，下拉菜单仍显示旧内容
- **根因**：`_showHistory()` 在 `hist.length === 0` 时直接 `return`，未清空 `dropdown.innerHTML`
- **修复**：空历史时主动清空 dropdown 并隐藏

### 地址输入框 ✕ 按钮不显示
- **现象**：程序设置输入框值后 ✕ 清除按钮不出现
- **根因**：仅靠 `oninput` 事件切换按钮显示，`input.value` 程序赋值不触发 `oninput`
- **修复**：改用 CSS class `.commute-wrap.has-value .commute-clear { display: flex }`，所有设置值的路径同步 toggle class

### 地址历史记录保存用户输入文字而非 Amap 地址
- **现象**：`selectTip()` 保存 `input.value`（用户原始输入）而非高德返回的地址名
- **修复**：改为 `_saveHistory(tip.name)`

### 再次点击地址输入框不显示历史
- **现象**：输入框已聚焦时再次点击不出现下拉
- **修复**：同时监听 `click` 和 `focus` 事件触发 `_showHistory()`

## 2026-07-12：hours_ago 过滤失效 — 非 ISO publish_time 导致 SQL 字符串比较错误

### 现象

筛选「个人」+「24小时内」，出现 87 天前的房源（如 id=7547 `Wed Apr 15 15:04:10 +0800 2026`）。

### 根因

微博爬虫输出的 `publish_time` 是 HTTP date 格式（`Wed Apr 15 15:04:10 +0800 2026`），与其他平台的 ISO 8601 不一致。`hours_ago` 过滤用 SQL 字符串比较 `publish_time >= '2026-07-11T...'`：

- ISO 8601：字典序 = 时间序 → 正确
- HTTP date：`'W'` (ASCII 87) > `'2'` (ASCII 50) → **永远 TRUE**

结果：所有非 ISO 格式的房源无视 `hours_ago` 过滤，全部通过。旧至 2022 年的房源也能出现在"24小时内"。

附加问题：
- `_is_too_old()` 虽能解析 HTTP date 格式，但部分旧数据是之前没有 age check 时入库的遗留
- server.py 的 `_import_listings` 路径完全没有 publish_time 归一化和 age check
- 微博爬虫没有在源头归一化格式

### 修复

**`pipeline.py:375`** — 新增 `_normalize_publish_time()` 函数，HTTP date → ISO 8601：

```python
def _normalize_publish_time(publish_time):
    # Wed Apr 15 15:04:10 +0800 2026 → 2026-04-15T07:04:10+00:00
```

**`pipeline.py:60` + `server.py:622`** — 入库前调用归一化 + age check：

```python
extracted["publish_time"] = _normalize_publish_time(extracted.get("publish_time"))
if _is_too_old(extracted.get("publish_time")):
    return None
```

**一次性 DB 迁移**：13 条非 ISO 格式归一化，88 条过期（>30 天）硬删除。

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/pipeline.py:375-402` | 新增 `_normalize_publish_time()` |
| `src/pipeline.py:60` | 入库前归一化 + age check |
| `src/api/server.py:23` | import `_normalize_publish_time`, `_is_too_old` |
| `src/api/server.py:622-625` | 导入路径归一化 + age check |

## 2026-07-12：中介漏判 — poster_id 缺失导致跨房源规则静默失效

### 现象

「杭州下沙1300个人急转租」标记为"个人"，但点击发布者主页有大量房源，明显是中介。同类漏判共 80 条（poster_id + contact 同时为空）。

### 根因

**闲鱼爬虫未将 poster_id 作为结构化字段传递**。MTOP API 返回的 `userNickName` 只嵌入 content 文本（`"发布者: {poster}"`），不在 `all_items` dict 中作为独立字段。LLM 提取失败时 poster_id=NULL，导致三条核心规则全部跳过：

- 同 poster ≥3 条 → SKIP（`if poster_id` 为 false）
- poster 名称关键词检测 → SKIP
- 同 contact ≥2 条 → SKIP（contact 也为空）

`detect_with_llm()` hybrid_score=0 → 判为"个人"。

附加问题：`reevaluate_all()` R1 阈值被悄悄从 3 提到 5，且用 `COUNT(*)` 而非 `COUNT(DISTINCT source_url)`，与 `detect_with_llm` 不一致。

### 修复

**`xianyu_async.py:376`** — item dict 增加 `"poster_id": poster` 结构化字段：

```python
all_items.append({
    ...
    "poster_id": poster,  # API 直接提取的发帖人，用于中介检测
    ...
})
```

**`pipeline.py:63-66` + `server.py:622-625`** — API poster_id 作为 LLM 提取的 fallback：

```python
api_poster = item.get("poster_id", "")
if not extracted.get("poster_id") and api_poster:
    extracted["poster_id"] = api_poster
```

**`agent_detector.py:266-271`** — R1 阈值恢复为 ≥3，用 `COUNT(DISTINCT source_url)`：

```sql
GROUP BY poster_id HAVING COUNT(DISTINCT source_url) >= 3
```

**`pipeline.py:97-100` + `server.py:643-646`** — API 结构化地址覆盖 LLM 提取：

```python
api_addr = item.get("api_address", "")
if api_addr and "杭州" in str(api_addr) and len(str(api_addr)) > 3:
    extracted["address"] = api_addr
```

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/crawler/xianyu_async.py:376` | item dict 增加 `poster_id` 字段 |
| `src/pipeline.py:63-66` | API poster_id fallback + API 地址覆盖 |
| `src/api/server.py:622-625,643-646` | 同上（导入路径） |
| `src/detector/agent_detector.py:266-284` | R1 阈值 5→3，R1/R2 用 `COUNT(DISTINCT)` |

## 2026-07-12：房源过滤与地图范围修复

### 现象

1. **地图默认范围过大**：初始加载时地图视野覆盖整个杭州，而非聚焦下沙
2. **"个人"筛选后全城散点**：点击「个人」筛选，地图上出现滨江、上城、萧山等下沙之外的房源；取消筛选后这些房源消失
3. **marker 视野不一致**：切换筛选条件后地图 zoom 级别突变

### 根因

三个独立问题叠加：

1. **地图初始中心偏离** — `initMap()` center 为 `[120.35, 30.31]`（杭州市中心），zoom=14，未聚焦下沙核心区域 `[120.38, 30.31]`

2. **fitView 时机错误** — `search()` 中两阶段 marker 渲染：
   - `renderResults` → `renderMarkers(50点, fitView=true)` → 视野拟合到 50 个点
   - `search()` → `renderMarkers(200点, fitView=false)` → 替换为 200 点但不重新拟合视野
   - 结果：显示 200 个 markers，视野却锁定在 50 个点的范围

3. **搜索无地域限制** — `search_and_fetch()` 中 keyword 为空时，SQL 退化为 `WHERE 1=1`，返回全杭州所有房源。默认综合排序下，下沙优质房源（有图、有面积）评分高占据前 50，非下沙房源被挤到后面。但筛选「个人」后只剩 465 条竞争，非下沙个人房源（滨江 27 条、上城 44 条等）进入 top 50，导致地图散到全城

### 修复

**`src/pipeline.py:429`** — 默认关键词聚焦下沙：

```python
if not filters.get("keyword", "").strip():
    filters["keyword"] = "下沙"
```

**`src/web/index.html:523`** — 初始中心移向下沙核心：

```javascript
map = new AMap.Map('map', { zoom: 15, center: [120.38, 30.31], resizeEnable: true });
```

**`src/web/index.html:854,642`** — fitView 切换到 200 点全量数据：

```javascript
// renderResults 内：不再 fitView（交给 200-point 加载统一处理）
renderMarkers(geoPoints, reset, false);

// search() 内：200-point 加载统一 fitView
renderMarkers(allPoints, true, true);
```

附带修复：
- 地图 marker 上限从 200 提升到 1000（前端 `mapLimit` + API `le=1000` + DB `min(limit, 1000)`），默认加载全部 ~516 条下沙房源
- 网格聚类（~16m）自动合并密集楼盘，性能无影响

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/pipeline.py:429` | `search_and_fetch` 无关键词时默认 `"下沙"` |
| `src/web/index.html:523` | 初始 center `[120.38, 30.31]`，zoom `15` |
| `src/web/index.html:854` | `renderResults` 内 `renderMarkers` fitView=false |
| `src/web/index.html:642` | 200-point `renderMarkers` fitView=true |
| `src/web/index.html:630` | `mapLimit` 200→1000 |
| `src/api/server.py:330` | Query `le=200` → `le=1000` |
| `src/db/schema.py:241` | `min(limit, 200)` → `min(limit, 1000)` |

## 2026-07-12：地图不显示——CSP 拦截高德 SDK

### 现象

前端地图只显示价格 marker 和距离圈，高德地图底图（瓦片）完全不显示。控制台大量 CSP violation 报错。

### 根因

`src/api/server.py` 中的 CSP（Content Security Policy）安全头过于严格，阻止了高德 JS API v2.0 的关键资源加载。

三个独立问题叠加：

1. **`'unsafe-eval'` 缺失** — 高德 SDK 内部使用 `eval()` 初始化 `DomRender` 和 `AMap.Scale` 等插件。CSP `script-src` 无此指令 → 插件初始化失败 → 瓦片图层不创建。

2. **`_AMapSecurityConfig` 未配置** — 高德 JS API v2.0 要求在加载 SDK 脚本**之前**设置 `window._AMapSecurityConfig.securityJsCode`，否则瓦片请求被服务端拒绝。`index.html` 中缺少此配置。

3. **高德子域名未加入白名单（根因）** — CSP 中遗漏了大量高德子域名：
   - 矢量瓦片 CDN：`jsapi-data[1-5].amap.com`（真实浏览器用 Canvas2D + fetch 加载 PBF 矢量瓦片）
   - 插件 CDN：`jsapi-service.amap.com`
   - 初始化 API：`jsapi.amap.com`
   - 日志/统计：`restapi.amap.com`、`vdata.amap.com`

   **关键差异**：headless Chrome 回退到栅格瓦片（`<img>` 加载 `webrd*.is.autonavi.com`），测试时能正常渲染；真实浏览器使用矢量瓦片（`fetch()` 加载 `jsapi-data*.amap.com`），被 CSP 拦截。两者渲染路径不同导致问题只在真实浏览器复现。

### 修复

**`src/api/server.py`**（line 305-312）— CSP 放开高德全量域名：

```python
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://*.amap.com; "  # +'unsafe-eval', +*.amap.com
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' https: data: blob:; "
    "connect-src 'self' https://*.amap.com https://*.is.autonavi.com; "     # +*.amap.com, +*.is.autonavi.com
    "worker-src 'self' blob:"
)
```

变化：
- `script-src`：添加 `'unsafe-eval'`，所有高德子域名收敛为 `https://*.amap.com`
- `connect-src`：收敛为 `https://*.amap.com https://*.is.autonavi.com`，覆盖矢量瓦片、栅格瓦片、API 等所有后端通信

**`src/web/index.html`**（line 10）— 加载 SDK 前注入安全密钥：

```html
<script>
  window._AMapSecurityConfig = {
    securityJsCode: '{{AMAP_SECURITY_CODE}}'
  };
</script>
<script src="https://webapi.amap.com/maps?v=2.0&key={{AMAP_JS_KEY}}"></script>
```

附带修复：
- 默认 zoom 12→14，地图更清晰
- 3 处 `e.target.closest()` 调用添加 `typeof ... === 'function'` 守卫，防止 Amap SVG 元素触发 `TypeError`

### 调试踩坑

- headless Chrome 用栅格瓦片渲染（PNG `<img>`），真实浏览器用矢量瓦片渲染（PBF `fetch()` + Canvas2D）。在 headless 中测试通过不代表真实浏览器没问题。
- 诊断代码需同时检测 `<img>`（栅格瓦片）和 `<canvas>`（矢量瓦片）两种渲染路径，否则会误报 "NO TILES"。
- CSP `*.amap.com` 通配符写法比逐个枚举子域名更健壮，避免高德新增 CDN 节点时再次被拦截。

### 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `src/api/server.py:305-312` | CSP 安全头：添加 `unsafe-eval`，收敛为 `*.amap.com` 通配 |
| `src/web/index.html:10-15` | 添加 `_AMapSecurityConfig` 安全密钥配置 |
| `src/web/index.html:523` | 默认 zoom 12→14 |
| `src/web/index.html:1469-1515` | `closest()` 类型守卫（3 处） |
