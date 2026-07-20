"""Privacy guard — blocks screen capture when sensitive windows are active.

Two-track matching:
1. Process exe basename (exact, case-insensitive) — high-confidence signal
2. Window title substring (case-insensitive) — catches browser tabs, dialog headers

Either match is sufficient to block.
"""

from __future__ import annotations

from app.perception.win32 import get_active_window_process_name, get_active_window_title

# Sensible defaults — password managers, banking, auth
DEFAULT_BLOCKED_PROCESSES = [
    "1password.exe",
    "bitwarden.exe",
    "keepass.exe",
    "keepassxc.exe",
    "lastpass.exe",
    "dashlane.exe",
    "authy.exe",
]

DEFAULT_BLOCKED_TITLE_KEYWORDS = [
    "1password",
    "bitwarden",
    "lastpass",
    "keepass",
    "online banking",
    "网上银行",
]


def privacy_guard_from_mapping(privacy: dict | None) -> PrivacyGuard:
    """从 proactive.privacy 映射构建 PrivacyGuard；缺省段用内置默认黑名单。"""
    if not isinstance(privacy, dict):
        return PrivacyGuard()
    processes = privacy.get("blocked_processes")
    keywords = privacy.get("blocked_title_keywords")
    return PrivacyGuard(
        blocked_processes=list(processes) if isinstance(processes, list) else None,
        blocked_title_keywords=list(keywords) if isinstance(keywords, list) else None,
    )


class PrivacyGuard:
    def __init__(
        self,
        blocked_processes: list[str] | None = None,
        blocked_title_keywords: list[str] | None = None,
    ) -> None:
        self._blocked_processes: set[str] = set()
        self._blocked_keywords: list[str] = []
        # None = 用内置默认；显式 [] = 用户清空黑名单，不要回退默认。
        self.set_blocked_processes(
            DEFAULT_BLOCKED_PROCESSES if blocked_processes is None else blocked_processes
        )
        self.set_blocked_title_keywords(
            DEFAULT_BLOCKED_TITLE_KEYWORDS
            if blocked_title_keywords is None
            else blocked_title_keywords
        )

    def set_blocked_processes(self, processes: list[str]) -> None:
        self._blocked_processes = {p.strip().casefold() for p in processes if p and p.strip()}

    def set_blocked_title_keywords(self, keywords: list[str]) -> None:
        self._blocked_keywords = [k.casefold() for k in keywords if k and k.strip()]

    @property
    def blocked_processes(self) -> list[str]:
        return sorted(self._blocked_processes)

    @property
    def blocked_title_keywords(self) -> list[str]:
        return list(self._blocked_keywords)

    def check_active_window(self) -> tuple[bool, str]:
        """Returns (is_blocked, reason). reason is '' when not blocked."""
        title = get_active_window_title()
        proc = get_active_window_process_name()

        if proc and proc.casefold() in self._blocked_processes:
            reason = proc
            if title:
                reason = f"{proc} — {title}"
            return True, reason

        if title:
            low = title.casefold()
            for kw in self._blocked_keywords:
                if kw in low:
                    return True, title

        return False, ""
