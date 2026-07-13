# 部署 & 环境

## 常用命令

```bash
python3 run.py [port]                  # 启动服务 (默认 8000)
python3 scripts/deep_dive.py --area 下沙  # 下沙深潜抓取
python3 -m src.cleanup.url_checker --dry-run / --batch 50
python3 -m src.crawler.xianyu_async --login  # 首次扫码登录
python3 -m src.detector.llm_reeval --sample 50
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

## 容器环境

Chromium 依赖预装至 `/tmp/chromium-libs/lib`，启动前设置：
```bash
export LD_LIBRARY_PATH=/tmp/chromium-libs/lib:$LD_LIBRARY_PATH
```

## .gitignore 要点

```
.env                    # 环境变量（含 API Key）
__pycache__/ *.py[cod]  # Python 编译产物
*.db *.db-* *.sqlite3   # 数据库 + WAL 辅助文件
.idea/ .vscode/         # IDE 配置
```
