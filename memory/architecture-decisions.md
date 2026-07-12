---
name: architecture-decisions
description: 核心架构决策：搜索降级、分页、预热、导入端点
metadata:
  type: project
---

# 架构决策记录

## 搜索三级降级

`src/db/schema.py:search_listings()` 实现：

1. **精确匹配** — 关键词完整匹配 title/address
2. **宽松匹配** — 拆成单字 OR 匹配（过滤常见停用词）
3. **无关键词** — 去掉关键词条件，保留其他筛选

确保用户搜任何词都不会返回空结果。无关键词时直接跳到第 3 级。

## 分页加载

前端 `PAGE_SIZE=20`，API 支持 `offset` 参数。用户滚动到底部点击"加载更多"追加数据（不是替换）。搜索时重置 offset，追加时累加。

## 启动预热

服务器启动后 3 秒自动触发 `fetch_new_listings(limit_per_source=50)`，一次性大量抓取填充缓存。之后每 2 小时增量更新（`limit_per_source=10`）。

## 定向抓取

用户搜索时，关键词会传给 `fetch_new_listings(keyword_hint=...)`，抓取器用这个关键词替换默认的"杭州租房"进行定向搜索。这样搜"滨江"就能抓到滨江的房源。

## 导入端点

`POST /api/import` — 宿主机闲鱼数据入库。接受 `[{url, source, content}]`，并行 LLM 提取 + geocode + 检测后写入数据库。也支持文件导入（`data/xianyu.json`，容器定时任务自动检测导入）。

## 开发环境

- 开发在容器（限制文件系统访问，安全）
- 部署在宿主机（完整环境，Playwright 可用）
- 容器不装 Playwright

**Why:** 这些决策来自实际使用中的问题——搜索返回空结果、导入太慢、冷启动无数据。每个都解决了具体痛点。

**How to apply:** 新增功能时参考这些模式。分页用 offset/limit，搜索用三级降级，批量操作用 asyncio.gather + semaphore。
