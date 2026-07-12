"""启动开发服务器。

Usage:
    python run.py          # 默认 127.0.0.1:8000
    python run.py 9000     # 自定义端口

前置条件: .env 里配好 AMAP_API_KEY 和 LLM_API_KEY
"""

import sys
import os

# 加载 .env
from dotenv import load_dotenv
load_dotenv()

# Playwright Chromium 依赖库路径（容器环境无 root 权限时的手动安装）
_LIB_DIR = "/tmp/chromium-libs/lib"
if os.path.isdir(_LIB_DIR):
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{_LIB_DIR}:{current_ld}" if current_ld else _LIB_DIR

# 检查关键配置
missing = []
if not os.getenv("AMAP_API_KEY"):
    missing.append("AMAP_API_KEY (高德地图)")
if not os.getenv("LLM_API_KEY"):
    missing.append("LLM_API_KEY (DeepSeek)")
if missing:
    print(f"⚠️  缺少配置: {', '.join(missing)}")
    print("   请编辑 .env 文件填入 API Key")
    print("   搜索和地图功能将不可用，但你可以先看界面\n")

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000


if __name__ == "__main__":
    import uvicorn
    reload = os.getenv("UVICORN_RELOAD", "true").lower() in ("1", "true", "yes")
    uvicorn.run("src.api.server:app", host="127.0.0.1", port=port, reload=reload)
