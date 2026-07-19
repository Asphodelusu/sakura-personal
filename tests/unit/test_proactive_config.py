"""ProactiveConfig.from_dict defaults must match dataclass fields."""

from __future__ import annotations

from app.perception.observer import ProactiveConfig


def test_proactive_config_from_dict_empty_matches_defaults() -> None:
    base = ProactiveConfig()
    loaded = ProactiveConfig.from_dict({})
    assert loaded.enabled == base.enabled
    assert loaded.timer_seconds == base.timer_seconds
    assert loaded.cooldown_seconds == base.cooldown_seconds
    assert loaded.min_silence_after_user == base.min_silence_after_user
    assert loaded.max_edge == base.max_edge
    assert loaded.request_timeout == base.request_timeout
    assert loaded.focus_settle_delay == base.focus_settle_delay
    assert loaded.window_switch_cooldown == base.window_switch_cooldown
    assert loaded.content_check_interval == base.content_check_interval
    assert loaded.content_min_chars == base.content_min_chars
    assert loaded.game_ocr_enabled == base.game_ocr_enabled
    assert base.focus_settle_delay == 15
    assert base.window_switch_cooldown == 25


def test_proactive_config_from_dict_overrides() -> None:
    loaded = ProactiveConfig.from_dict(
        {
            "enabled": False,
            "timer_seconds": 120,
            "max_edge": 768,
            "min_silence_after_user": 5,
            "content_check_interval": 45,
            "game_ocr_enabled": False,
        }
    )
    assert loaded.enabled is False
    assert loaded.timer_seconds == 120
    assert loaded.max_edge == 768
    assert loaded.min_silence_after_user == 5
    assert loaded.content_check_interval == 45
    assert loaded.game_ocr_enabled is False
    assert loaded.cooldown_seconds == ProactiveConfig().cooldown_seconds


def test_looks_like_game_context_skips_browsers() -> None:
    from unittest.mock import patch

    from app.perception.observer import ProactiveObserver
    from app.perception.screen_reader import WindowText

    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
    )
    empty = WindowText(is_accessible=False, app_type="custom_ui")
    with patch(
        "app.perception.observer.get_active_window_process_name",
        return_value="chrome.exe",
    ):
        assert observer._looks_like_game_context(empty) is False
    with patch(
        "app.perception.observer.get_active_window_process_name",
        return_value="MyGalGame.exe",
    ):
        assert observer._looks_like_game_context(empty) is True
    with patch(
        "app.perception.observer.get_active_window_process_name",
        return_value="unityplayer.exe",
    ):
        assert observer._looks_like_game_context(WindowText(is_accessible=True, text_content="hi")) is True
