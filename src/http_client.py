"""共享 HTTP 客户端 — 复用连接池，避免每次请求新建 TCP+TLS 连接。

所有 Firecrawl 和高德 API 调用共享一个模块级 httpx.AsyncClient，
配置连接池限制，在 FastAPI lifespan 中统一关闭。
"""

import httpx
import atexit

# 连接池配置
_limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
_timeout = httpx.Timeout(30.0)

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """获取共享的 httpx.AsyncClient。

    自动创建并注册 atexit 清理。也在 FastAPI app lifespan 中调用 close_http_client()。
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_timeout, limits=_limits)
    return _client


async def close_http_client():
    """关闭共享 HTTP 客户端（在 app shutdown 时调用）。"""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# 进程退出时兜底清理
atexit.register(lambda: None)  # 占位；atexit 不支持 async，实际清理由 lifespan 负责
