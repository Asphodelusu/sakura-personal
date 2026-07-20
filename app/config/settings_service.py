from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from app.agent.mcp.settings import MCPRuntimeSettings, normalize_mcp_runtime_settings
from app.agent.runtime_limits import RuntimeLoopSettings, normalize_runtime_loop_settings
from app.config.character_loader import DEFAULT_CHARACTER_ID, CharacterProfile, CharacterRegistry
from app.config.yaml_config import load_yaml_mapping, save_yaml_mapping
from app.llm.api_client import ApiSettings
from app.llm.local_client import LocalLlmSettings
from app.perception.proactive_config import ProactiveConfig
from app.perception.privacy import (
    DEFAULT_BLOCKED_PROCESSES,
    DEFAULT_BLOCKED_TITLE_KEYWORDS,
)
from app.storage.paths import StoragePaths
from app.ui.theme import ThemeSettings, theme_from_mapping, theme_to_mapping
from app.agent.screen_awareness import (
    SCREEN_AWARENESS_DEFAULT_CHECK_INTERVAL_MINUTES,
    SCREEN_AWARENESS_DEFAULT_COOLDOWN_MINUTES,
    SCREEN_AWARENESS_DEFAULT_SCREEN_CONTEXT_BATCH_LIMIT,
    SCREEN_AWARENESS_DEFAULT_SCREEN_CONTEXT_RESOLUTION,
    ScreenAwarenessSettings,
)
from app.voice.tts_settings import (
    DEFAULT_GENIE_TTS_API_URL,
    DEFAULT_GPT_SOVITS_API_URL,
    TTS_PROVIDER_CUSTOM_GPT_SOVITS,
    TTS_PROVIDER_GENIE,
    TTS_PROVIDER_GPT_SOVITS,
    TTS_PROVIDER_NONE,
    GPTSoVITSTTSSettings,
)


API_CONFIG_FILE = "api.yaml"
CHARACTERS_CONFIG_FILE = "characters.yaml"
SYSTEM_CONFIG_FILE = "system_config.yaml"


@dataclass(frozen=True)
class DebugLogSettings:
    """调试日志配置。"""

    enabled: bool = False
    body_enabled: bool = False
    file_enabled: bool = False
    profile: str = "info"
    # 开发者选项:舞台调试框(画窗口/布局/实际立绘三框 + DPR 数值,排查布局/HiDPI)。
    stage_debug_overlay: bool = False
    # 舞台碰撞遮罩(默认开):setMask 到内容矩形并集,立绘四周空白点击穿透,避免误拖/挡点击。
    stage_collision_mask: bool = True


@dataclass(frozen=True)
class StartupSettings:
    """启动行为配置。"""

    launch_at_login: bool = False


BUBBLE_AUTO_HIDE_MIN_DELAY_SECONDS = 1
BUBBLE_AUTO_HIDE_MAX_DELAY_SECONDS = 120
BUBBLE_AUTO_HIDE_DEFAULT_DELAY_SECONDS = 5


@dataclass(frozen=True)
class BubbleSettings:
    """对话气泡无操作自动隐藏配置。"""

    auto_hide_enabled: bool = True
    auto_hide_delay_seconds: int = BUBBLE_AUTO_HIDE_DEFAULT_DELAY_SECONDS

    def normalized(self) -> "BubbleSettings":
        delay = max(
            BUBBLE_AUTO_HIDE_MIN_DELAY_SECONDS,
            min(BUBBLE_AUTO_HIDE_MAX_DELAY_SECONDS, int(self.auto_hide_delay_seconds)),
        )
        return BubbleSettings(
            auto_hide_enabled=bool(self.auto_hide_enabled),
            auto_hide_delay_seconds=delay,
        )


BACKCHANNEL_MIN_DELAY_MS = 100
BACKCHANNEL_MAX_DELAY_MS = 5000
BACKCHANNEL_DEFAULT_DELAY_MS = 600
BACKCHANNEL_MODES = ("off", "rules", "hybrid")
BACKCHANNEL_DEFAULT_MODE = "rules"
# hybrid 后台分类超时(安全网):超时按无标签落兜底,不阻塞迟到的接话。
# 仅对 hybrid 生效;规则分类同步不触发。0 表示不设超时。
BACKCHANNEL_MIN_TIMEOUT_MS = 0
BACKCHANNEL_MAX_TIMEOUT_MS = 2000
BACKCHANNEL_DEFAULT_TIMEOUT_MS = 400


