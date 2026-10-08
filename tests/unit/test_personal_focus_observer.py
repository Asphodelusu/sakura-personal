"""Focus dwell follows the Qt observer: settle, fast-switch, busy hold, privacy."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.focus_observer import FocusGate, FocusObserver, FocusSnapshot
from app.agent.runtime import AgentRuntime
from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.core_host.screen_awareness_settings import ScreenAwarenessSettingsBoundary
import importlib.util

_DIAGNOSTICS = Path(__file__).resolve().parents[2] / "tools" / "observer_diagnostics.py"
_SPEC = importlib.util.spec_from_file_location("observer_diagnostics", _DIAGNOSTICS)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
read_new_lines = _MODULE.read_new_lines

GENERATION_ID = "00000000-0000-4000-8000-000000004501"
GENERATION_CREDENTIAL = "45" * 16


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def snap(hwnd: int, process: str, title: str = "", *, own: bool = False) -> FocusSnapshot:
    return FocusSnapshot(hwnd=hwnd, process=process, title=title, changed_at=0.0, pid=hwnd, own_process=own)


def gate(**overrides: bool) -> FocusGate:
    return FocusGate(enabled=True, **overrides)


def observer(clock: Clock, *, timer: float = 480.0) -> FocusObserver:
    item = FocusObserver(clock=clock, timer_seconds=timer)
    item.configure(enabled=True, timer_seconds=timer)
    return item


def test_stable_dwell_emits_one_window_trigger_and_a_title_change_does_not_reset_it() -> None:
    clock = Clock()
    item = observer(clock)
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["action"] == "wait"
    clock.now = 1
    item.advance(snap(2, "browser.exe", "one"), scope="a", gate=gate())
    clock.now = 6
    titled = item.advance(snap(2, "browser.exe", "two"), scope="a", gate=gate())
    assert titled["action"] == "wait"
    assert any(record["kind"] == "focus_title" for record in item.diagnostics())
    clock.now = 16
    decision = item.advance(snap(2, "browser.exe", "two"), scope="a", gate=gate())
    assert decision == {"action": "capture", "trigger": "window", "reason": "ready"}
    assert all("two" not in record["process"] for record in item.diagnostics())


def test_fast_switch_resets_settle_and_cooldown_defers_the_next_app() -> None:
    clock = Clock()
    item = observer(clock)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 1
    item.advance(snap(2, "browser.exe"), scope="a", gate=gate())
    clock.now = 10
    item.advance(snap(3, "notes.exe"), scope="a", gate=gate())
    clock.now = 16
    assert item.advance(snap(3, "notes.exe"), scope="a", gate=gate())["action"] == "wait"
    clock.now = 25
    assert item.advance(snap(3, "notes.exe"), scope="a", gate=gate())["trigger"] == "window"
    clock.now = 30
    deferred = item.advance(snap(4, "mail.exe"), scope="a", gate=gate())
    assert deferred["action"] == "wait"
    clock.now = 90
    assert item.advance(snap(4, "mail.exe"), scope="a", gate=gate())["trigger"] == "window"


def test_busy_holds_the_trigger_and_idle_resumes_without_a_new_switch() -> None:
    clock = Clock()
    item = observer(clock)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 1
    item.advance(snap(2, "browser.exe"), scope="a", gate=gate())
    clock.now = 20
    held = item.advance(snap(2, "browser.exe"), scope="a", gate=gate(busy=True))
    assert held["action"] == "hold"
    clock.now = 21
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate(busy=True))["action"] == "hold"
    clock.now = 22
    resumed = item.advance(snap(2, "browser.exe"), scope="a", gate=gate())
    assert resumed["action"] == "capture"
    assert any(record["kind"] == "busy_resume" for record in item.diagnostics())


def test_timer_waits_out_speech_cooldown_but_a_settled_window_does_not() -> None:
    clock = Clock()
    item = observer(clock, timer=30)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 30
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate(cooldown=True))["action"] == "wait"
    clock.now = 31
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == "timer"
    clock.now = 32
    item.advance(snap(2, "browser.exe"), scope="a", gate=gate(cooldown=True))
    clock.now = 48
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate(cooldown=True))["trigger"] == "window"


def test_privacy_and_own_process_do_not_capture_or_record_the_title() -> None:
    clock = Clock()
    item = observer(clock)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 1
    item.advance(snap(2, "vault.exe", "Account Secret"), scope="a", gate=gate(), blocked_processes=("vault.exe",))
    clock.now = 20
    blocked = item.advance(
        snap(2, "vault.exe", "Account Secret"),
        scope="a",
        gate=gate(),
        blocked_processes=("vault.exe",),
    )
    assert blocked == {"action": "wait", "trigger": "window", "reason": "privacy"}
    assert all("Secret" not in "".join(record.values()) for record in item.diagnostics())
    clock.now = 21
    assert item.advance(
        snap(2, "vault.exe", "Account Secret"),
        scope="a",
        gate=gate(),
        blocked_processes=("vault.exe",),
    )["trigger"] == ""
    clock.now = 40
    own = item.advance(snap(9, "sakura.exe", "Sakura", own=True), scope="a", gate=gate())
    assert own["reason"] == "self_window"


def test_first_scope_bind_keeps_a_goodbye_and_a_later_scope_clears_it() -> None:
    clock = Clock()
    item = observer(clock)
    item.set_away_mode(True)
    assert item.advance(snap(1, "editor.exe"), scope="generation-a", gate=gate())["reason"] == "away"
    assert item.away_mode is True
    assert item.advance(snap(1, "editor.exe"), scope="generation-b", gate=gate())["reason"] != "away"
    assert item.away_mode is False


def test_scope_change_drops_a_pending_dwell() -> None:
    clock = Clock()
    item = observer(clock)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 10
    item.advance(snap(2, "browser.exe"), scope="a", gate=gate())
    clock.now = 12
    assert item.advance(snap(2, "browser.exe"), scope="b", gate=gate())["action"] == "wait"
    clock.now = 30
    assert item.advance(snap(2, "browser.exe"), scope="b", gate=gate())["trigger"] == ""


def test_unsettled_capture_survives_a_busy_sample_and_failure_is_not_speech() -> None:
    clock = Clock()
    item = observer(clock)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 1
    item.advance(snap(2, "browser.exe"), scope="a", gate=gate())
    clock.now = 20
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["action"] == "capture"
    clock.now = 21
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate(busy=True))["action"] == "hold"
    clock.now = 22
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["action"] == "capture"
    item.settle_attempt("aborted")
    clock.now = 23
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["trigger"] == "window"
    item.settle_attempt("failed")
    assert not any(record["reason"] == "spoke" for record in item.diagnostics())
    clock.now = 24
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["trigger"] == ""


def test_idle_fires_once_and_explicit_away_suppresses_triggers() -> None:
    clock = Clock()
    item = observer(clock, timer=10_000)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 30
    first = item.advance(snap(1, "editor.exe"), scope="a", gate=FocusGate(enabled=True, idle_seconds=600))
    assert first["trigger"] == "idle"
    item.settle_attempt("submitted")
    clock.now = 31
    second = item.advance(snap(1, "editor.exe"), scope="a", gate=FocusGate(enabled=True, idle_seconds=600))
    assert second["trigger"] == ""
    item.note_user_activity()
    clock.now = 40
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=FocusGate(enabled=True, idle_seconds=10))["trigger"] == ""
    clock.now = 50
    item.set_away_mode(True)
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["reason"] == "away"
    item.note_user_activity()
    clock.now = 70
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["trigger"] == "window"
    item.settle_attempt("submitted")
    item.settle_attempt("silent")
    assert any(record["kind"] == "evaluation" and record["reason"] == "silent" for record in item.diagnostics())


def test_diagnostics_file_is_utf8_and_reopens_after_rotation(tmp_path: Path) -> None:
    path = tmp_path / "observer-diagnostics.jsonl"
    clock = Clock()
    item = observer(clock)
    item.set_diagnostics_path(path)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 1
    item.advance(snap(2, "记事本.exe"), scope="a", gate=gate())
    lines, path_cursor = read_new_lines(path, 0)
    assert any("记事本.exe" in line for line in lines)
    partial = tmp_path / "partial.jsonl"
    prefix = '{"kind":"focus_app_switch","process":"'.encode() + "记".encode()[:2]
    partial.write_bytes(prefix)
    partial_lines, cursor = read_new_lines(partial, None)
    assert partial_lines == []
    with partial.open("ab") as handle:
        handle.write("记".encode()[2:] + '事本.exe","trigger":"","reason":"app_switch"}\n'.encode())
    partial_lines, _cursor = read_new_lines(partial, cursor)
    assert partial_lines == ['{"kind":"focus_app_switch","process":"记事本.exe","trigger":"","reason":"app_switch"}']
    rendered = _MODULE.format_observer_event(partial_lines[0]).encode("utf-8")
    assert "focus_app_switch".encode() in rendered
    assert "app_switch".encode() in rendered
    assert "记事本.exe".encode() in rendered
    replacement = tmp_path / "replacement.jsonl"
    path.replace(replacement)
    path.write_text(
        '{"kind":"rotated","process":"","trigger":"","reason":"next"}\n{"kind":"again","process":"","trigger":"","reason":"later"}\n',
        encoding="utf-8",
    )
    rotated, _cursor = read_new_lines(path, path_cursor)
    assert rotated[0] == '{"kind":"rotated","process":"","trigger":"","reason":"next"}'
    assert len(rotated) == 2


def test_open_time_rotation_reads_the_new_file_from_the_start(tmp_path: Path, monkeypatch) -> None:
    import pathlib

    new_line = b'{"kind":"document","reason":"must_read_full_line"}\n'
    cut = new_line.index(b'ment","reason"')
    path = tmp_path / "observer-diagnostics.jsonl"
    path.write_bytes(b"x" * (cut - 1) + b"\n")
    _lines, cursor = read_new_lines(path, None)
    assert cursor["offset"] == cut
    original_open = pathlib.Path.open
    swapped = False

    def open_after_replace(self, *args, **kwargs):
        nonlocal swapped
        if self == path and not swapped:
            swapped = True
            if self.exists():
                self.rename(tmp_path / "rotated.jsonl")
            self.write_bytes(new_line)
        if self.name == "gone.jsonl":
            raise FileNotFoundError(self)
        return original_open(self, *args, **kwargs)

    gone = tmp_path / "gone.jsonl"
    gone.write_bytes(b"x\n")
    monkeypatch.setattr(pathlib.Path, "open", open_after_replace)
    lines, _cursor = read_new_lines(path, cursor)
    assert lines == [new_line.decode("utf-8").rstrip("\n")]
    assert read_new_lines(gone, cursor)[0] == []


def test_runtime_advance_uses_the_screen_gate_and_boundary(tmp_path: Path) -> None:
    clock = Clock()
    clock.now = 100
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=False),
        client=None,
        clock=clock,
        idle_seconds=lambda: 0,
    )
    runtime.configure_screen_initiative(enabled=True, cooldown_seconds=600)
    runtime.set_focus_diagnostics_path(None)
    first = runtime.advance_focus(
        {"hwnd": 1, "pid": 1, "process": "editor.exe", "title": "", "ownProcess": False},
        scope="generation-a",
        busy=False,
        timer_seconds=480,
    )
    assert first["reason"] == "silence"
    clock.now = 130
    runtime.advance_focus(
        {"hwnd": 2, "pid": 2, "process": "browser.exe", "title": "page", "ownProcess": False},
        scope="generation-a",
        busy=True,
        timer_seconds=480,
    )
    clock.now = 150
    decision = runtime.advance_focus(
        {"hwnd": 2, "pid": 2, "process": "browser.exe", "title": "page", "ownProcess": False},
        scope="generation-a",
        busy=False,
        timer_seconds=480,
    )
    assert decision["action"] == "capture"

    boundary = ScreenAwarenessSettingsBoundary(
        GENERATION_ID,
        GENERATION_CREDENTIAL,
        tmp_path,
        session_provider=lambda: type("Session", (), {"runtime": runtime})(),
    )
    response = boundary.handle({
        "id": "focus",
        "name": "screen_awareness.focus.advance",
        "generationId": GENERATION_ID,
        "generationCredential": GENERATION_CREDENTIAL,
        "protocolMajor": 2,
        "protocolMinor": 2,
        "payload": {"busy": False, "scope": "generation-a"},
    })
    assert response["payload"]["action"] in {"wait", "capture", "hold"}
    assert "title" not in response["payload"]


def test_personal_acceptance_waits_for_perception_before_arming_timing() -> None:
    clock = Clock()
    item = observer(clock, timer=100)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 100
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == "timer"
    item.settle_attempt("submitted", personal=True)
    clock.now = 101
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == ""
    assert item.next_timer_at == 0
    assert item.publish_perception(scope="a", app_key="editor.exe|1", interval=600, content_quiet=600)
    assert item.next_timer_at == 701
    assert item.content_quiet_until == 701
    clock.now = 200
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == ""
    clock.now = 701
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == "timer"
    assert item.next_timer_at == 701
    assert not item.publish_perception(scope="other", app_key="editor.exe|1", interval=10, content_quiet=10)
    assert item.next_timer_at == 701


def test_personal_idle_stays_one_shot_and_new_focus_bypasses_cooldown() -> None:
    clock = Clock()
    item = observer(clock, timer=10_000)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 30
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=FocusGate(enabled=True, idle_seconds=600))["trigger"] == "idle"
    item.settle_attempt("submitted", personal=True)
    clock.now = 31
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=FocusGate(enabled=True, idle_seconds=600))["trigger"] == ""
    item.release_perception_hold()
    clock.now = 40
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=FocusGate(enabled=True, cooldown=True, idle_seconds=600))["trigger"] == ""

    clock.now = 100
    item.advance(snap(2, "browser.exe"), scope="a", gate=gate())
    clock.now = 120
    assert item.advance(snap(2, "browser.exe"), scope="a", gate=gate())["trigger"] == "window"
    assert item.publish_perception(scope="a", app_key="browser.exe|2", interval=600, content_quiet=600)
    clock.now = 121
    item.advance(snap(3, "notes.exe"), scope="a", gate=FocusGate(enabled=True, cooldown=True))
    clock.now = 190
    assert item.advance(snap(3, "notes.exe"), scope="a", gate=FocusGate(enabled=True, cooldown=True))["trigger"] == "window"


@pytest.mark.parametrize("accepted_first", [True, False])
def test_failed_perception_cannot_be_held_again_by_late_acceptance(accepted_first) -> None:
    clock = Clock()
    item = observer(clock, timer=100)
    item.advance(snap(1, "editor.exe"), scope="a", gate=gate())
    clock.now = 100
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == "timer"
    if accepted_first:
        item.settle_attempt("submitted", personal=True)
    item.release_perception_hold()
    if not accepted_first:
        item.settle_attempt("submitted", personal=True)
    assert item.next_timer_at == 0
    clock.now = 500
    assert item.advance(snap(1, "editor.exe"), scope="a", gate=gate())["trigger"] == "timer"
