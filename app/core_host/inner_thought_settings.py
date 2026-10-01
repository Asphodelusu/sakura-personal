"""Load inner-thought settings and the dedicated model client.

Canonical ``config/system_config.yaml`` wins over a legacy copy. An explicit
inner-thought or fast slot that cannot be resolved disables the optional call.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app.agent.inner_thought import InnerThoughtSettings
from app.config.model_slots import (
    InnerThoughtModelChoice,
    resolve_inner_thought_model,
    resolve_model_slot,
)
from app.config.models import MODEL_SLOT_CHAT_FAST, ApiSettings, ModelSelectionSettings, ModelSlotSelection
from app.config.settings_service import AppSettingsService
from app.config.yaml_config import load_yaml_mapping
from app.core.runtime_log import log_event
from app.llm.api_client import OpenAICompatibleClient

_HIDDEN_SLOTS = ("inner_thought", "chat_fast", "chat")


def load_inner_thought_settings(user_root: Path) -> InnerThoughtSettings:
    root = Path(user_root)
    canonical = _read_mapping(root / "config" / "system_config.yaml")
    if "inner_thought" in canonical:
        return _settings_from_section(canonical.get("inner_thought"))
    legacy = _read_mapping(root / "data" / "config" / "system_config.yaml")
    if "inner_thought" in legacy:
        return _settings_from_section(legacy.get("inner_thought"))
    return InnerThoughtSettings().normalized()


def attach_inner_thought(runtime: object, user_root: Path) -> None:
    settings = load_inner_thought_settings(user_root)
    choice = _load_choice(user_root)
    client: OpenAICompatibleClient | None = None
    if choice.settings is not None:
        timeout = min(int(choice.settings.timeout_seconds or settings.timeout_seconds), int(settings.timeout_seconds))
        timeout = max(1, timeout)
        client = OpenAICompatibleClient(
            replace(choice.settings, timeout_seconds=timeout),
            request_attempts=1,
        )
    elif choice.reason:
        log_event(
            "InnerThought",
            "内心独白模型未使用",
            {"code": choice.reason, "source_slot": choice.source_slot},
            severity="info",
        )
    configure = getattr(runtime, "configure_inner_thought", None)
    if not callable(configure):
        return
    configure(settings=settings, client=client, source_slot=choice.source_slot)


def load_fast_slot_settings(user_root: Path) -> ApiSettings | None:
    """The chat_fast slot, falling back to the chat slot; None when neither resolves."""
    try:
        service = AppSettingsService(Path(user_root))
        profiles = service.load_api_profiles()
        base = service.load_api_settings()
        raw = load_yaml_mapping(service.api_config_path)
    except (OSError, UnicodeError, ValueError):
        return None
    selections, invalid = _selections(raw.get("model_slots"))
    if MODEL_SLOT_CHAT_FAST in invalid:
        return None
    resolved = resolve_model_slot(profiles, selections, MODEL_SLOT_CHAT_FAST, base)
    return resolved.settings if resolved is not None else None


def _load_choice(user_root: Path) -> InnerThoughtModelChoice:
    try:
        service = AppSettingsService(Path(user_root))
        profiles = service.load_api_profiles()
        base = service.load_api_settings()
        raw = load_yaml_mapping(service.api_config_path)
    except (OSError, UnicodeError, ValueError):
        return InnerThoughtModelChoice(settings=None, source_slot="", reason="INNER_THOUGHT_SELECTION_INVALID")
    selections, invalid = _selections(raw.get("model_slots"))
    return resolve_inner_thought_model(profiles, selections, base, invalid_slots=invalid)


def _selections(raw: object) -> tuple[ModelSelectionSettings, frozenset[str]]:
    if not isinstance(raw, Mapping):
        return ModelSelectionSettings(), frozenset()
    invalid: set[str] = set()
    parsed: dict[str, ModelSlotSelection | None] = {}
    for name in _HIDDEN_SLOTS:
        if name not in raw:
            parsed[name] = None
            continue
        value = raw.get(name)
        if not isinstance(value, Mapping):
            invalid.add(name)
            parsed[name] = None
            continue
        profile_id = value.get("profile_id", "")
        model = value.get("model", "")
        if not isinstance(profile_id, str) or not isinstance(model, str):
            invalid.add(name)
            parsed[name] = None
            continue
        if bool(profile_id.strip()) != bool(model.strip()):
            invalid.add(name)
            parsed[name] = None
            continue
        context_window = value.get("context_window_tokens")
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or not 4_096 <= context_window <= 2_000_000
        ):
            invalid.add(name)
            parsed[name] = None
            continue
        parsed[name] = ModelSlotSelection(
            profile_id=profile_id.strip(),
            model=model.strip(),
            context_window_tokens=context_window if isinstance(context_window, int) else None,
        )
    chat = parsed.get("chat") or ModelSlotSelection()
    return (
        ModelSelectionSettings(
            chat=chat,
            chat_fast=parsed.get("chat_fast"),
            inner_thought=parsed.get("inner_thought"),
        ),
        frozenset(invalid),
    )


def _settings_from_section(raw: object) -> InnerThoughtSettings:
    section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return InnerThoughtSettings(
        enabled=_as_bool(section.get("enabled"), True),
        window_size=_as_int(section.get("window_size"), 6),
        timeout_seconds=_as_int(section.get("timeout_seconds"), 8),
        join_timeout_seconds=_as_int(section.get("join_timeout_seconds"), 3),
        skip_fast_tier=_as_bool(section.get("skip_fast_tier"), True),
        skip_proactive=_as_bool(section.get("skip_proactive"), True),
    ).normalized()


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        return load_yaml_mapping(path)
    except (OSError, UnicodeError, ValueError):
        return {}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value