@dataclass(frozen=True)
class BackchannelSettings:
    """本地快速接话层配置。

    默认关闭;rules 为纯规则模式,hybrid 为 rules-first + 本地 embedding 意图泛化。
    """

    enabled: bool = False
    mode: str = BACKCHANNEL_DEFAULT_MODE
    delay_ms: int = BACKCHANNEL_DEFAULT_DELAY_MS
    probability: float = 1.0
    tts_enabled: bool = False
    timeout_ms: int = BACKCHANNEL_DEFAULT_TIMEOUT_MS

    @property
    def active(self) -> bool:
        return self.enabled and self.mode != "off"

    def normalized(self) -> "BackchannelSettings":
        mode = self.mode if self.mode in BACKCHANNEL_MODES else BACKCHANNEL_DEFAULT_MODE
        delay = max(
            BACKCHANNEL_MIN_DELAY_MS,
            min(BACKCHANNEL_MAX_DELAY_MS, int(self.delay_ms)),
        )
        probability = max(0.0, min(1.0, float(self.probability)))
        timeout = max(
            BACKCHANNEL_MIN_TIMEOUT_MS,
            min(BACKCHANNEL_MAX_TIMEOUT_MS, int(self.timeout_ms)),
        )
        return BackchannelSettings(
            enabled=bool(self.enabled),
            mode=mode,
            delay_ms=delay,
            probability=probability,
            tts_enabled=bool(self.tts_enabled),
            timeout_ms=timeout,
        )


