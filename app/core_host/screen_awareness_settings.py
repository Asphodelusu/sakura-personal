"""Runtime v2 screen-awareness settings boundary."""

from __future__ import annotations

import hmac
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.agent.screen_awareness import (
    SCREEN_AWARENESS_MAX_CHECK_INTERVAL_MINUTES,
    SCREEN_AWARENESS_MAX_COOLDOWN_MINUTES,
    SCREEN_AWARENESS_MAX_SCREEN_CONTEXT_BATCH_LIMIT,
    SCREEN_AWARENESS_MIN_CHECK_INTERVAL_MINUTES,
    SCREEN_AWARENESS_MIN_COOLDOWN_MINUTES,
    SCREEN_AWARENESS_MIN_SCREEN_CONTEXT_BATCH_LIMIT,
    SCREEN_AWARENESS_SCREEN_CONTEXT_RESOLUTIONS,
    ScreenAwarenessSettings,
)
from app.config.settings_service import AppSettingsService
from app.config.yaml_config import load_yaml_mapping
from app.core_host.protocol import response


SCREEN_AWARENESS_SETTINGS_REQUEST_NAMES = frozenset(
    {
        "screen_awareness.settings.get",
        "screen_awareness.settings.save",
        "screen_awareness.privacy.get",
        "screen_awareness.focus.advance",
    }
)
DEFAULT_BLOCKED_PROCESSES = (
    "1password.exe",
    "bitwarden.exe",
    "keepass.exe",
    "keepassxc.exe",
    "lastpass.exe",
    "dashlane.exe",
    "authy.exe",
)
DEFAULT_BLOCKED_TITLE_KEYWORDS = (
    "1password",
    "bitwarden",
    "lastpass",
    "keepass",
    "online banking",
    "网上银行",
)
MAX_PRIVACY_ENTRIES = 64
MAX_PRIVACY_ENTRY_CHARS = 128


