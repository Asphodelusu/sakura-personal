"""app/plugins/discovery.py — 插件发现。

负责扫描 plugins/ 目录和 plugins.yaml 配置，
发现可用插件并解析其清单信息。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.plugins.models import PluginSpec


class PluginDiscovery:
    """从配置文件和插件目录发现可用插件。

    职责：
    - 解析 data/config/plugins.yaml 中的插件入口
    - 按 priority 排序
    - 检查 enabled 状态
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._config_path = base_dir / "data" / "config" / "plugins.yaml"

    def discover(self) -> list[PluginSpec]:
        """发现所有已配置的插件（按优先级降序排列）。"""
        specs = self._load_specs()
        specs.sort(key=lambda s: s.priority, reverse=True)
        return specs

    def discover_enabled(self) -> list[PluginSpec]:
        """发现所有启用的插件。"""
        return [s for s in self.discover() if s.enabled]

    def _load_specs(self) -> list[PluginSpec]:
        if not self._config_path.is_file():
            return []
        raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        specs: list[PluginSpec] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            entry = item.get("entry")
            if not isinstance(entry, str) or not entry.strip():
                continue
            specs.append(
                PluginSpec(
                    entry=entry.strip(),
                    enabled=bool(item.get("enabled", True)),
                    priority=int(item.get("priority", 100 - idx)),
                )
            )
        return specs
