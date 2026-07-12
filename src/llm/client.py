"""共享 LLM 客户端模块。

所有 LLM 调用（提取、重排、去重）统一从此处获取 AsyncOpenAI 实例，
避免 3 个模块各自创建独立客户端和连接池。
"""

import os
from openai import AsyncOpenAI

# 统一默认值（与 .env.example 一致）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))
DEFAULT_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
DEFAULT_TIMEOUT = 30.0  # LLM 调用超时（秒）

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
LLM_MODEL = os.getenv("LLM_MODEL", DEFAULT_MODEL)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """获取共享的 AsyncOpenAI 单例。

    配置了 timeout 和 max_retries：
    - timeout=30s：防止 API 调用无限挂起
    - max_retries=2：自动重试网络瞬断，避免静默失败

    注：asyncio 单线程模型下，check-then-create 之间无 await 点，
    极低概率的并发初始化仅会浪费一个连接池，不会崩溃。
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            timeout=DEFAULT_TIMEOUT,
            max_retries=2,
        )
    return _client


# 便于其他模块直接引用的配置字典
LLM_CONFIG = {
    "model": LLM_MODEL,
    "max_tokens": DEFAULT_MAX_TOKENS,
    "temperature": DEFAULT_TEMPERATURE,
}
