---
name: platform-strategy
description: 各中文租房平台的数据抓取可行性和风险决策
metadata:
  type: project
---

# 平台抓取策略

## 当前活跃

| 平台 | 抓取方式 | 状态 |
|------|---------|:--:|
| 豆瓣 | Firecrawl 云 API（SSR，无需 JS） | ✅ |
| 闲鱼 | 宿主机 Playwright（需登录 cookie） | ✅ |
| 58同城 | HTTP（已失效，2026-07 新增反爬） | ❌ |

## 已验证不可行

| 平台 | 原因 |
|------|------|
| Wellcee | SPA，Firecrawl 无法触发数据加载 |
| 赶集网 | 重定向到 58同城 |
| Zuber | 当前环境不可达 |

## 法律风险禁止

| 平台 | 风险 |
|------|------|
| 贝壳找房 | 2022 年已有判例（罚款 500 万） |
| 安居客 | 2026-02 四川高院判例，涉及《反不正当竞争法》 |

## 技术风险禁止

| 平台 | 原因 |
|------|------|
| 小红书 | 签名月更，IP 分钟级封禁 |
| 抖音 | 账号存活 3.7 小时，搜索不稳定 |

## 闲鱼 Playwright 架构

- 首次: `--login` 扫码保存 cookie
- 之后: 自动搜索 → 提取 data-itemid → 抓详情页 → 写 JSON
- 日志走 stderr，JSON 走 stdout/文件
- 容器通过 `data/xianyu.json` → `/api/import` 入库
- 容器不装 Playwright（缺 libnspr4 等系统库），抓取跑在宿主机

**Why:** side project 预算有限，优先稳定低风险平台。贝壳/安居客的法律判例是硬边界。

**How to apply:** 新增平台前对照此表。如需添加新源，优先找 SSR 页面或已有 API 的平台。
