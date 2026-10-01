"""ProactiveObserver runtime config.

Personal mode reads this mapping. Canonical ``config/system_config.yaml`` wins;
``data/config/system_config.yaml`` is used only when the canonical file is absent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


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
# Settings-page bounds for the fields that page edits. YAML load keeps other
# finite values; it does not round seconds into minutes.
PERSONAL_SETTING_BOUNDS = {
    "timer_seconds": (1.0, 86400.0),
    "cooldown_seconds": (0.0, 86400.0),
    "focus_settle_delay": (0.0, 3600.0),
    "window_switch_cooldown": (0.0, 86400.0),
    "poll_interval": (0.2, 300.0),
}
_GAME_OCR_HARD_DISABLED = True


@dataclass
class ProactiveConfig:
    enabled: bool = True
    timer_seconds: float = 480
    cooldown_seconds: float = 600
    min_silence_after_user: float = 10
    window_switch_enabled: bool = True
    window_switch_cooldown: float = 60
    focus_settle_delay: float = 15
    idle_threshold_seconds: float = 600
    poll_interval: float = 5.0
    content_check_interval: float = 30.0
    content_min_chars: int = 30
    content_quiet_seconds: float = 180.0
    game_ocr_enabled: bool = False
    max_edge: int = 1920
    request_timeout: float = 60.0
    eval_temperature: float = 0.7
    max_tokens: int = 1024
    adaptive_interval_min: float = 300.0
    adaptive_interval_max: float = 1800.0
    silent_eval_cooldown_seconds: float = 300.0
    away_max_seconds: float = 12 * 3600

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ProactiveConfig":
        if not isinstance(raw, dict):
            return cls()
        base = cls()
        return cls(
            enabled=_bool_value(raw.get("enabled"), base.enabled),
            timer_seconds=_finite_float(raw.get("timer_seconds"), base.timer_seconds, minimum=0.0),
            cooldown_seconds=_finite_float(raw.get("cooldown_seconds"), base.cooldown_seconds, minimum=0.0),
            min_silence_after_user=_finite_float(
                raw.get("min_silence_after_user"), base.min_silence_after_user, minimum=0.0
            ),
            window_switch_enabled=_bool_value(raw.get("window_switch_enabled"), base.window_switch_enabled),
            window_switch_cooldown=_finite_float(
                raw.get("window_switch_cooldown"), base.window_switch_cooldown, minimum=0.0
            ),
            focus_settle_delay=_finite_float(raw.get("focus_settle_delay"), base.focus_settle_delay, minimum=0.0),
            idle_threshold_seconds=_finite_float(
                raw.get("idle_threshold_seconds"), base.idle_threshold_seconds, minimum=0.0
            ),
            poll_interval=_finite_float(raw.get("poll_interval"), base.poll_interval, minimum=0.0),
            content_check_interval=_finite_float(
                raw.get("content_check_interval"), base.content_check_interval, minimum=0.0
            ),
            content_min_chars=_finite_int(raw.get("content_min_chars"), base.content_min_chars, minimum=0),
            content_quiet_seconds=_finite_float(
                raw.get("content_quiet_seconds"), base.content_quiet_seconds, minimum=0.0
            ),
            game_ocr_enabled=False if _GAME_OCR_HARD_DISABLED else _bool_value(
                raw.get("game_ocr_enabled"), False
            ),
            max_edge=_finite_int(raw.get("max_edge"), base.max_edge, minimum=1),
            request_timeout=_finite_float(raw.get("request_timeout"), base.request_timeout, minimum=0.0),
            eval_temperature=_finite_float(raw.get("eval_temperature"), base.eval_temperature),
            max_tokens=_finite_int(raw.get("max_tokens"), base.max_tokens, minimum=1),
            adaptive_interval_min=_finite_float(
                raw.get("adaptive_interval_min"), base.adaptive_interval_min, minimum=0.0
            ),
            adaptive_interval_max=_finite_float(
                raw.get("adaptive_interval_max"), base.adaptive_interval_max, minimum=0.0
            ),
            silent_eval_cooldown_seconds=_finite_float(
                raw.get("silent_eval_cooldown_seconds"),
                base.silent_eval_cooldown_seconds,
                minimum=0.0,
            ),
            away_max_seconds=_finite_float(raw.get("away_max_seconds"), base.away_max_seconds, minimum=0.0),
        )


def resolve_proactive_config_path(base_dir: Path) -> Path:
    root = Path(base_dir)
    canonical = root / "config" / "system_config.yaml"
    if canonical.exists():
        return canonical
    legacy = root / "data" / "config" / "system_config.yaml"
    if legacy.exists():
        return legacy
    return canonical


def normalize_proactive_config_mapping(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a proactive section. Unknown keys are not returned; callers keep them."""
    source = dict(raw) if isinstance(raw, Mapping) else {}
    cfg = ProactiveConfig.from_dict(source)
    privacy_raw = source.get("privacy")
    if isinstance(privacy_raw, dict):
        processes = _dedupe(privacy_raw.get("blocked_processes"))
        keywords = _dedupe(privacy_raw.get("blocked_title_keywords"))
        if "blocked_processes" not in privacy_raw:
            processes = list(DEFAULT_BLOCKED_PROCESSES)
        if "blocked_title_keywords" not in privacy_raw:
            keywords = list(DEFAULT_BLOCKED_TITLE_KEYWORDS)
    else:
        processes = list(DEFAULT_BLOCKED_PROCESSES)
        keywords = list(DEFAULT_BLOCKED_TITLE_KEYWORDS)
    return {
        "enabled": bool(cfg.enabled),
        "timer_seconds": float(cfg.timer_seconds),
        "cooldown_seconds": float(cfg.cooldown_seconds),
        "min_silence_after_user": float(cfg.min_silence_after_user),
        "window_switch_enabled": bool(cfg.window_switch_enabled),
        "window_switch_cooldown": float(cfg.window_switch_cooldown),
        "focus_settle_delay": float(cfg.focus_settle_delay),
        "idle_threshold_seconds": float(cfg.idle_threshold_seconds),
        "poll_interval": float(cfg.poll_interval),
        "content_check_interval": float(cfg.content_check_interval),
        "content_min_chars": int(cfg.content_min_chars),
        "content_quiet_seconds": float(cfg.content_quiet_seconds),
        "game_ocr_enabled": False,
        "max_edge": int(cfg.max_edge),
        "request_timeout": float(cfg.request_timeout),
        "eval_temperature": float(cfg.eval_temperature),
        "max_tokens": int(cfg.max_tokens),
        "adaptive_interval_min": float(cfg.adaptive_interval_min),
        "adaptive_interval_max": float(cfg.adaptive_interval_max),
        "silent_eval_cooldown_seconds": float(cfg.silent_eval_cooldown_seconds),
        "away_max_seconds": float(cfg.away_max_seconds),
        "privacy": {
            "blocked_processes": processes,
            "blocked_title_keywords": keywords,
        },
    }


def selected_character_is_personal(user_root: Path) -> bool:
    """Personal identity matches AssistantAdapter: a selected card with system guards.

    The decision uses the path recorded on the profile. It does not read the card
    or the guards text, and it does not infer the mode from the character id.
    """
    from app.config.character_loader import CharacterRegistry
    from app.config.settings_service import AppSettingsService

    root = Path(user_root)
    try:
        registry = CharacterRegistry(root, issue_sink=lambda *_args: None)
        character_id = AppSettingsService(root).load_current_character_id(registry)
    except (OSError, UnicodeError, ValueError):
        return False
    if not character_id:
        return False
    profile = registry.profiles.get(character_id)
    return profile is not None and profile.system_guards_path is not None


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _finite_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _finite_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        parsed = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _dedupe(values: object) -> list[str]:
    result: list[str] = []
    if isinstance(values, (str, bytes)):
        candidates = [str(values)]
    else:
        try:
            candidates = list(values or [])
        except TypeError:
            candidates = []
    for value in candidates:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result
