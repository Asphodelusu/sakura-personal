from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.ui.tauri_settings import (
    TAURI_SETTINGS_PROTOCOL_VERSION,
    TauriSettingsProcess,
    parse_tauri_settings_payload,
)


def _minimal_settings_payload(*, nonce: str = "test-nonce") -> dict:
    return {
        "version": TAURI_SETTINGS_PROTOCOL_VERSION,
        "nonce": nonce,
        "screen_awareness": {
            "enabled": True,
            "screen_context_enabled": True,
            "check_interval_minutes": 3,
            "cooldown_minutes": 5,
            "screen_context_batch_limit": 4,
            "screen_context_resolution": "1080p",
        },
        "mcp": {"windows_enabled": False},
        "runtime_loop": {
            "max_agent_steps_per_turn": 8,
            "max_tool_calls_per_step": 4,
            "max_tool_calls_per_turn": 6,
        },
        "system_basic": {
            "debug_log": {
                "enabled": True,
                "body_enabled": False,
                "file_enabled": True,
                "profile": "info",
                "stage_debug_overlay": False,
                "stage_collision_mask": True,
            },
            "ui": {
                "subtitle_typing_interval_ms": 40,
                "reply_segment_pause_ms": 120,
                "speech_font_size": 18,
                "name_font_size": 11,
                "input_font_size": 13,
                "button_font_size": 12,
            },
            "bubble": {
                "auto_hide_enabled": True,
                "auto_hide_delay_seconds": 6,
            },
        },
        "theme": {
            "primary_color": "#ff6b9d",
            "primary_hover_color": "#ff86b0",
            "accent_color": "#ffd166",
            "text_color": "#1f1f1f",
            "secondary_text_color": "#4a4a4a",
            "muted_text_color": "#7a7a7a",
            "page_background_color": "#f7f7f7",
            "panel_background_color": "#ffffff",
            "input_background_color": "#ffffff",
            "bubble_background_color": "#fff0f6",
            "border_color": "#e5e5e5",
            "ai_enabled": False,
            "visual_effect_mode": "none",
        },
        "theme_changed": False,
        "character": {
            "current_character_id": "Sakura",
            "layout": {
                "portrait_scale_percent": 88,
                "control_panel_width": 360,
                "bubble_height": 180,
                "control_panel_vertical_offset": 0,
                "input_bar_offset": 0,
            },
        },
        "api": {
            "settings": {
                "timeout_seconds": 60,
                "temperature": 0.8,
                "top_p": None,
                "max_tokens": None,
            },
            "profiles": [
                {
                    "id": "default",
                    "alias": "默认",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "secret",
                    "models": ["demo-model"],
                }
            ],
            "model_selection": {
                "slots": {
                    "chat": {"profile_id": "default", "model": "demo-model"},
                    "vision_chat": {"profile_id": "", "model": ""},
                    "memory_curation": {"profile_id": "", "model": ""},
                }
            },
        },
        "tts": {
            "enabled": False,
            "provider": "none",
            "api_url": "",
            "work_dir": "",
            "python_path": "",
            "tts_config_path": "",
            "timeout_seconds": 60,
        },
        "system_extra": {
            "startup": {
                "launch_at_login": False,
                "launch_at_login_supported": True,
            },
            "backchannel": {
                "enabled": False,
                "mode": "rule",
                "delay_ms": 300,
                "probability": 1.0,
                "tts_enabled": False,
                "timeout_ms": 5000,
            },
        },
        "memory": {
            "curation": {
                "trigger_turns": 2,
                "backfill_limit": 200,
            }
        },
        "plugins": {
            "enabled_by_id": {},
            "settings_by_id": {},
        },
    }


def test_parse_tauri_settings_payload_accepts_minimal_roundtrip() -> None:
    raw = _minimal_settings_payload()
    result = parse_tauri_settings_payload(raw, expected_nonce="test-nonce")

    assert result.screen_awareness.check_interval_minutes == 3
    assert result.screen_awareness.screen_context_resolution == "1080p"
    assert result.character.character_id == "Sakura"
    assert result.api.profiles[0].id == "default"


def test_tauri_settings_process_persist_handler_called_on_apply() -> None:
    calls: list[tuple[object, bool]] = []

    def handler(result: object, final: bool) -> bool:
        calls.append((result, final))
        return True

    process = TauriSettingsProcess(
        base_dir=Path("."),
        settings=MagicMock(),
        persist_handler=handler,
    )
    process._nonce = "test-nonce"
    raw = _minimal_settings_payload()
    result = parse_tauri_settings_payload(raw, expected_nonce="test-nonce")

    process._on_apply_requested("req-1", result)

    assert len(calls) == 1
    assert calls[0][1] is False


def test_tauri_settings_process_persist_handler_failure_surfaces_on_apply() -> None:
    process = TauriSettingsProcess(
        base_dir=Path("."),
        settings=MagicMock(),
        persist_handler=lambda _result, _final: False,
    )
    process._nonce = "test-nonce"
    raw = _minimal_settings_payload()
    result = parse_tauri_settings_payload(raw, expected_nonce="test-nonce")
    process._process = MagicMock()
    process._done = False

    process._on_apply_requested("req-2", result)

    process._process.write.assert_called_once()
    line = process._process.write.call_args[0][0].decode("utf-8")
    payload = json.loads(line.split("@@SAKURA_SETTINGS_RPC_RESULT@@", 1)[1].strip())
    assert payload["ok"] is False