@dataclass(frozen=True)
class AppSettingsService:
    """集中管理运行配置；唯一持久化来源是 data/config/*.yaml。"""

    base_dir: Path

    @property
    def config_dir(self) -> Path:
        return StoragePaths(self.base_dir).config_dir

    @property
    def api_config_path(self) -> Path:
        return self.config_dir / API_CONFIG_FILE

    @property
    def characters_config_path(self) -> Path:
        return self.config_dir / CHARACTERS_CONFIG_FILE

    @property
    def system_config_path(self) -> Path:
        return self.config_dir / SYSTEM_CONFIG_FILE

    def load_api_settings(self) -> ApiSettings:
        data = self._api_section("llm")
        timeout_seconds = _int_value(
            data.get("timeout_seconds"),
            60,
        )
        return ApiSettings(
            base_url=str(data.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/"),
            api_key=str(data.get("api_key", "")).strip(),
            model=str(data.get("model", "gpt-4.1-mini")).strip(),
            timeout_seconds=timeout_seconds,
            temperature=_optional_float(data.get("temperature"), minimum=0.0, maximum=2.0),
            top_p=_optional_float(data.get("top_p"), minimum=0.0, maximum=1.0),
            max_tokens=_optional_positive_int(data.get("max_tokens")),
            frequency_penalty=_optional_float(data.get("frequency_penalty"), minimum=-2.0, maximum=2.0),
            presence_penalty=_optional_float(data.get("presence_penalty"), minimum=-2.0, maximum=2.0),
        )

    def save_api_settings(self, settings: ApiSettings) -> None:
        data = load_yaml_mapping(self.api_config_path)
        llm_data: dict[str, Any] = {
            "base_url": settings.base_url.strip().rstrip("/"),
            "api_key": settings.api_key.strip(),
            "model": settings.model.strip(),
            "timeout_seconds": int(settings.timeout_seconds),
        }
        # 仅写入用户显式配置的高级参数，避免给老配置塞入空键、改变默认行为。
        if settings.temperature is not None:
            llm_data["temperature"] = float(settings.temperature)
        if settings.top_p is not None:
            llm_data["top_p"] = float(settings.top_p)
        if settings.max_tokens is not None:
            llm_data["max_tokens"] = int(settings.max_tokens)
        if settings.frequency_penalty is not None:
            llm_data["frequency_penalty"] = float(settings.frequency_penalty)
        if settings.presence_penalty is not None:
            llm_data["presence_penalty"] = float(settings.presence_penalty)
        data["llm"] = llm_data
        save_yaml_mapping(self.api_config_path, data)

    def save_api_profiles(self, profiles: list) -> None:
        """保存 API 供应商配置列表到 api.yaml。"""
        data = load_yaml_mapping(self.api_config_path)
        data["api_profiles"] = [
            {
                "id": str(getattr(p, "id", "")),
                "alias": str(getattr(p, "alias", "")),
                "base_url": str(getattr(p, "base_url", "")).strip().rstrip("/"),
                "api_key": str(getattr(p, "api_key", "")).strip(),
                "models": [{"name": name} for name in _dedupe(getattr(p, "models", ()))],
            }
            for p in profiles
        ]
        save_yaml_mapping(self.api_config_path, data)

    def save_model_selection(self, settings: object) -> None:
        """保存模型槽位选择到 api.yaml。"""
        from app.config.models import (
            MODEL_SLOT_CHAT,
            MODEL_SLOT_CHAT_FAST,
            MODEL_SLOT_MEMORY_CURATION,
            MODEL_SLOT_ORDER,
            MODEL_SLOT_VISION_CHAT,
        )
        data = load_yaml_mapping(self.api_config_path)
        slots: dict[str, dict[str, str]] = {}
        for slot in MODEL_SLOT_ORDER:
            selection = getattr(settings, "slots", {}).get(slot) if hasattr(settings, "slots") else getattr(settings, slot, None)
            if selection is None:
                continue
            pid = str(getattr(selection, "profile_id", "")).strip()
            model = str(getattr(selection, "model", "")).strip()
            if not pid and not model:
                continue
            if slot != MODEL_SLOT_CHAT and not pid:
                continue
            slots[slot] = {"profile_id": pid, "model": model}
        data["model_slots"] = slots
        save_yaml_mapping(self.api_config_path, data)

    def load_api_profiles(self) -> list:
        """从 api.yaml 读取 API 供应商列表；无有效数据时返回空列表。"""
        from app.config.model_slots import normalize_provider_models
        from app.config.models import ApiConfigProfile

        data = load_yaml_mapping(self.api_config_path)
        raw_profiles = data.get("api_profiles")
        if not isinstance(raw_profiles, list):
            return []
        profiles: list[ApiConfigProfile] = []
        seen: set[str] = set()
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                continue
            profile_id = str(raw.get("id", "")).strip()
            if not profile_id or profile_id in seen:
                continue
            base_url = str(raw.get("base_url", "")).strip().rstrip("/")
            if not base_url:
                continue
            models = normalize_provider_models(raw.get("models"))
            if not models:
                continue
            seen.add(profile_id)
            profiles.append(
                ApiConfigProfile(
                    id=profile_id,
                    alias=str(raw.get("alias", "")).strip() or profile_id,
                    base_url=base_url,
                    api_key=str(raw.get("api_key", "")).strip(),
                    models=models,
                )
            )
        return profiles

    def load_model_selection(self):
        """从 api.yaml 读取模型槽位选择；无有效数据时返回空 ModelSelectionSettings。"""
        from app.config.models import (
            MODEL_SLOT_CHAT,
            MODEL_SLOT_MEMORY_CURATION,
            MODEL_SLOT_ORDER,
            MODEL_SLOT_VISION_CHAT,
            ModelSelectionSettings,
            ModelSlotSelection,
        )

        data = load_yaml_mapping(self.api_config_path)
        raw_slots = data.get("model_slots")
        if not isinstance(raw_slots, dict):
            return ModelSelectionSettings()

        def _slot_selection(slot: str, *, required: bool) -> ModelSlotSelection | None:
            raw = raw_slots.get(slot)
            if not isinstance(raw, dict):
                return None
            profile_id = str(raw.get("profile_id", "")).strip()
            model = str(raw.get("model", "")).strip()
            if not profile_id and not model:
                return None
            if required and (not profile_id or not model):
                return None
            if not required and not profile_id:
                return None
            return ModelSlotSelection(profile_id=profile_id, model=model)

        chat = _slot_selection(MODEL_SLOT_CHAT, required=True)
        if chat is None:
            return ModelSelectionSettings()
        optional_slots = {
            slot: _slot_selection(slot, required=False)
            for slot in MODEL_SLOT_ORDER
            if slot != MODEL_SLOT_CHAT
        }
        return ModelSelectionSettings(chat=chat, **optional_slots)

    def load_local_llm_settings(self) -> LocalLlmSettings:
        data = self._api_section("local_llm")
        vision_route = str(data.get("vision_route", "cloud")).strip().lower()
        background_route = str(data.get("background_route", "cloud")).strip().lower()
        return LocalLlmSettings(
            enabled=_bool_value(data.get("enabled"), False),
            base_url=str(data.get("base_url", "")).strip(),
            api_key=str(data.get("api_key", "")).strip(),
            text_model=str(data.get("text_model", "")).strip(),
            vision_model=str(data.get("vision_model", "")).strip(),
            timeout_seconds=_int_value(data.get("timeout_seconds"), 120),
            vision_route=vision_route if vision_route in {"cloud", "local", "auto"} else "cloud",
            background_route=background_route if background_route in {"cloud", "local", "auto"} else "cloud",
        ).normalized()

    def save_local_llm_settings(self, settings: LocalLlmSettings) -> None:
        normalized = settings.normalized()
        data = load_yaml_mapping(self.api_config_path)
        local_data: dict[str, Any] = {
            "enabled": normalized.enabled,
            "base_url": normalized.base_url,
            "api_key": normalized.api_key,
            "text_model": normalized.text_model,
            "vision_model": normalized.vision_model,
            "timeout_seconds": normalized.timeout_seconds,
            "vision_route": normalized.vision_route,
            "background_route": normalized.background_route,
        }
        data["local_llm"] = local_data
        save_yaml_mapping(self.api_config_path, data)

    def load_tts_settings(
        self,
        *,
        validate_enabled: bool = True,
        character_profile: CharacterProfile | None = None,
    ) -> GPTSoVITSTTSSettings:
        data = self._api_section("tts")
        playback_backend = str(data.get("playback_backend", "")).strip()
        gpt_sovits = _mapping(data.get("gpt_sovits"))
        genie_tts = _mapping(data.get("genie_tts"))
        provider = str(data.get("provider", "")).strip().lower()
        enabled = _bool_value(data.get("enabled"), False)
        if provider in {"none", "off", "disabled", "不使用"}:
            enabled = False
            provider = TTS_PROVIDER_NONE
        elif provider in {"gpt-sovits", "gpt_sovits", "gptsovits"}:
            enabled = True
            provider = TTS_PROVIDER_GPT_SOVITS
        elif provider in {
            "custom-gpt-sovits",
            "custom_gpt_sovits",
            "custom-sovits",
            "custom_sovits",
            "external-gpt-sovits",
            "external_gpt_sovits",
            "external-sovits",
            "external_sovits",
        }:
            enabled = True
            provider = TTS_PROVIDER_CUSTOM_GPT_SOVITS
        elif provider in {"genie", "genie-tts", "genie_tts"}:
            enabled = True
            provider = TTS_PROVIDER_GENIE
        else:
            provider = TTS_PROVIDER_GPT_SOVITS if enabled else TTS_PROVIDER_NONE

        # 无语音角色不能启用 TTS，启动和设置页加载时直接降级为关闭。
        if enabled and character_profile is not None and character_profile.voice is None:
            enabled = False

        provider_data = genie_tts if provider == TTS_PROVIDER_GENIE else gpt_sovits
        default_api_url = DEFAULT_GENIE_TTS_API_URL if provider == TTS_PROVIDER_GENIE else DEFAULT_GPT_SOVITS_API_URL
        api_url = str(provider_data.get("api_url", default_api_url)).strip()
        work_dir = _optional_path(provider_data.get("work_dir"), self.base_dir)
        python_path = _optional_path(provider_data.get("python_path"), self.base_dir)
        tts_config_path = _optional_path(provider_data.get("tts_config_path"), self.base_dir)
        ref_lang = "ja"
        text_lang = "ja"
        timeout_seconds = _int_value(provider_data.get("timeout_seconds"), 60)
        onnx_model_dir = _optional_path(genie_tts.get("onnx_model_dir"), self.base_dir)
        if character_profile is not None:
            if provider == TTS_PROVIDER_GENIE and onnx_model_dir is None:
                onnx_model_dir = StoragePaths(self.base_dir).tts_bundle_onnx_for(character_profile.id)
            settings = GPTSoVITSTTSSettings.from_character_profile(
                character_profile=character_profile,
                enabled=enabled,
                api_url=api_url,
                ref_lang=ref_lang,
                text_lang=text_lang,
                timeout_seconds=timeout_seconds,
                provider=provider,
                work_dir=work_dir,
                python_path=python_path,
                tts_config_path=tts_config_path,
                onnx_model_dir=onnx_model_dir,
                validate_enabled=validate_enabled,
            )
            if playback_backend:
                settings = replace(settings, playback_backend=playback_backend)
        else:
            if provider == TTS_PROVIDER_GENIE and onnx_model_dir is None:
                onnx_model_dir = StoragePaths(self.base_dir).tts_bundle_onnx_for("default")
            settings = GPTSoVITSTTSSettings(
                enabled=enabled,
                api_url=api_url,
                ref_audio_path=self.base_dir / "ref" / "VO01_2210.ogg",
                ref_text_path=self.base_dir / "ref" / "text.txt",
                ref_text="",
                provider=provider,
                work_dir=work_dir,
                python_path=python_path,
                tts_config_path=tts_config_path,
                character_name="sakura",
                onnx_model_dir=onnx_model_dir,
                ref_lang=ref_lang,
                text_lang=text_lang,
                timeout_seconds=timeout_seconds,
            )
            if playback_backend:
                settings = replace(settings, playback_backend=playback_backend)
        if settings.enabled and validate_enabled:
            settings.validate()
        return settings

    def save_tts_settings(self, settings: GPTSoVITSTTSSettings) -> None:
        data = load_yaml_mapping(self.api_config_path)
        saved_provider = settings.provider if settings.enabled else TTS_PROVIDER_NONE
        section_provider = (
            settings.provider
            if settings.provider in {TTS_PROVIDER_GENIE, TTS_PROVIDER_GPT_SOVITS}
            else TTS_PROVIDER_GPT_SOVITS
        )
        tts_data: dict[str, object] = {
            "provider": saved_provider,
            "enabled": bool(settings.enabled),
        }
        if section_provider == TTS_PROVIDER_GENIE:
            tts_data["genie_tts"] = {
                "api_url": settings.api_url.strip() or DEFAULT_GENIE_TTS_API_URL,
                "work_dir": _path_for_config(settings.work_dir, self.base_dir),
                "onnx_model_dir": _path_for_config(settings.onnx_model_dir, self.base_dir),
                "ref_lang": settings.ref_lang.strip(),
                "text_lang": settings.text_lang.strip(),
                "timeout_seconds": int(settings.timeout_seconds),
            }
        elif section_provider == TTS_PROVIDER_GPT_SOVITS:
            tts_data["gpt_sovits"] = {
                "api_url": settings.api_url.strip(),
                "work_dir": _path_for_config(settings.work_dir, self.base_dir),
                "python_path": _path_for_config(settings.python_path, self.base_dir),
                "tts_config_path": _path_for_config(settings.tts_config_path, self.base_dir),
                "ref_lang": settings.ref_lang.strip(),
                "text_lang": settings.text_lang.strip(),
                "timeout_seconds": int(settings.timeout_seconds),
            }
        data["tts"] = tts_data
        save_yaml_mapping(self.api_config_path, data)

    def load_mcp_runtime_settings(self) -> MCPRuntimeSettings:
        mcp = self._system_section("mcp")
        return normalize_mcp_runtime_settings(
            MCPRuntimeSettings(
                windows_enabled=_bool_value(
                    mcp.get("windows_enabled"),
                    False,
                )
            )
        )

    def save_mcp_runtime_settings(self, settings: MCPRuntimeSettings) -> None:
        normalized_settings = normalize_mcp_runtime_settings(settings)
        self.save_system_values(
            "mcp",
            {"windows_enabled": bool(normalized_settings.windows_enabled)},
        )

    def load_runtime_loop_settings(self) -> RuntimeLoopSettings:
        tool_loop = self._system_section("tool_loop")
        defaults = RuntimeLoopSettings()
        return normalize_runtime_loop_settings(
            RuntimeLoopSettings(
                max_agent_steps_per_turn=_int_value(
                    tool_loop.get("max_agent_steps_per_turn"),
                    defaults.max_agent_steps_per_turn,
                ),
                max_tool_calls_per_step=_int_value(
                    tool_loop.get("max_tool_calls_per_step"),
                    defaults.max_tool_calls_per_step,
                ),
                max_tool_calls_per_turn=_int_value(
                    tool_loop.get("max_tool_calls_per_turn"),
                    defaults.max_tool_calls_per_turn,
                ),
            )
        )

    def save_runtime_loop_settings(self, settings: RuntimeLoopSettings) -> None:
        normalized = normalize_runtime_loop_settings(settings)
        self.save_system_values(
            "tool_loop",
            {
                "max_agent_steps_per_turn": int(normalized.max_agent_steps_per_turn),
                "max_tool_calls_per_step": int(normalized.max_tool_calls_per_step),
                "max_tool_calls_per_turn": int(normalized.max_tool_calls_per_turn),
            },
        )

    def load_debug_log_settings(self) -> DebugLogSettings:
        debug = self._system_section("debug")
        return DebugLogSettings(
            enabled=_bool_value(debug.get("enabled"), False),
            body_enabled=_bool_value(debug.get("body_enabled"), False),
            file_enabled=_bool_value(debug.get("file_enabled"), False),
            stage_debug_overlay=_bool_value(debug.get("stage_debug_overlay"), False),
            stage_collision_mask=_bool_value(debug.get("stage_collision_mask"), True),
        )

    def save_debug_log_settings(self, settings: DebugLogSettings) -> None:
        self.save_system_values(
            "debug",
            {
                "enabled": bool(settings.enabled),
                "body_enabled": bool(settings.body_enabled),
                "file_enabled": bool(settings.file_enabled),
                "stage_debug_overlay": bool(settings.stage_debug_overlay),
                "stage_collision_mask": bool(settings.stage_collision_mask),
            },
        )

    def load_startup_settings(self) -> StartupSettings:
        startup = self._system_section("startup")
        return StartupSettings(
            launch_at_login=_bool_value(startup.get("launch_at_login"), False),
        )

    def save_startup_settings(self, settings: StartupSettings) -> None:
        self.save_system_values(
            "startup",
            {"launch_at_login": bool(settings.launch_at_login)},
        )

    def load_theme_settings(self) -> ThemeSettings:
        ui = self._system_section("ui")
        return theme_from_mapping(ui.get("theme"))

    def save_theme_settings(self, settings: ThemeSettings) -> None:
        ui = self._system_section("ui")
        ui["theme"] = theme_to_mapping(settings)
        data = load_yaml_mapping(self.system_config_path)
        data["ui"] = ui
        save_yaml_mapping(self.system_config_path, data)

    def load_screen_awareness_settings(self) -> ScreenAwarenessSettings:
        """兼容接口：只同步主动总开关到 ScreenAwarenessSettings.enabled。

        主动看屏已由 ProactiveObserver（`proactive` 配置）接管。
        若尚未写入 proactive.enabled，则回退到旧 screen_awareness.enabled，避免迁移后误开。
        interval / cooldown / batch / resolution 等旧字段固定返回默认值，不再读写 YAML。
        """
        enabled = self._resolve_proactive_enabled(default=True)
        return ScreenAwarenessSettings(
            enabled=enabled,
            screen_context_enabled=True,  # 旧字段；Observer 不单独使用
            check_interval_minutes=SCREEN_AWARENESS_DEFAULT_CHECK_INTERVAL_MINUTES,
            cooldown_minutes=SCREEN_AWARENESS_DEFAULT_COOLDOWN_MINUTES,
            screen_context_batch_limit=SCREEN_AWARENESS_DEFAULT_SCREEN_CONTEXT_BATCH_LIMIT,
            screen_context_resolution=SCREEN_AWARENESS_DEFAULT_SCREEN_CONTEXT_RESOLUTION,
        )

    def save_screen_awareness_settings(self, settings: ScreenAwarenessSettings) -> None:
        """兼容接口：仅把 settings.enabled 写入 proactive.enabled；旧批次字段忽略。"""
        normalized = settings.normalized()
        data = load_yaml_mapping(self.system_config_path)
        proactive = data.get("proactive", {})
        if not isinstance(proactive, dict):
            proactive = {}
        proactive["enabled"] = bool(normalized.enabled)
        data["proactive"] = proactive
        save_yaml_mapping(self.system_config_path, data)

    def load_proactive_care_settings(self) -> ScreenAwarenessSettings:
        """已弃用别名；等价于 load_screen_awareness_settings（且旧批次逻辑未启用）。"""
        return self.load_screen_awareness_settings()

    def load_proactive_config(self) -> dict[str, Any]:
        """加载主动屏幕感知 ProactiveObserver 的运行时配置。"""
        proactive = self._system_section("proactive")
        result = dict(proactive) if isinstance(proactive, dict) else {}
        if "enabled" not in result:
            result["enabled"] = self._resolve_proactive_enabled(default=True)
        return normalize_proactive_config_mapping(result)

    def save_proactive_config(self, config: Mapping[str, Any] | None) -> None:
        """写入 proactive 段（合并规范化后的字段，保留未知键）。"""
        normalized = normalize_proactive_config_mapping(config)
        data = load_yaml_mapping(self.system_config_path)
        existing = data.get("proactive", {})
        if not isinstance(existing, dict):
            existing = {}
        merged = dict(existing)
        merged.update(normalized)
        data["proactive"] = merged
        save_yaml_mapping(self.system_config_path, data)

    def _resolve_proactive_enabled(self, *, default: bool) -> bool:
        """优先 proactive.enabled，其次旧 screen_awareness.enabled。"""
        proactive = self._system_section("proactive")
        if isinstance(proactive, dict) and "enabled" in proactive:
            return _bool_value(proactive.get("enabled"), default)
        legacy = self._system_section("screen_awareness")
        if isinstance(legacy, dict) and "enabled" in legacy:
            return _bool_value(legacy.get("enabled"), default)
        return default

    def save_proactive_care_settings(self, settings: ScreenAwarenessSettings) -> None:
        """已弃用别名；等价于 save_screen_awareness_settings。"""
        self.save_screen_awareness_settings(settings)

    def load_bubble_settings(self) -> BubbleSettings:
        ui = self._system_section("ui")
        return BubbleSettings(
            auto_hide_enabled=_bool_value(ui.get("bubble_auto_hide_enabled"), True),
            auto_hide_delay_seconds=_int_value(
                ui.get("bubble_auto_hide_delay_seconds"),
                BUBBLE_AUTO_HIDE_DEFAULT_DELAY_SECONDS,
            ),
        )

    def save_bubble_settings(self, settings: BubbleSettings) -> None:
        # 气泡配置位于 ui section 下，须读-改-写以保留 subtitle_language/theme 等其他 ui 键。
        normalized = settings.normalized()
        ui = self._system_section("ui")
        ui["bubble_auto_hide_enabled"] = bool(normalized.auto_hide_enabled)
        ui["bubble_auto_hide_delay_seconds"] = int(normalized.auto_hide_delay_seconds)
        data = load_yaml_mapping(self.system_config_path)
        data["ui"] = ui
        save_yaml_mapping(self.system_config_path, data)

    def load_backchannel_settings(self) -> BackchannelSettings:
        section = self._system_section("backchannel")
        return BackchannelSettings(
            enabled=_bool_value(section.get("enabled"), False),
            mode=str(section.get("mode", BACKCHANNEL_DEFAULT_MODE) or BACKCHANNEL_DEFAULT_MODE),
            delay_ms=_int_value(section.get("delay_ms"), BACKCHANNEL_DEFAULT_DELAY_MS),
            probability=_float_value(section.get("probability"), 1.0),
            tts_enabled=_bool_value(section.get("tts_enabled"), False),
            timeout_ms=_int_value(section.get("timeout_ms"), BACKCHANNEL_DEFAULT_TIMEOUT_MS),
        ).normalized()

    def save_backchannel_settings(self, settings: BackchannelSettings) -> None:
        normalized = settings.normalized()
        data = load_yaml_mapping(self.system_config_path)
        data["backchannel"] = {
            "enabled": bool(normalized.enabled),
            "mode": normalized.mode,
            "delay_ms": int(normalized.delay_ms),
            "probability": float(normalized.probability),
            "tts_enabled": bool(normalized.tts_enabled),
            "timeout_ms": int(normalized.timeout_ms),
        }
        save_yaml_mapping(self.system_config_path, data)

    def load_turn_routing_settings(self):
        from app.agent.turn_routing import TurnRoutingSettings

        section = self._system_section("turn_routing")
        return TurnRoutingSettings(
            enabled=_bool_value(section.get("enabled"), True),
            classifier_enabled=_bool_value(section.get("classifier_enabled"), False),
            backchannel_orchestration_enabled=_bool_value(
                section.get("backchannel_orchestration_enabled"), False
            ),
            simple_greeting_max_chars=_int_value(section.get("simple_greeting_max_chars"), 12),
            classifier_timeout_seconds=_int_value(section.get("classifier_timeout_seconds"), 1),
        )

    def save_turn_routing_settings(self, settings) -> None:
        data = load_yaml_mapping(self.system_config_path)
        data["turn_routing"] = {
            "enabled": bool(settings.enabled),
            "classifier_enabled": bool(settings.classifier_enabled),
            "backchannel_orchestration_enabled": bool(settings.backchannel_orchestration_enabled),
            "simple_greeting_max_chars": int(settings.simple_greeting_max_chars),
            "classifier_timeout_seconds": int(settings.classifier_timeout_seconds),
        }
        save_yaml_mapping(self.system_config_path, data)

    def load_memory_curation_settings(self):
        from app.agent.memory_curator import MemoryCurationSettings
        from app.config.defaults import (
            DEFAULT_MEMORY_CURATION_CATCH_UP_TURNS,
            DEFAULT_MEMORY_CURATION_COOLDOWN_MINUTES,
            DEFAULT_MEMORY_CURATION_IDLE_MINUTES,
            DEFAULT_MEMORY_CURATION_LONG_IDLE_MINUTES,
            DEFAULT_MEMORY_CURATION_MIN_TURNS,
            DEFAULT_MEMORY_CURATION_TRIGGER_TURNS,
        )

        memory = self._system_section("memory_curation")
        legacy_trigger = _int_value(memory.get("trigger_turns"), DEFAULT_MEMORY_CURATION_TRIGGER_TURNS)
        catch_up_turns = memory.get("catch_up_turns")
        if catch_up_turns is None:
            catch_up_turns = legacy_trigger
        return MemoryCurationSettings(
            enabled=_bool_value(memory.get("enabled"), True),
            backfill_limit=_int_value(memory.get("backfill_limit"), 200),
            idle_minutes=_int_value(memory.get("idle_minutes"), DEFAULT_MEMORY_CURATION_IDLE_MINUTES),
            min_turns=_int_value(memory.get("min_turns"), DEFAULT_MEMORY_CURATION_MIN_TURNS),
            cooldown_minutes=_int_value(
                memory.get("cooldown_minutes"),
                DEFAULT_MEMORY_CURATION_COOLDOWN_MINUTES,
            ),
            long_idle_minutes=_int_value(
                memory.get("long_idle_minutes"),
                DEFAULT_MEMORY_CURATION_LONG_IDLE_MINUTES,
            ),
            catch_up_turns=_int_value(catch_up_turns, DEFAULT_MEMORY_CURATION_CATCH_UP_TURNS),
            trigger_turns=legacy_trigger,
        ).normalized()

    def save_memory_curation_settings(self, settings) -> None:
        normalized = settings.normalized()
        self.save_system_values(
            "memory_curation",
            {
                "enabled": bool(normalized.enabled),
                "backfill_limit": int(normalized.backfill_limit),
                "idle_minutes": int(normalized.idle_minutes),
                "min_turns": int(normalized.min_turns),
                "cooldown_minutes": int(normalized.cooldown_minutes),
                "long_idle_minutes": int(normalized.long_idle_minutes),
                "catch_up_turns": int(normalized.catch_up_turns),
            },
        )

    def load_current_character_id(self, character_registry: CharacterRegistry) -> str:
        data = load_yaml_mapping(self.characters_config_path)
        configured = str(data.get("current_character_id", "")).strip()
        if configured in character_registry.profiles:
            return configured
        if DEFAULT_CHARACTER_ID in character_registry.profiles:
            return DEFAULT_CHARACTER_ID
        if character_registry.profiles:
            return next(iter(character_registry.profiles))
        raise ValueError("未找到任何角色包。")

    def save_current_character_id(
        self,
        character_registry: CharacterRegistry,
        character_id: str,
    ) -> None:
        character_registry.get(character_id)
        data = load_yaml_mapping(self.characters_config_path)
        data["current_character_id"] = character_id
        save_yaml_mapping(self.characters_config_path, data)

    def load_system_values(self, section: str) -> dict[str, Any]:
        return self._system_section(section)

    def save_system_values(self, section: str, values: dict[str, Any]) -> None:
        data = load_yaml_mapping(self.system_config_path)
        current = _mapping(data.get(section))
        current.update(values)
        data[section] = current
        save_yaml_mapping(self.system_config_path, data)

    def _api_section(self, name: str) -> dict[str, Any]:
        return _mapping(load_yaml_mapping(self.api_config_path).get(name))

    def _system_section(self, name: str) -> dict[str, Any]:
        return _mapping(load_yaml_mapping(self.system_config_path).get(name))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_path(value: Any, base_dir: Path) -> Path | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    return base_dir / path


def _path_for_config(path: Path | None, base_dir: Path) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_proactive_config_mapping(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把设置页 / YAML 的 proactive 段规整为可写入、可注入 Observer 的字典。"""
    source = dict(raw) if isinstance(raw, Mapping) else {}
    cfg = ProactiveConfig.from_dict(source)
    privacy_raw = source.get("privacy")
    if isinstance(privacy_raw, dict):
        processes = _dedupe(privacy_raw.get("blocked_processes"))
        keywords = _dedupe(privacy_raw.get("blocked_title_keywords"))
        # 缺键 → 默认黑名单；显式空列表 → 保持清空
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
        "game_ocr_enabled": bool(cfg.game_ocr_enabled),
        "max_edge": int(cfg.max_edge),
        "request_timeout": float(cfg.request_timeout),
        "eval_temperature": float(cfg.eval_temperature),
        "max_tokens": int(cfg.max_tokens),
        "adaptive_interval_min": float(cfg.adaptive_interval_min),
        "adaptive_interval_max": float(cfg.adaptive_interval_max),
        "away_max_seconds": float(cfg.away_max_seconds),
        "privacy": {
            "blocked_processes": processes,
            "blocked_title_keywords": keywords,
        },
    }


def _optional_float(value: Any, *, minimum: float, maximum: float) -> float | None:
    """解析可选浮点参数；缺省或非法返回 None，合法值 clamp 到 [minimum, maximum]。"""
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, parsed))


def _dedupe(values: object) -> list[str]:
    """去重字符串列表，保持顺序。"""
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


def _optional_positive_int(value: Any) -> int | None:
    """解析可选正整数；缺省、非法或非正返回 None。"""
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default
