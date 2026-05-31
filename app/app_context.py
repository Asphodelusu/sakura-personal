from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.agent import AgentRuntime, MemoryStore, ReminderStore, ToolRegistry
from app.agent.mcp import MCPRuntimeSettings, MCPToolProvider
from app.agent.memory_curator import MemoryCurator, MemoryCurationSettings, MemoryCurationState
from app.api_client import ApiSettings, OpenAICompatibleClient
from app.character_loader import CharacterProfile, CharacterRegistry
from app.chat_history import ChatHistoryStore
from app.proactive_care import ProactiveCareSettings
from app.tts import TTSProvider
from app.visual_observation import VisualObservationStore


@dataclass(frozen=True)
class AppContext:
    """应用启动阶段组装出的核心依赖。"""

    base_dir: Path
    env_path: Path
    settings: ApiSettings
    api_client: OpenAICompatibleClient
    character_registry: CharacterRegistry
    character_profile: CharacterProfile
    system_prompt: str
    tts_provider: TTSProvider
    memory_store: MemoryStore
    reminder_store: ReminderStore
    tool_registry: ToolRegistry
    mcp_tool_provider: MCPToolProvider | None
    agent_runtime: AgentRuntime
    history_store: ChatHistoryStore
    visual_observation_store: VisualObservationStore
    mcp_settings: MCPRuntimeSettings
    memory_curation_settings: MemoryCurationSettings
    memory_curation_state: MemoryCurationState
    memory_curator: MemoryCurator
    proactive_care_settings: ProactiveCareSettings
