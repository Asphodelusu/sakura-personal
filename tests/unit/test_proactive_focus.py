"""Focus tracking: settle, rapid switch, deferred cooldown, busy-safe."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.perception.observer import FocusSnapshot, ProactiveConfig, ProactiveObserver


def _observer(**cfg: object) -> ProactiveObserver:
    config = ProactiveConfig(
        enabled=True,
        poll_interval=0.05,
        focus_settle_delay=float(cfg.get("focus_settle_delay", 0.2)),
        window_switch_cooldown=float(cfg.get("window_switch_cooldown", 1.0)),
        timer_seconds=float(cfg.get("timer_seconds", 9999)),
        cooldown_seconds=float(cfg.get("cooldown_seconds", 600)),
        min_silence_after_user=float(cfg.get("min_silence_after_user", 0)),
        content_check_interval=float(cfg.get("content_check_interval", 9999)),
        idle_threshold_seconds=float(cfg.get("idle_threshold_seconds", 99999)),
    )
    obs = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=config,
    )
    obs._last_user_at = 0.0
    obs._last_eval_at = 0.0
    obs._last_proactive_at = 0.0
    return obs


def _collect(obs: ProactiveObserver, now: float) -> list[str]:
    with patch("app.perception.observer.time.monotonic", return_value=now):
        return asyncio.run(obs._collect_triggers())


def test_app_focus_settle_emits_ready_trigger() -> None:
    obs = _observer(focus_settle_delay=0.15, window_switch_cooldown=0.0)
    with (
        patch("app.perception.observer.get_foreground_hwnd", side_effect=[1, 2, 2]),
        patch(
            "app.perception.observer.get_active_window_process_name",
            side_effect=["a.exe", "b.exe", "b.exe"],
        ),
        patch(
            "app.perception.observer.get_active_window_title",
            side_effect=["A", "B", "B"],
        ),
    ):
        t0 = 1000.0
        obs._sync_focus_tracking(t0, seed_only=True)
        obs._sync_focus_tracking(t0 + 0.01)
        assert obs._pending_focus is not None
        assert obs._pending_focus.process == "b.exe"
        assert obs._ready_focus_trigger == ""

        obs._sync_focus_tracking(t0 + 0.20)
        assert obs._ready_focus_trigger.startswith("window:")
        assert "B" in obs._ready_focus_trigger

        triggers = _collect(obs, t0 + 0.21)
        assert any(t.startswith("window:") for t in triggers)


def test_rapid_switch_resets_settle_only_last_wins() -> None:
    obs = _observer(focus_settle_delay=0.3, window_switch_cooldown=0.0)
    titles = {"hwnd": 1, "proc": "a.exe", "title": "A"}

    def hwnd() -> int:
        return titles["hwnd"]

    def proc() -> str:
        return titles["proc"]

    def title() -> str:
        return titles["title"]

    with (
        patch("app.perception.observer.get_foreground_hwnd", side_effect=hwnd),
        patch("app.perception.observer.get_active_window_process_name", side_effect=proc),
        patch("app.perception.observer.get_active_window_title", side_effect=title),
    ):
        obs._sync_focus_tracking(100.0, seed_only=True)
        titles.update(hwnd=2, proc="b.exe", title="B")
        obs._sync_focus_tracking(100.05)
        assert obs._pending_focus is not None and obs._pending_focus.app_key.endswith("|2")

        titles.update(hwnd=3, proc="c.exe", title="C")
        obs._sync_focus_tracking(100.10)
        assert obs._pending_focus is not None and obs._pending_focus.app_key.endswith("|3")

        # C 未 settle（距离 100.10 仅 0.10 < 0.3）
        obs._sync_focus_tracking(100.20)
        assert obs._ready_focus_trigger == ""

        obs._sync_focus_tracking(100.45)
        assert "C" in obs._ready_focus_trigger
        assert "B" not in obs._ready_focus_trigger.split("->")[-1]


def test_type_cooldown_defers_then_backfills() -> None:
    obs = _observer(focus_settle_delay=0.1, window_switch_cooldown=1.0)
    obs._last_window_trigger_at = 1000.0  # 刚触发过 → 冷却中
    with (
        patch("app.perception.observer.get_foreground_hwnd", side_effect=[1, 2, 2]),
        patch(
            "app.perception.observer.get_active_window_process_name",
            side_effect=["a.exe", "game.exe", "game.exe"],
        ),
        patch(
            "app.perception.observer.get_active_window_title",
            side_effect=["A", "Game", "Game"],
        ),
    ):
        obs._sync_focus_tracking(1000.0, seed_only=True)
        obs._sync_focus_tracking(1000.2)  # 冷却内 → deferred
        assert obs._deferred_focus is not None
        assert obs._pending_focus is None
        assert obs._ready_focus_trigger == ""

        # 冷却结束 + deferred 停留时间已超过 settle → 直接补票
        obs._sync_focus_tracking(1001.1)
        assert obs._ready_focus_trigger.startswith("window:")
        assert "Game" in obs._ready_focus_trigger


def test_busy_still_tracks_and_ready_survives() -> None:
    """busy 不调用 collect，但 sync 仍走：settle 可完成，释放后补评估。"""
    obs = _observer(focus_settle_delay=0.1, window_switch_cooldown=0.0)
    busy = {"on": True}
    obs._is_busy = lambda: busy["on"] or False

    with (
        patch("app.perception.observer.get_foreground_hwnd", return_value=1),
        patch("app.perception.observer.get_active_window_process_name", return_value="a.exe"),
        patch("app.perception.observer.get_active_window_title", return_value="A"),
    ):
        obs._sync_focus_tracking(100.0, seed_only=True)

    # busy 期间切到 B：应继续跟踪并完成 settle
    with (
        patch("app.perception.observer.get_foreground_hwnd", return_value=2),
        patch("app.perception.observer.get_active_window_process_name", return_value="b.exe"),
        patch("app.perception.observer.get_active_window_title", return_value="B"),
    ):
        obs._sync_focus_tracking(100.05)
        assert obs._pending_focus is not None
        obs._sync_focus_tracking(100.20)
        assert obs._ready_focus_trigger.startswith("window:")
        assert obs._focus_current is not None
        assert obs._focus_current.process == "b.exe"

    # 模拟主循环：busy 时跳过 collect，ready 保留
    assert busy["on"]
    assert obs._ready_focus_trigger.startswith("window:")

    busy["on"] = False
    triggers = _collect(obs, 100.25)
    assert any(t.startswith("window:") for t in triggers)
    obs._consume_focus_triggers(triggers)
    assert obs._ready_focus_trigger == ""


def test_speak_cooldown_blocks_timer_but_not_focus() -> None:
    obs = _observer(focus_settle_delay=0.05, window_switch_cooldown=0.0, timer_seconds=0.01)
    obs._last_proactive_at = 9000.0
    with (
        patch("app.perception.observer.get_foreground_hwnd", side_effect=[1, 2, 2]),
        patch(
            "app.perception.observer.get_active_window_process_name",
            side_effect=["a.exe", "b.exe", "b.exe"],
        ),
        patch(
            "app.perception.observer.get_active_window_title",
            side_effect=["A", "B", "B"],
        ),
        patch("app.perception.observer.get_idle_seconds", return_value=0.0),
    ):
        obs._sync_focus_tracking(9000.0, seed_only=True)
        obs._sync_focus_tracking(9000.1)
        obs._sync_focus_tracking(9000.2)
        assert obs._ready_focus_trigger

        triggers = _collect(obs, 9000.3)
        assert any(t.startswith("window:") for t in triggers)
        assert "timer" not in triggers


def test_same_app_title_change_does_not_rearm_app_focus() -> None:
    obs = _observer(focus_settle_delay=1.0, window_switch_cooldown=0.0)
    with (
        patch("app.perception.observer.get_foreground_hwnd", return_value=9),
        patch("app.perception.observer.get_active_window_process_name", return_value="chrome.exe"),
        patch(
            "app.perception.observer.get_active_window_title",
            side_effect=["Tab1", "Tab2"],
        ),
    ):
        obs._sync_focus_tracking(1.0, seed_only=True)
        obs._sync_focus_tracking(1.1)
        assert obs._pending_focus is None
        assert obs._deferred_focus is None
        assert obs._ready_focus_trigger == ""
        assert obs._focus_current is not None
        assert obs._focus_current.title == "Tab2"


def test_focus_snapshot_app_key() -> None:
    a = FocusSnapshot(hwnd=1, process="X.EXE", title="t")
    b = FocusSnapshot(hwnd=1, process="x.exe", title="other")
    c = FocusSnapshot(hwnd=2, process="x.exe", title="t")
    assert a.app_key == b.app_key
    assert a.app_key != c.app_key


def test_rapid_switch_during_cooldown_keeps_last_deferred() -> None:
    """冷却内连续快切：deferred 只保留最后目标，冷却后按最终焦点补票。"""
    obs = _observer(focus_settle_delay=0.05, window_switch_cooldown=1.0)
    obs._last_window_trigger_at = 100.0
    focus = {"hwnd": 1, "proc": "a.exe", "title": "A"}

    def hwnd() -> int:
        return focus["hwnd"]

    def proc() -> str:
        return focus["proc"]

    def title() -> str:
        return focus["title"]

    with (
        patch("app.perception.observer.get_foreground_hwnd", side_effect=hwnd),
        patch("app.perception.observer.get_active_window_process_name", side_effect=proc),
        patch("app.perception.observer.get_active_window_title", side_effect=title),
    ):
        obs._sync_focus_tracking(100.0, seed_only=True)
        focus.update(hwnd=2, proc="b.exe", title="B")
        obs._sync_focus_tracking(100.1)
        assert obs._deferred_focus is not None
        assert obs._deferred_focus.app_key.endswith("|2")

        focus.update(hwnd=3, proc="c.exe", title="C")
        obs._sync_focus_tracking(100.2)
        assert obs._deferred_focus is not None
        assert obs._deferred_focus.app_key.endswith("|3")
        assert obs._ready_focus_trigger == ""

        # 冷却结束（>=1.0），C 已稳定超过 settle
        focus.update(hwnd=3, proc="c.exe", title="C")
        obs._sync_focus_tracking(101.2)
        assert "C" in obs._ready_focus_trigger
        assert "B" not in obs._ready_focus_trigger.split("->")[-1]
