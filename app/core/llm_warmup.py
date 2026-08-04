"""启动期 LLM HTTP/TLS 连接预热。

与首轮对话不互斥：httpx.Client 线程安全，共用连接池即可。
改 API / 退出时预热失败一律静默忽略。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.core.debug_log import debug_log


def warm_llm_clients(
    *clients: Any,
    timeout_seconds: float = 8.0,
) -> int:
    """对若干 LLM 客户端做连接预热；同一 base_url+api_key 只暖一次。

    返回成功预热的端点数（去重后）。
    """
    warmed = 0
    seen: set[str] = set()
    for client in clients:
        if client is None:
            continue
        warm = getattr(client, "warm_connection", None)
        if not callable(warm):
            continue
        key = _client_endpoint_key(client)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        try:
            if bool(warm(timeout_seconds=timeout_seconds)):
                warmed += 1
        except Exception as exc:  # noqa: BLE001
            debug_log("API", "连接预热异常（已忽略）", {"error": str(exc)})
    return warmed


def warm_agent_runtime_llm_connections(
    agent_runtime: Any,
    *,
    timeout_seconds: float = 8.0,
) -> int:
    """预热 Agent 对话路径上的主模型与 chat_fast。"""
    return warm_llm_clients(
        getattr(agent_runtime, "api_client", None),
        getattr(agent_runtime, "chat_fast_api_client", None),
        timeout_seconds=timeout_seconds,
    )


def _client_endpoint_key(client: Any) -> str:
    settings = getattr(client, "settings", None)
    if settings is None:
        cloud = getattr(client, "cloud_client", None)
        settings = getattr(cloud, "settings", None)
    if settings is None:
        return ""
    base = str(getattr(settings, "base_url", "") or "").strip().rstrip("/")
    key = str(getattr(settings, "api_key", "") or "").strip()
    if not base:
        return ""
    host = urlsplit(base).netloc or base
    # 只取 key 后缀，避免日志/集合里堆完整密钥；去重足够。
    return f"{host}|{key[-8:] if key else ''}"
