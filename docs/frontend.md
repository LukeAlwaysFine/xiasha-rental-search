# 前端架构

## 技术栈

单页 HTML + 高德 JS API 2.0 + taste-skill 设计体系。无框架，纯 vanilla JS。

## UI 设计

taste-skill 体系：珊瑚红 `#ff6b6b`，深蓝灰 `#1a1a2e`。Geist 字体。圆角 12px。阴影 `0 1px 3px rgba(0,0,0,.06)` → hover `0 8px 24px rgba(0,0,0,.12)`。动效 `cubic-bezier(.4,0,.2,1)`，支持 `prefers-reduced-motion`。

暗色模式：`data-theme="dark"` CSS 变量覆盖所有颜色，`localStorage` 持久化，优先跟随系统 `prefers-color-scheme`。Header 右侧 🌙/☀️ 切换按钮。地图始终保持原始样式不受主题影响。

## 筛选面板

- 关键词输入 + 价格区间 + 户型/租类/房东类型/平台/时间芯片分组
- 房东类型：`个人` / `中介` / `疑似中介` 三个选项
- 所有筛选点击即搜索（300ms debounce，搜索按钮立即触发）；芯片 `data-group` 分组匹配
- 一键清除筛选 + `grid-template-rows` 折叠动画 + 折叠状态 localStorage 记忆
- 无坐标房源 SQL `CASE WHEN lng IS NULL THEN 1` 排最后

## 地图

- 初始聚焦下沙 `[120.38, 30.31]` zoom 15
- 全量 markers（≤1000）网格聚类（~16m）
- 6 层暖→冷六色渐变距离圈（200m-5km）
- AMap.Scale 比例尺；金色参考标记（📍）；距离模式 zoom 14 聚焦通勤点
- 三人色标记：个人=绿 `#2ed573` / 中介=红 `#ff4757` / 疑似中介=蓝 `#3742fa`
- 图例：个人房东 / 中介 / 疑似中介
- 标记三层覆盖：DB 预存 → 批量 geocode（2并发+逐条 gen 检查）→ 点击 fallback（`_offsetCoord` 去重叠）

## 房源列表

- 骨架屏加载：3 个脉冲动画骨架卡片替代 spinner
- 卡片标签：个人/中介/疑似中介/转租/平台，颜色区分
- 排序：`_llm_score` > 面积 +3 > 户型 +3 > 个人 +5 > 超 7 天 -10
- 无坐标排最后

## 收藏功能

- 卡片圆形 ♥ 按钮乐观更新 → API 同步（失败回滚）
- 头部 "♥ 我的收藏" 按钮切换收藏模式
- `_favIds` 数组驱动 UI 标记

## 操作按钮

- Header「🔄 抓取房源」手动触发 +「🔍 检查是否中介」主页检测
- 状态栏实时进度 + 预计剩余 + 完成后顶部持久化记录（时间戳 + 耗时 + 结果）
- 抓取成功自动链式 poster 检查（新增>0时，2s延迟）
- 状态栏 5s 后自动隐藏（`_posterCheckPollId` 主动清理防竞态）

## 详情面板

- 点击卡片 → 右侧抽屉滑入（桌面 420px）/ 底部弹出（移动端 80vh）
- 图片画廊横向 scroll-snap、基本信息双列 grid、AI 分析、联系方式、收藏同步
- 关闭：✕ / 遮罩 / Escape。加载骨架屏 + 错误重试
- AbortController 防重复请求；与 `toggleFav` 双向同步收藏状态

## 地址搜索

`/api/inputtips` 下拉 + localStorage 历史（存储 200 条，显示 10 条，输入时优先本地匹配省 API）。可逐条 ✕ 删除。输入框内 ✕ 清除按钮；服务端 24h 缓存。

## 移动端

<900px：浮动 🗺️/📋 按钮切换全屏地图/列表。

## 安全 & 无障碍

- `escapeHtml`/`escapeAttr` 剥离控制字符
- `safeUrl` 白名单阻止伪协议，外链 `rel="noopener"`
- `:focus-visible` 焦点环，交互元素 `aria-label`，时间标签颜色 ● 圆点
- Toast 通知：右上角 `success`/`error`/`info`，3s 自动消失

## 竞态控制

- 三轮询互斥，reset 清除旧 interval
- 前端定时器 ID 追踪 + `clearTimeout` 防竞态
