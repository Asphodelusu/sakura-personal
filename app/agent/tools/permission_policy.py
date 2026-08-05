"""ToolPermissionPolicy — 统一的工具权限与确认策略。

将散落在 ToolRegistry、runtime.py、tool_policy.py 中的：
- 风险等级 → 是否需确认的映射
- free_access 模式的豁免规则
- 高风险工具的强制确认逻辑
集中到单一策略类中。

free_access 语义（与 UI「完整访问权限」开关一致）：
- 开启（默认）：所有非高风险工具豁免确认，直接执行（含 open_url / open_local_folder 等
  requires_confirmation 工具）；破坏性操作（delete 等）始终确认。
- 关闭：所有 requires_confirmation 工具都走确认面板。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.tools.registry import Tool


@dataclass
class ToolPermissionPolicy:
    """工具权限策略。

    决定：
    1. 给定工具是否需要用户确认
    2. free_access 模式下哪些工具可跳过确认
    3. 哪些工具因高风险必须始终确认
    """

    free_access_enabled: bool = True

    # ---- 高风险标记 (free_access 也不能跳过) ----

    HIGH_RISK_CONFIRMATION_PATTERNS: tuple[str, ...] = (
        "delete_file", "remove_file", "unlink_file",
        "delete_path", "remove_path",
        "delete_local_file", "remove_local_file",
    )

    # ---- 判断逻辑 ----

    def requires_confirmation(self, tool: Tool, arguments: dict[str, Any] | None = None) -> bool:
        """判断工具是否需要用户确认。

        返回 True 表示需要弹出确认面板。
        """
        # 工具本身不需要确认
        if not tool.requires_confirmation:
            return False

        # free_access 模式下的豁免
        if self.free_access_enabled and self._can_execute_with_free_access(tool):
            return False

        return True

    def _can_execute_with_free_access(self, tool: Tool) -> bool:
        """free_access 模式下是否可直接执行。

        当前语义：豁免所有非高风险工具（含打开 URL / 本地文件夹等 requires_confirmation
        工具）。破坏性操作由 _is_always_high_risk 拦截，始终确认。
        """
        # 高风险工具始终需要确认
        if self._is_always_high_risk(tool):
            return False
        return True

    def _is_always_high_risk(self, tool: Tool) -> bool:
        """检查是否属于不可豁免的高风险工具。"""
        if tool.risk == "high":
            return True
        if tool.confirmation_risk in {"delete_file", "file_delete", "destructive_file"}:
            return True
        normalized = tool.name.lower()
        return any(
            marker in normalized
            for marker in self.HIGH_RISK_CONFIRMATION_PATTERNS
        )
