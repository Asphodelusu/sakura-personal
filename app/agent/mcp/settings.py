from __future__ import annotations

import sys
from dataclasses import dataclass
from dataclasses import replace

from app.agent.mcp.config import MCPConfig


WINDOWS_MCP_ENABLED_KEY = "WINDOWS_MCP_ENABLED"
WINDOWS_MCP_AVAILABLE = True
WINDOWS_MCP_EXPERIMENTAL_TEXT = "实验性功能，供想要尝鲜的用户使用；可能不稳定，请谨慎开启"
DESKTOP_MCP_EXPERIMENTAL_TEXT = WINDOWS_MCP_EXPERIMENTAL_TEXT


@dataclass(frozen=True)
class DesktopMCP:
    """某平台对应的桌面控制 MCP。"""

    server_name: str
    label: str


_DESKTOP_MCP_BY_PLATFORM: dict[str, DesktopMCP] = {
    "win32": DesktopMCP(server_name="windows", label="Windows MCP"),
    "darwin": DesktopMCP(server_name="macos", label="macOS MCP"),
}


def resolve_desktop_mcp(platform: str | None = None) -> DesktopMCP | None:
    """返回当前平台的桌面控制 MCP。"""
    key = sys.platform if platform is None else platform
    return _DESKTOP_MCP_BY_PLATFORM.get(key)


@dataclass(frozen=True)
class MCPRuntimeSettings:
    """MCP 运行时开关；由 data/config/system_config.yaml 提供。"""

    windows_enabled: bool = False


def normalize_mcp_runtime_settings(settings: MCPRuntimeSettings) -> MCPRuntimeSettings:
    """归一化 MCP 运行时开关，保留全局屏蔽能力的兜底。"""

    if WINDOWS_MCP_AVAILABLE:
        return settings
    return replace(settings, windows_enabled=False)


def apply_mcp_runtime_settings(
    config: MCPConfig,
    settings: MCPRuntimeSettings,
) -> MCPConfig:
    """按运行时开关覆盖需要重启加载的 MCP server。"""

    normalized_settings = normalize_mcp_runtime_settings(settings)
    servers = [
        replace(server, enabled=normalized_settings.windows_enabled)
        if server.name == "windows"
        else server
        for server in config.servers
    ]
    return replace(config, servers=servers)
