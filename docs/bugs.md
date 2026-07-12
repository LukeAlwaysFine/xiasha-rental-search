# Bug 记录与修复

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
