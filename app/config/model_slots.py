from __future__ import annotations

from dataclasses import dataclass

from app.config.models import (
    MODEL_SLOT_CHAT,
    MODEL_SLOT_CHAT_FAST,
    MODEL_SLOT_FALLBACKS,
    MODEL_SLOT_INNER_THOUGHT,
    ApiConfigProfile,
    ModelSelectionSettings,
    ModelSlotSelection,
)
from app.llm.api_client import ApiSettings


DEFAULT_CONTEXT_WINDOW_TOKENS = 32_768


@dataclass(frozen=True)
class ResolvedModelSlot:
    slot: str
    source_slot: str
    selection: ModelSlotSelection
    settings: ApiSettings


@dataclass(frozen=True)
class InnerThoughtModelChoice:
    settings: ApiSettings | None
    source_slot: str
    reason: str


_INNER_THOUGHT_SLOT_ORDER = (
    MODEL_SLOT_INNER_THOUGHT,
    MODEL_SLOT_CHAT_FAST,
    MODEL_SLOT_CHAT,
)


def normalize_provider_models(models: object) -> tuple[str, ...]:
    names: list[str] = []
    if isinstance(models, list | tuple):
        for item in models:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
            else:
                name = ""
            if name and name not in names:
                names.append(name)
    return tuple(names)


def resolve_inner_thought_model(
    profiles: list[ApiConfigProfile],
    selections: ModelSelectionSettings,
    base_settings: ApiSettings,
    *,
    invalid_slots: frozenset[str] = frozenset(),
) -> InnerThoughtModelChoice:
    """Use the first explicit slot. A configured but unusable slot disables the call."""

    for slot in _INNER_THOUGHT_SLOT_ORDER:
        if slot in invalid_slots:
            return InnerThoughtModelChoice(
                settings=None,
                source_slot=slot,
                reason="INNER_THOUGHT_SELECTION_INVALID",
            )
        selection = selections.get(slot)
        if selection is None or not selection.configured:
            continue
        settings = _explicit_slot_settings(profiles, selection, base_settings, slot)
        if settings is None:
            return InnerThoughtModelChoice(
                settings=None,
                source_slot=slot,
                reason="INNER_THOUGHT_SELECTION_INVALID",
            )
        return InnerThoughtModelChoice(settings=settings, source_slot=slot, reason="")
    return InnerThoughtModelChoice(settings=None, source_slot="", reason="INNER_THOUGHT_UNCONFIGURED")


def _explicit_slot_settings(
    profiles: list[ApiConfigProfile],
    selection: ModelSlotSelection,
    base_settings: ApiSettings,
    slot: str,
) -> ApiSettings | None:
    profile = find_profile(profiles, selection.profile_id)
    if profile is None or not profile.api_key.strip() or not profile.base_url.strip():
        return None
    if selection.model.strip() not in profile.models:
        return None
    return api_settings_from_selection(
        profile,
        selection.model,
        base_settings,
        include_dialogue_params=slot == MODEL_SLOT_CHAT,
        context_window_tokens=selection.context_window_tokens,
    )


def resolve_model_slot(
    profiles: list[ApiConfigProfile],
    selections: ModelSelectionSettings,
    slot: str,
    base_settings: ApiSettings,
) -> ResolvedModelSlot | None:
    for candidate in (slot, *MODEL_SLOT_FALLBACKS.get(slot, ())):
        selection = selections.get(candidate)
        if selection is None or not selection.configured:
            continue
        profile = find_profile(profiles, selection.profile_id)
        if profile is None:
            continue
        if selection.model.strip() not in profile.models:
            continue
        return ResolvedModelSlot(
            slot=slot,
            source_slot=candidate,
            selection=selection,
            settings=api_settings_from_selection(
                profile,
                selection.model,
                base_settings,
                include_dialogue_params=candidate == MODEL_SLOT_CHAT,
                context_window_tokens=selection.context_window_tokens,
            ),
        )
    return None


def api_settings_from_selection(
    profile: ApiConfigProfile,
    model: str,
    base_settings: ApiSettings,
    *,
    include_dialogue_params: bool = False,
    context_window_tokens: int | None = None,
) -> ApiSettings:
    return ApiSettings(
        base_url=profile.base_url.strip().rstrip("/"),
        api_key=profile.api_key.strip(),
        model=model.strip(),
        timeout_seconds=base_settings.timeout_seconds,
        temperature=base_settings.temperature if include_dialogue_params else None,
        top_p=base_settings.top_p if include_dialogue_params else None,
        max_tokens=base_settings.max_tokens if include_dialogue_params else None,
        context_window_tokens=context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS,
        context_window_source="user" if context_window_tokens is not None else "fallback",
    )


def find_profile(
    profiles: list[ApiConfigProfile],
    profile_id: str,
) -> ApiConfigProfile | None:
    for profile in profiles:
        if profile.id == profile_id:
            return profile
    return None
