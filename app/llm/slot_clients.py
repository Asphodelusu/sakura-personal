"""按 model_slots 构建聊天 / 视觉 / 记忆整理 LLM 客户端。

聊天主路径为 RoutingLlmClient（云端 chat 槽 + 可选本地路由）；
显式配置 vision_chat / memory_curation 槽位时使用独立 OpenAICompatibleClient。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.model_slots import ResolvedModelSlot, resolve_model_slot
from app.config.models import (
    MODEL_SLOT_CHAT,
    MODEL_SLOT_CHAT_FAST,
    MODEL_SLOT_MEMORY_CURATION,
    MODEL_SLOT_VISION_CHAT,
)
from app.config.settings_service import AppSettingsService
from app.llm.api_client import ApiSettings, OpenAICompatibleClient
from app.llm.local_client import LocalLlmSettings, create_routing_llm_client


@dataclass(frozen=True)
class AppLlmClients:
    chat: object
    chat_fast: OpenAICompatibleClient | None
    vision: OpenAICompatibleClient | None
    memory_curation: object


def resolve_chat_api_settings(
    settings_service: AppSettingsService,
    base_settings: ApiSettings | None = None,
) -> ApiSettings:
    """解析聊天槽位；无 profiles 时回退 legacy api_settings。"""
    base = base_settings or settings_service.load_api_settings()
    profiles = settings_service.load_api_profiles()
    if not profiles:
        return base
    selection = settings_service.load_model_selection()
    chat_slot = resolve_model_slot(profiles, selection, MODEL_SLOT_CHAT, base)
    if chat_slot is not None:
        return chat_slot.settings
    return base


def resolve_vision_api_settings(
    settings_service: AppSettingsService,
    base_settings: ApiSettings | None = None,
) -> ApiSettings | None:
    """解析显式 vision_chat 槽位；未配置时返回 None。"""
    base = base_settings or settings_service.load_api_settings()
    profiles = settings_service.load_api_profiles()
    if not profiles:
        return None
    selection = settings_service.load_model_selection()
    vision_slot = resolve_model_slot(profiles, selection, MODEL_SLOT_VISION_CHAT, base)
    if vision_slot is None or vision_slot.source_slot != MODEL_SLOT_VISION_CHAT:
        return None
    return vision_slot.settings


def build_app_llm_clients(
    settings_service: AppSettingsService,
    *,
    base_settings: ApiSettings | None = None,
) -> AppLlmClients:
    base = base_settings or settings_service.load_api_settings()
    profiles = settings_service.load_api_profiles()
    selection = settings_service.load_model_selection()
    chat_settings = resolve_chat_api_settings(settings_service, base)
    local_llm_settings = settings_service.load_local_llm_settings()
    chat_client = create_routing_llm_client(chat_settings, local_llm_settings)

    chat_fast_slot = resolve_model_slot(profiles, selection, MODEL_SLOT_CHAT_FAST, base)
    chat_fast_client = _client_for_explicit_slot(chat_fast_slot, MODEL_SLOT_CHAT_FAST)

    vision_slot = resolve_model_slot(profiles, selection, MODEL_SLOT_VISION_CHAT, base)
    vision_client = _client_for_explicit_slot(vision_slot, MODEL_SLOT_VISION_CHAT)

    memory_slot = resolve_model_slot(profiles, selection, MODEL_SLOT_MEMORY_CURATION, base)
    if memory_slot is not None and memory_slot.source_slot == MODEL_SLOT_MEMORY_CURATION:
        memory_client: object = OpenAICompatibleClient(memory_slot.settings)
    else:
        memory_client = chat_client

    return AppLlmClients(
        chat=chat_client,
        chat_fast=chat_fast_client,
        vision=vision_client,
        memory_curation=memory_client,
    )


def _client_for_explicit_slot(
    resolved: ResolvedModelSlot | None,
    slot: str,
) -> OpenAICompatibleClient | None:
    if resolved is None or resolved.source_slot != slot:
        return None
    return OpenAICompatibleClient(resolved.settings)


def refresh_app_llm_clients(
    settings_service: AppSettingsService,
    *,
    base_settings: ApiSettings | None = None,
) -> AppLlmClients:
    """设置变更后重建客户端（与 build_app_llm_clients 相同，语义别名）。"""
    return build_app_llm_clients(settings_service, base_settings=base_settings)
