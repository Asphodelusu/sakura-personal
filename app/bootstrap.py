from __future__ import annotations

from pathlib import Path

from app.agent import AgentRuntime, MemoryStore, ReminderStore, create_builtin_tool_registry
from app.agent.mcp import MCPRuntimeSettings, register_mcp_tools_from_config
from app.agent.memory_curator import MemoryCurator, MemoryCurationSettings, MemoryCurationState
from app.api_client import ApiSettings, OpenAICompatibleClient
from app.app_context import AppContext
from app.character_loader import (
    DEFAULT_CHARACTER_ID,
    CharacterProfile,
    CharacterRegistry,
    load_character_system_prompt,
)
from app.chat_history import ChatHistoryStore
from app.debug_log import debug_log
from app.proactive_care import ProactiveCareSettings
from app.tts import create_tts_provider
from app.visual_observation import VisualObservationStore


def build_app_context(base_dir: Path) -> AppContext:
    """加载启动配置并创建主窗口所需的核心依赖。"""

    env_path = base_dir / ".env"
    settings = ApiSettings.load(env_path)
    api_client = OpenAICompatibleClient(settings)
    debug_log(
        "Startup",
        "API 配置已加载",
        {
            "base_url": settings.base_url,
            "model": settings.model,
            "timeout_seconds": settings.timeout_seconds,
            "api_key": settings.api_key,
        },
    )

    character_registry = CharacterRegistry(base_dir)
    character_profile = character_registry.current(env_path)
    system_prompt = load_character_system_prompt(character_profile)
    debug_log(
        "Startup",
        "角色配置已加载",
        {
            "character_id": character_profile.id,
            "display_name": character_profile.display_name,
            "reply_tones": character_profile.reply_tones,
        },
    )

    tts_provider = create_tts_provider(base_dir, character_profile)
    debug_log(
        "Startup",
        "TTS Provider 已创建",
        {"provider": type(tts_provider).__name__},
    )

    memory_store = MemoryStore(base_dir / "data" / "memory.json")
    reminder_store = ReminderStore(base_dir / "data" / "reminders.json")
    tool_registry = create_builtin_tool_registry(
        base_dir,
        memory_store,
        reminder_store,
    )
    mcp_tool_provider = register_mcp_tools_from_config(
        base_dir,
        tool_registry,
    )
    agent_runtime = AgentRuntime(
        api_client=api_client,
        system_prompt=system_prompt,
        reply_tones=character_profile.reply_tones,
        reply_portraits=character_profile.portrait_choices,
        tools=tool_registry,
        memory=memory_store,
    )
    history_store = _create_history_store(base_dir, character_profile)
    visual_observation_store = _create_visual_observation_store(base_dir, character_profile)
    mcp_settings = MCPRuntimeSettings.load(env_path)
    memory_curation_settings = MemoryCurationSettings.load(env_path)
    memory_curation_state = MemoryCurationState(
        base_dir / "data" / "memory_curation_state.json"
    )
    memory_curator = MemoryCurator(api_client, memory_store)
    proactive_care_settings = ProactiveCareSettings.load(env_path)

    debug_log(
        "Startup",
        "核心服务已创建",
        {
            "tool_count": len(tool_registry.all()),
            "mcp_enabled": mcp_tool_provider is not None,
            "windows_mcp_enabled": mcp_settings.windows_enabled,
            "auto_memory": memory_curation_settings.enabled,
        },
    )

    return AppContext(
        base_dir=base_dir,
        env_path=env_path,
        settings=settings,
        api_client=api_client,
        character_registry=character_registry,
        character_profile=character_profile,
        system_prompt=system_prompt,
        tts_provider=tts_provider,
        memory_store=memory_store,
        reminder_store=reminder_store,
        tool_registry=tool_registry,
        mcp_tool_provider=mcp_tool_provider,
        agent_runtime=agent_runtime,
        history_store=history_store,
        visual_observation_store=visual_observation_store,
        mcp_settings=mcp_settings,
        memory_curation_settings=memory_curation_settings,
        memory_curation_state=memory_curation_state,
        memory_curator=memory_curator,
        proactive_care_settings=proactive_care_settings,
    )


def _create_history_store(base_dir: Path, profile: CharacterProfile) -> ChatHistoryStore:
    history_path = base_dir / "data" / "chat_history" / f"{profile.id}.jsonl"
    _migrate_legacy_history(base_dir, profile, history_path)
    return ChatHistoryStore(history_path, profile.display_name)


def _create_visual_observation_store(
    base_dir: Path,
    profile: CharacterProfile,
) -> VisualObservationStore:
    visual_path = base_dir / "data" / "visual_observations" / f"{profile.id}.jsonl"
    return VisualObservationStore(visual_path)


def _migrate_legacy_history(base_dir: Path, profile: CharacterProfile, history_path: Path) -> None:
    if profile.id != DEFAULT_CHARACTER_ID or history_path.exists():
        return
    legacy_path = base_dir / "data" / "chat_history.jsonl"
    if not legacy_path.exists():
        return
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        print(f"[History] 旧历史迁移失败：{exc}")
