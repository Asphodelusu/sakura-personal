"""Foreground dwell tracking for scheduled screen observation.

The state machine follows the Qt-era ProactiveObserver: app focus is process plus
window handle, fast switches reset the settle clock, a title change inside the same
app does not, and a busy UI holds a ready trigger instead of consuming it. This
module does not capture the screen or speak. Content-change, UIA, OCR and media
triggers are not represented here.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


FOCUS_SETTLE_DELAY_SECONDS = 15.0
WINDOW_SWITCH_COOLDOWN_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 5.0
IDLE_TRIGGER_SECONDS = 600.0
AWAY_MAX_SECONDS = 12 * 3600.0
_QUIET_OUTCOMES = frozenset({"privacy", "unchanged", "self"})
_COMMIT_OUTCOMES = frozenset({"failed", "submitted", *_QUIET_OUTCOMES})
DIAGNOSTIC_BYTE_LIMIT = 256 * 1024
_DIAGNOSTIC_MEMORY = 200


@dataclass(frozen=True)
class FocusSnapshot:
    hwnd: int
    process: str
    title: str
    changed_at: float
    pid: int = 0
    own_process: bool = False

    @property
    def app_key(self) -> str:
        return f"{(self.process or '').casefold()}|{int(self.hwnd)}"

    @property
    def label(self) -> str:
        return self.process or f"hwnd:{self.hwnd}"


@dataclass(frozen=True)
class FocusGate:
    enabled: bool = True
    busy: bool = False
    continuation: bool = False
    silence: bool = False
    cooldown: bool = False
    idle_seconds: float = 0.0


@dataclass
class FocusObserver:
    clock: Callable[[], float] = time.monotonic
    settle_delay: float = FOCUS_SETTLE_DELAY_SECONDS
    window_switch_cooldown: float = WINDOW_SWITCH_COOLDOWN_SECONDS
    poll_interval: float = POLL_INTERVAL_SECONDS
    timer_seconds: float = 480.0
    enabled: bool = True
    diagnostics_path: Path | None = None
    _scope: str = ""
    _current: FocusSnapshot | None = None
    _previous: FocusSnapshot | None = None
    _pending: FocusSnapshot | None = None
    _deferred: FocusSnapshot | None = None
    _settled_at: float = 0.0
    _ready_trigger: str = ""
    _last_window_trigger_at: float | None = None
    _last_timer_check: float | None = None
    _last_eval_at: float = 0.0
    _offered: list[str] = field(default_factory=list)
    _idle_armed: bool = True
    _away: bool = False
    _away_set_at: float = 0.0
    _window_eval_ok_at: dict[str, float] = field(default_factory=dict)
    _was_busy: bool = False
    _last_busy_log_at: float = 0.0
    _last_diagnostic_key: tuple[str, str, str] = ("", "", "")
    _diagnostics: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=_DIAGNOSTIC_MEMORY))

    def configure(self, *, enabled: bool, timer_seconds: float) -> None:
        self.enabled = bool(enabled)
        self.timer_seconds = max(1.0, float(timer_seconds))

    def set_diagnostics_path(self, path: Path | None) -> None:
        self.diagnostics_path = path

    def reset(self, scope: str = "") -> None:
        self._scope = scope
        self._current = None
        self._previous = None
        self._pending = None
        self._deferred = None
        self._settled_at = 0.0
        self._ready_trigger = ""
        self._last_window_trigger_at = None
        self._last_timer_check = None
        self._last_eval_at = 0.0
        self._window_eval_ok_at.clear()
        self._was_busy = False
        self._offered = []
        self._idle_armed = True
        self._away = False
        self._away_set_at = 0.0

    def advance(
        self,
        snapshot: FocusSnapshot | None,
        *,
        scope: str,
        gate: FocusGate,
        blocked_processes: tuple[str, ...] = (),
        blocked_titles: tuple[str, ...] = (),
    ) -> dict[str, str]:
        if scope != self._scope:
            preserve_away = self._scope == "" and self._away
            away_at = self._away_set_at
            self.reset(scope)
            if preserve_away:
                self._away = True
                self._away_set_at = away_at
        now = float(self.clock())
        if snapshot is not None and snapshot.changed_at <= 0:
            snapshot = FocusSnapshot(
                hwnd=snapshot.hwnd,
                process=snapshot.process,
                title=snapshot.title,
                changed_at=now,
                pid=snapshot.pid,
                own_process=snapshot.own_process,
            )
        if snapshot is not None:
            self._sync(snapshot, now)
        self.configure(enabled=gate.enabled, timer_seconds=self.timer_seconds)
        if not gate.enabled:
            return self._decision("wait", "", "disabled")
        current = self._current
        if current is not None and current.own_process:
            self._ready_trigger = ""
            return self._decision("wait", "", "self_window", process=current.label)
        privacy = _privacy_reason(current, blocked_processes, blocked_titles)
        triggers = self._collect(
            now,
            cooldown=gate.cooldown,
            silence=gate.silence,
            idle_seconds=gate.idle_seconds,
        )
        if privacy and triggers:
            self._commit(triggers, "privacy", now)
            return self._decision("wait", triggers[0], "privacy", process=current.label if current else "")
        if not triggers:
            reason = "silence" if gate.silence else "away" if self._away else "tracking"
            return self._decision("wait", "", reason, process=current.label if current else "")
        if gate.busy or gate.continuation:
            self._note_busy(now, "continuation" if gate.continuation else "busy")
            return self._decision("hold", triggers[0], "busy" if gate.busy else "continuation", process=current.label if current else "")
        if self._was_busy:
            self._was_busy = False
            self._emit("busy_resume", process=current.label if current else "", trigger="", reason="idle")
        self._offered = list(triggers)
        return self._decision("capture", triggers[0], "ready", process=current.label if current else "")

    @property
    def away_mode(self) -> bool:
        return self._away

    def set_away_mode(self, enabled: bool) -> None:
        now = float(self.clock())
        self._away = bool(enabled)
        self._away_set_at = now if enabled else 0.0
        if enabled:
            self._idle_armed = True
            self._emit("away", process=self._current.label if self._current else "", trigger="", reason="away_on")
        else:
            self._emit("away", process=self._current.label if self._current else "", trigger="", reason="away_off")

    def note_user_activity(self) -> None:
        self._idle_armed = True
        if self._away:
            self._away = False
            self._away_set_at = 0.0
            self._emit("away", process=self._current.label if self._current else "", trigger="", reason="away_off")

    def settle_attempt(self, outcome: str) -> None:
        """Finish a capture offer. Abort keeps the trigger; failure does not count as an evaluation."""
        kind = str(outcome or "").strip()
        now = float(self.clock())
        if kind == "aborted":
            self._offered = []
            self._emit("attempt_aborted", process=self._current.label if self._current else "", trigger="", reason="aborted")
            return
        if kind in {"spoke", "silent"}:
            self._emit("evaluation", process=self._current.label if self._current else "", trigger="", reason=kind)
            if kind == "spoke":
                self._idle_armed = True
            return
        if kind not in _COMMIT_OUTCOMES:
            return
        offered = list(self._offered)
        if not offered and self._ready_trigger:
            offered = [self._ready_trigger]
        self._commit(offered, kind, now)

    def diagnostics(self) -> tuple[dict[str, str], ...]:
        return tuple(self._diagnostics)

    def _decision(self, action: str, trigger: str, reason: str, *, process: str = "") -> dict[str, str]:
        if action == "capture" or reason in {"privacy", "disabled"}:
            self._emit(
                "trigger_capture" if action == "capture" else reason,
                process=process,
                trigger=trigger.split(":", 1)[0],
                reason=reason,
            )
        return {"action": action, "trigger": trigger.split(":", 1)[0], "reason": reason}

    def _note_busy(self, now: float, reason: str) -> None:
        if not self._was_busy or now - self._last_busy_log_at >= 60.0:
            self._last_busy_log_at = now
            self._emit("busy_hold", process=self._current.label if self._current else "", trigger="", reason=reason)
        self._was_busy = True

    def _sync(self, snap: FocusSnapshot, now: float) -> None:
        if self._current is None:
            self._current = snap
            return
        current = self._current
        if snap.app_key == current.app_key:
            if snap.title != current.title or snap.process != current.process or snap.pid != current.pid:
                self._current = FocusSnapshot(
                    hwnd=current.hwnd,
                    process=snap.process or current.process,
                    title=snap.title or current.title,
                    changed_at=current.changed_at,
                    pid=snap.pid or current.pid,
                    own_process=snap.own_process,
                )
                self._emit("focus_title", process=self._current.label, trigger="", reason="same_app")
            self._promote_deferred(now)
            self._finalize(now)
            return
        if snap.own_process and current.own_process:
            self._current = snap
            self._pending = None
            self._settled_at = 0.0
            self._deferred = None
            return
        if snap.own_process:
            self._previous = current
            self._current = snap
            self._ready_trigger = ""
            self._pending = None
            self._settled_at = 0.0
            self._deferred = None
            self._emit("own_process", process=snap.label, trigger="", reason="self_window")
            return
        self._previous = current
        self._current = snap
        self._ready_trigger = ""
        self._emit("focus_app_switch", process=snap.label, trigger="", reason="app_switch")
        if self._window_cooled(now):
            self._arm(snap, now)
        else:
            self._deferred = snap
            self._pending = None
            self._settled_at = 0.0
            self._emit("focus_deferred", process=snap.label, trigger="", reason="window_cooldown")
        self._promote_deferred(now)
        self._finalize(now)

    def _window_cooled(self, now: float) -> bool:
        last = self._last_window_trigger_at
        return last is None or now - last >= self.window_switch_cooldown

    def _arm(self, snap: FocusSnapshot, now: float) -> None:
        self._pending = snap
        self._settled_at = now + self.settle_delay
        self._deferred = None
        self._emit("settle_armed", process=snap.label, trigger="", reason="dwell")

    def _promote_deferred(self, now: float) -> None:
        deferred = self._deferred
        current = self._current
        if deferred is None or current is None or deferred.app_key != current.app_key:
            return
        if not self._window_cooled(now):
            return
        elapsed = max(0.0, now - deferred.changed_at)
        if elapsed >= self.settle_delay:
            self._emit_ready(now)
            self._deferred = None
            self._pending = None
            self._settled_at = 0.0
        else:
            self._pending = current
            self._settled_at = deferred.changed_at + self.settle_delay
            self._deferred = None

    def _finalize(self, now: float) -> None:
        if self._settled_at <= 0 or now < self._settled_at:
            return
        pending = self._pending
        current = self._current
        if pending is not None and current is not None and pending.app_key == current.app_key:
            self._emit_ready(now)
        self._pending = None
        self._settled_at = 0.0

    def _emit_ready(self, now: float) -> None:
        current = self._current
        if current is None or current.own_process:
            return
        self._ready_trigger = "window"
        self._last_window_trigger_at = now
        self._emit("settle_ready", process=current.label, trigger="window", reason="dwell")

    def _collect(self, now: float, *, cooldown: bool, silence: bool, idle_seconds: float) -> list[str]:
        if self._away:
            if self._away_set_at and now - self._away_set_at >= AWAY_MAX_SECONDS:
                self._away = False
                self._away_set_at = 0.0
            else:
                return []
        if silence:
            return []
        if idle_seconds < IDLE_TRIGGER_SECONDS:
            self._idle_armed = True
        triggers: list[str] = []
        if self._last_timer_check is None:
            self._last_timer_check = now
        if self._ready_trigger:
            app_key = self._current.app_key if self._current else ""
            ok_at = self._window_eval_ok_at.get(app_key) if app_key else None
            if ok_at is not None and now < ok_at:
                self._ready_trigger = ""
            else:
                triggers.append(self._ready_trigger)
        throttle = now - self._last_eval_at < self.poll_interval * 1.5
        if throttle and not triggers:
            return []
        if throttle and triggers:
            return triggers
        if not cooldown and self._settled_at == 0 and not self._ready_trigger:
            last_timer = now if self._last_timer_check is None else self._last_timer_check
            if now >= last_timer + self.timer_seconds:
                triggers.append("timer")
        if (
            not triggers
            and not cooldown
            and self._settled_at == 0
            and idle_seconds >= IDLE_TRIGGER_SECONDS
            and self._idle_armed
        ):
            triggers.append("idle")
        return triggers

    def _commit(self, triggers: list[str], outcome: str, now: float) -> None:
        self._offered = []
        if any(trigger.startswith("window") for trigger in triggers):
            self._ready_trigger = ""
        if outcome == "submitted":
            self._last_eval_at = now
            if any(trigger == "timer" for trigger in triggers):
                self._last_timer_check = now
            if any(trigger == "idle" for trigger in triggers):
                self._idle_armed = False
            if any(trigger.startswith("window") for trigger in triggers) and self._current is not None:
                self._window_eval_ok_at[self._current.app_key] = now + self.timer_seconds
        elif outcome == "failed":
            if any(trigger == "timer" for trigger in triggers):
                self._last_timer_check = now
            if any(trigger == "idle" for trigger in triggers):
                self._idle_armed = False
        self._emit(
            "evaluation" if outcome == "submitted" else outcome,
            process=self._current.label if self._current else "",
            trigger=triggers[0].split(":", 1)[0] if triggers else "",
            reason=outcome,
        )

    def _emit(self, kind: str, *, process: str, trigger: str, reason: str) -> None:
        key = (kind, process, reason)
        if key == self._last_diagnostic_key and kind in {"own_process", "self_window", "disabled", "privacy"}:
            return
        self._last_diagnostic_key = key
        record = {"kind": kind, "process": process, "trigger": trigger, "reason": reason}
        self._diagnostics.append(record)
        _append_diagnostic(self.diagnostics_path, record)


def snapshot_from_mapping(value: Mapping[str, Any] | None) -> FocusSnapshot | None:
    if value is None:
        return None
    return FocusSnapshot(
        hwnd=int(value.get("hwnd") or 0),
        process=str(value.get("process") or ""),
        title=str(value.get("title") or ""),
        changed_at=float(value.get("changedAt") or 0.0),
        pid=int(value.get("pid") or 0),
        own_process=bool(value.get("ownProcess")),
    )


def _privacy_reason(
    snapshot: FocusSnapshot | None,
    processes: tuple[str, ...],
    titles: tuple[str, ...],
) -> str:
    if snapshot is None:
        return ""
    process = snapshot.process.casefold()
    if process and process in {item.casefold() for item in processes}:
        return "privacy"
    title = snapshot.title.casefold()
    if title and any(keyword and keyword.casefold() in title for keyword in titles):
        return "privacy"
    return ""


def _append_diagnostic(path: Path | None, record: Mapping[str, str]) -> None:
    if path is None:
        return
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size + len(line.encode("utf-8")) > DIAGNOSTIC_BYTE_LIMIT:
            rotated = path.with_name(path.name + ".1")
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    except OSError:
        return