def load_screen_privacy(app_root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Blocked foreground processes and title keywords, casefolded.

    ``screen_awareness.privacy`` wins over the Qt-era ``proactive.privacy``; an
    explicit empty list clears the defaults. Unreadable config keeps the defaults.
    """
    root = Path(app_root)
    for path, section_name in (
        (root / "config" / "system_config.yaml", "screen_awareness"),
        (root / "data" / "config" / "system_config.yaml", "proactive"),
    ):
        try:
            section = load_yaml_mapping(path).get(section_name)
        except (OSError, UnicodeError, ValueError):
            continue
        privacy = section.get("privacy") if isinstance(section, Mapping) else None
        if isinstance(privacy, Mapping):
            return (
                _privacy_entries(privacy.get("blocked_processes"), DEFAULT_BLOCKED_PROCESSES),
                _privacy_entries(privacy.get("blocked_title_keywords"), DEFAULT_BLOCKED_TITLE_KEYWORDS),
            )
    return (
        _privacy_entries(None, DEFAULT_BLOCKED_PROCESSES),
        _privacy_entries(None, DEFAULT_BLOCKED_TITLE_KEYWORDS),
    )


def _privacy_entries(raw: object, default: tuple[str, ...]) -> tuple[str, ...]:
    values = raw if isinstance(raw, list) else list(default)
    entries: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        entry = value.strip().casefold()[:MAX_PRIVACY_ENTRY_CHARS]
        if entry and entry not in entries:
            entries.append(entry)
    return tuple(entries[:MAX_PRIVACY_ENTRIES])


class ScreenAwarenessSettingsError(ValueError):
    def __init__(self, code: str, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def public_error(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": False,
            "details": {"feature": "privacy.screen_awareness", "field": self.field},
        }


class ScreenAwarenessSettingsBoundary:
    def __init__(
        self,
        generation_id: str,
        generation_credential: str,
        app_root: Path,
        session_provider: Any = None,
    ) -> None:
        self._generation_id = generation_id
        self._generation_credential = generation_credential
        self._app_root = Path(app_root)
        self._service = AppSettingsService(app_root)
        self._save_lock = threading.Lock()
        self._session_provider = session_provider

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        supplied = request.get("generationCredential")
        if (
            request.get("generationId") != self._generation_id
            or not isinstance(supplied, str)
            or not hmac.compare_digest(supplied, self._generation_credential)
        ):
            raise RuntimeError("GENERATION_IDENTITY_MISMATCH")
        try:
            payload = request.get("payload")
            if not isinstance(payload, Mapping):
                raise ScreenAwarenessSettingsError("INVALID_REQUEST", "主动屏幕感知设置请求格式无效。")
            name = request.get("name")
            if name == "screen_awareness.settings.get":
                if payload:
                    raise ScreenAwarenessSettingsError("INVALID_REQUEST", "设置读取请求必须为空。")
                result = self.snapshot()
            elif name == "screen_awareness.settings.save":
                if set(payload) != {"settings"}:
                    raise ScreenAwarenessSettingsError("INVALID_REQUEST", "设置保存请求格式无效。")
                result = self.save(payload["settings"])
            elif name == "screen_awareness.privacy.get":
                if payload:
                    raise ScreenAwarenessSettingsError("INVALID_REQUEST", "隐私名单读取请求必须为空。")
                processes, keywords = load_screen_privacy(self._app_root)
                result = {
                    "schemaVersion": 1,
                    "blockedProcesses": list(processes),
                    "blockedTitleKeywords": list(keywords),
                }
            elif name == "screen_awareness.focus.advance":
                result = self.advance_focus(payload)
            else:
                raise ScreenAwarenessSettingsError("UNKNOWN_COMMAND", "不支持的主动屏幕感知设置命令。")
            return response(
                request,
                generation_id=self._generation_id,
                generation_credential=self._generation_credential,
                protocol_minor=2,
                payload=result,
            )
        except ScreenAwarenessSettingsError as error:
            return response(
                request,
                generation_id=self._generation_id,
                generation_credential=self._generation_credential,
                protocol_minor=2,
                error=error.public_error(),
            )

    def advance_focus(self, payload: Mapping[str, Any]) -> dict[str, str]:
        allowed = {"busy", "scope", "snapshot", "outcome"}
        if not isinstance(payload, Mapping) or not set(payload) <= allowed or "busy" not in payload:
            raise ScreenAwarenessSettingsError("INVALID_REQUEST", "焦点观察请求格式无效。")
        busy = payload.get("busy")
        scope = payload.get("scope", "")
        outcome = payload.get("outcome", "")
        if not isinstance(busy, bool) or not isinstance(scope, str) or len(scope) > 128:
            raise ScreenAwarenessSettingsError("FIELD_INVALID", "焦点观察字段无效。", field="busy")
        if not isinstance(outcome, str) or len(outcome) > 32:
            raise ScreenAwarenessSettingsError("FIELD_INVALID", "焦点观察结果无效。", field="outcome")
        if outcome and outcome not in {"aborted", "failed", "privacy", "unchanged", "self", "submitted"}:
            raise ScreenAwarenessSettingsError("FIELD_INVALID", "焦点观察结果无效。", field="outcome")
        snapshot = payload.get("snapshot")
        if snapshot is not None and not _valid_focus_snapshot(snapshot):
            raise ScreenAwarenessSettingsError("FIELD_INVALID", "前台窗口快照无效。", field="snapshot")
        if scope != self._generation_id:
            return {"action": "wait", "trigger": "", "reason": "stale_scope"}
        runtime = _session_runtime(self._session_provider)
        if runtime is None or not callable(getattr(runtime, "advance_focus", None)):
            return {"action": "wait", "trigger": "", "reason": "unavailable"}
        if outcome:
            runtime.settle_focus_attempt(outcome)
            return {"action": "wait", "trigger": "", "reason": outcome}
        try:
            settings = self._service.load_screen_awareness_settings().normalized()
        except (OSError, UnicodeError, ValueError) as error:
            raise ScreenAwarenessSettingsError(
                "CONFIG_READ_ONLY", "主动屏幕感知配置损坏或不可读取。"
            ) from error
        processes, keywords = load_screen_privacy(self._app_root)
        marker = self._app_root / "logs" / "observer-diagnostics.enabled"
        if marker.is_file() or _diagnostics_env_enabled():
            runtime.set_focus_diagnostics_path(self._app_root / "logs" / "observer-diagnostics.jsonl")
        decision = runtime.advance_focus(
            snapshot,
            scope=scope,
            busy=busy,
            timer_seconds=float(settings.check_interval_minutes * 60),
            blocked_processes=processes,
            blocked_titles=keywords,
        )
        if set(decision) != {"action", "trigger", "reason"}:
            raise ScreenAwarenessSettingsError("INVALID_REQUEST", "焦点观察结果无效。")
        return {
            "action": str(decision["action"]),
            "trigger": str(decision["trigger"]),
            "reason": str(decision["reason"]),
        }

    def snapshot(self) -> dict[str, object]:
        try:
            settings = self._service.load_screen_awareness_settings().normalized()
        except (OSError, UnicodeError, ValueError) as error:
            raise ScreenAwarenessSettingsError(
                "CONFIG_READ_ONLY", "主动屏幕感知配置损坏或不可读取。"
            ) from error
        return _snapshot(settings)

    def save(self, raw: object) -> dict[str, object]:
        settings = _validate_settings(raw)
        with self._save_lock:
            try:
                self._service.save_screen_awareness_settings(settings)
            except (OSError, UnicodeError, ValueError) as error:
                raise ScreenAwarenessSettingsError(
                    "CONFIG_SAVE_FAILED", "主动屏幕感知设置保存失败，原文件保持不变。"
                ) from error
        return _snapshot(settings)


def _validate_settings(raw: object) -> ScreenAwarenessSettings:
    fields = {
        "enabled",
        "checkIntervalMinutes",
        "cooldownMinutes",
        "batchLimit",
        "resolution",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ScreenAwarenessSettingsError("INVALID_REQUEST", "主动屏幕感知设置字段无效。")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ScreenAwarenessSettingsError("FIELD_INVALID", "启用开关必须是布尔值。", field="enabled")

    def bounded_integer(name: str, minimum: int, maximum: int) -> int:
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ScreenAwarenessSettingsError(
                "FIELD_INVALID", f"{name} 超出允许范围。", field=name
            )
        return value

    resolution = raw.get("resolution")
    if resolution not in SCREEN_AWARENESS_SCREEN_CONTEXT_RESOLUTIONS:
        raise ScreenAwarenessSettingsError(
            "FIELD_INVALID", "截图分辨率无效。", field="resolution"
        )
    return ScreenAwarenessSettings(
        enabled=enabled,
        screen_context_enabled=enabled,
        check_interval_minutes=bounded_integer(
            "checkIntervalMinutes",
            SCREEN_AWARENESS_MIN_CHECK_INTERVAL_MINUTES,
            SCREEN_AWARENESS_MAX_CHECK_INTERVAL_MINUTES,
        ),
        cooldown_minutes=bounded_integer(
            "cooldownMinutes",
            SCREEN_AWARENESS_MIN_COOLDOWN_MINUTES,
            SCREEN_AWARENESS_MAX_COOLDOWN_MINUTES,
        ),
        screen_context_batch_limit=bounded_integer(
            "batchLimit",
            SCREEN_AWARENESS_MIN_SCREEN_CONTEXT_BATCH_LIMIT,
            SCREEN_AWARENESS_MAX_SCREEN_CONTEXT_BATCH_LIMIT,
        ),
        screen_context_resolution=str(resolution),
    )


def _diagnostics_env_enabled() -> bool:
    return os.environ.get("SAKURA_OBSERVER_DIAGNOSTICS", "").strip() == "1"


def _session_runtime(provider: object | None) -> object | None:
    if not callable(provider):
        return None
    try:
        session = provider()
    except Exception:
        return None
    return getattr(session, "runtime", None)


def _valid_focus_snapshot(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"hwnd", "pid", "process", "title", "ownProcess"}:
        return False
    hwnd = value.get("hwnd")
    pid = value.get("pid")
    process = value.get("process")
    title = value.get("title")
    own = value.get("ownProcess")
    return (
        isinstance(hwnd, int)
        and not isinstance(hwnd, bool)
        and 0 <= hwnd <= 2**64 - 1
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and 0 <= pid <= 2**32
        and isinstance(process, str)
        and len(process) <= 260
        and isinstance(title, str)
        and len(title) <= 512
        and isinstance(own, bool)
    )


def _snapshot(settings: ScreenAwarenessSettings) -> dict[str, object]:
    normalized = settings.normalized()
    return {
        "schemaVersion": 1,
        "settings": {
            "enabled": normalized.allows_screen_context(),
            "checkIntervalMinutes": normalized.check_interval_minutes,
            "cooldownMinutes": normalized.cooldown_minutes,
            "batchLimit": normalized.screen_context_batch_limit,
            "resolution": normalized.screen_context_resolution,
        },
    }


__all__ = [
    "SCREEN_AWARENESS_SETTINGS_REQUEST_NAMES",
    "ScreenAwarenessSettingsBoundary",
    "ScreenAwarenessSettingsError",
]
