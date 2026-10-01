"""Foreground dwell tracking for scheduled screen observation.

The state machine follows the Qt-era ProactiveObserver: app focus is process plus
window handle, fast switches reset the settle clock, a title change inside the same
app does not, and a busy UI holds a ready trigger instead of consuming it. This
module consumes bounded visible text but does not collect it, capture or speak.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


FOCUS_SETTLE_DELAY_SECONDS = 15.0
WINDOW_SWITCH_COOLDOWN_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 5.0
IDLE_TRIGGER_SECONDS = 600.0
AWAY_MAX_SECONDS = 12 * 3600.0
_QUIET_OUTCOMES = frozenset({"privacy", "self"})
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
    visible_text: str | None = None

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
    window_switch_enabled: bool = True
    window_switch_cooldown: float = WINDOW_SWITCH_COOLDOWN_SECONDS
    poll_interval: float = POLL_INTERVAL_SECONDS
    timer_seconds: float = 480.0
    idle_threshold_seconds: float = IDLE_TRIGGER_SECONDS
    away_max_seconds: float = AWAY_MAX_SECONDS
    enabled: bool = True
    diagnostics_path: Path | None = None
    content_check_interval: float = 30.0
    content_min_chars: int = 30
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
    _next_timer_at: float = 0.0
    _content_quiet_until: float = 0.0
    _perception_hold: bool = False
    _perception_settled: bool = False
    _held_triggers: list[str] = field(default_factory=list)
    _content_baseline: str = ""
    _content_pending: bool = False
    _last_content_check_at: float | None = None
    _last_visual: tuple[str, str, str, str] | None = None

    def configure(
        self,
        *,
        enabled: bool,
        timer_seconds: float,
        settle_delay: float | None = None,
        window_switch_enabled: bool | None = None,
        window_switch_cooldown: float | None = None,
        poll_interval: float | None = None,
        idle_threshold_seconds: float | None = None,
        away_max_seconds: float | None = None,
        content_check_interval: float | None = None,
        content_min_chars: int | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.timer_seconds = max(1.0, float(timer_seconds))
        if settle_delay is not None:
            self.settle_delay = max(0.0, float(settle_delay))
        if window_switch_enabled is not None:
            self.window_switch_enabled = bool(window_switch_enabled)
            if not self.window_switch_enabled:
                self._pending = None
                self._deferred = None
                self._settled_at = 0.0
                self._ready_trigger = ""
        if window_switch_cooldown is not None:
            self.window_switch_cooldown = max(0.0, float(window_switch_cooldown))
        if poll_interval is not None:
            self.poll_interval = max(0.0, float(poll_interval))
        if idle_threshold_seconds is not None:
            self.idle_threshold_seconds = max(0.0, float(idle_threshold_seconds))
        if away_max_seconds is not None:
            self.away_max_seconds = max(0.0, float(away_max_seconds))
        if content_check_interval is not None:
            self.content_check_interval = max(0.2, float(content_check_interval))
        if content_min_chars is not None:
            self.content_min_chars = max(1, int(content_min_chars))

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
        self._next_timer_at = 0.0
        self._content_quiet_until = 0.0
        self._perception_hold = False
        self._perception_settled = False
        self._held_triggers = []
        self._clear_content()
        self._last_visual = None

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
            snapshot = replace(snapshot, changed_at=now)
        restricted = (
            not gate.enabled or gate.busy or gate.continuation or gate.silence
            or self._away or (snapshot is not None and snapshot.own_process)
            or bool(_privacy_reason(snapshot, blocked_processes, blocked_titles))
        )
        if restricted:
            if not gate.enabled or self._away or (snapshot is not None and snapshot.own_process) or _privacy_reason(snapshot, blocked_processes, blocked_titles):
                self._last_visual = None
                self._clear_content()
            if snapshot is not None:
                snapshot = replace(snapshot, visible_text=None)
        if snapshot is not None:
            self._sync(snapshot, now)
            if snapshot.visible_text is not None:
                self._read_content(snapshot.visible_text, now)
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
        self._perception_settled = False
        return self._decision("capture", triggers[0], "ready", process=current.label if current else "")

    @property
    def away_mode(self) -> bool:
        return self._away

    def set_away_mode(self, enabled: bool) -> None:
        now = float(self.clock())
        self._away = bool(enabled)
        self._away_set_at = now if enabled else 0.0
        if enabled:
            self._clear_content()
            self._last_visual = None
            self.release_perception_hold()
            self._offered = []
            self._held_triggers = []
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

    def settle_attempt(self, outcome: str, *, personal: bool = False) -> None:
        """Finish a capture offer. Abort keeps the trigger; failure does not count as an evaluation."""
        kind = str(outcome or "").strip()
        now = float(self.clock())
        if personal and kind == "submitted":
            self._accept_without_success(now)
            return
        if kind == "aborted":
            self._perception_hold = False
            self._offered = []
            self._emit("attempt_aborted", process=self._current.label if self._current else "", trigger="", reason="aborted")
            return
        if kind in {"spoke", "silent"}:
            self._perception_hold = False
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

    @property
    def next_timer_at(self) -> float:
        return self._next_timer_at

    @property
    def content_quiet_until(self) -> float:
        return self._content_quiet_until

    def focus_identity(self) -> tuple[str, str, str, bool, int, str]:
        current = self._current
        if current is None:
            return self._scope, "", "", False, 0, ""
        return self._scope, current.app_key, current.process, bool(current.own_process), current.pid, current.title

    def offer_trigger(self) -> str:
        raw = ""
        if self._offered:
            raw = self._offered[0]
        elif self._held_triggers:
            raw = self._held_triggers[0]
        elif self._ready_trigger:
            raw = self._ready_trigger
        return raw.split(":", 1)[0]

    def publish_perception(
        self,
        *,
        scope: str,
        app_key: str,
        interval: float,
        content_quiet: float,
        visible_text: str | None = None,
    ) -> bool:
        """Arm adaptive timing only after validated perception for the same focus."""
        current_key = self._current.app_key if self._current is not None else ""
        if scope != self._scope or current_key != app_key:
            return False
        text = visible_text if visible_text is not None else (self._current.visible_text if self._current else None)
        if text is not None and len(text.strip()) >= self.content_min_chars:
            self._content_baseline = text.strip()[:2000]
            latest = self._current.visible_text if self._current else None
            self._content_pending = bool(latest and latest.strip() != self._content_baseline)
        now = float(self.clock())
        self._next_timer_at = now + float(interval)
        if app_key:
            self._window_eval_ok_at[app_key] = now + float(interval)
        quiet_until = now + float(content_quiet)
        if quiet_until > self._content_quiet_until:
            self._content_quiet_until = quiet_until
        self._perception_hold = False
        self._perception_settled = True
        self._last_eval_at = now
        return True

    def release_perception_hold(self) -> None:
        self._perception_hold = False
        self._perception_settled = True

    def content_read_allowed(
        self, gate: FocusGate, *, blocked_processes: tuple[str, ...] = (),
        blocked_titles: tuple[str, ...] = (),
    ) -> bool:
        current = self._current
        if (
            not gate.enabled or gate.busy or gate.continuation or gate.silence or gate.cooldown
            or self._away or self._perception_hold or current is None or current.own_process
            or _privacy_reason(current, blocked_processes, blocked_titles)
        ):
            return False
        now = float(self.clock())
        return bool(self._offered) or self._last_content_check_at is None or (
            now - self._last_content_check_at >= self.content_check_interval
        )

    def _clear_content(self) -> None:
        self._content_baseline = ""
        self._content_pending = False
        self._last_content_check_at = None
        for name in ("_current", "_previous", "_pending", "_deferred"):
            snapshot = getattr(self, name)
            if snapshot is not None:
                setattr(self, name, replace(snapshot, visible_text=None))

    def skip_repeated_summary(self, summary: str, trigger: str) -> bool:
        current = self._current
        if current is None or not current.title.strip() or not summary.strip():
            return False
        value = (self._scope, current.app_key, current.title.strip(), summary.strip())
        repeated = value == self._last_visual and trigger not in {"window", "content"}
        self._last_visual = value
        return repeated

    def _read_content(self, text: str, now: float) -> None:
        if self._last_content_check_at is not None and now - self._last_content_check_at < self.content_check_interval:
            return
        self._last_content_check_at = now
        text = text.strip()[:2000]
        if len(text) < self.content_min_chars:
            return
        if not self._content_baseline:
            self._content_baseline = text
        self._content_pending = text != self._content_baseline

    def note_personal_evaluation(
        self,
        *,
        outcome: str,
        stage: str,
        elapsed_ms: int,
        trigger: str,
        process: str,
    ) -> None:
        record = {
            "kind": "personal_evaluation",
            "process": process,
            "trigger": trigger.split(":", 1)[0],
            "reason": outcome,
            "stage": stage,
            "elapsed_ms": str(int(elapsed_ms)),
        }
        self._diagnostics.append(record)
        _append_diagnostic(self.diagnostics_path, record)

    def _accept_without_success(self, now: float) -> None:
        offered = list(self._offered)
        if not offered and self._ready_trigger:
            offered = [self._ready_trigger]
        self._held_triggers = offered
        self._offered = []
        if any(trigger.startswith("window") for trigger in offered):
            self._ready_trigger = ""
        if any(trigger == "idle" or trigger.startswith("idle") for trigger in offered):
            self._idle_armed = False
        self._last_eval_at = now
        if not self._perception_settled:
            self._perception_hold = True
        self._emit(
            "evaluation",
            process=self._current.label if self._current else "",
            trigger=offered[0].split(":", 1)[0] if offered else "",
            reason="accepted",
        )

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
        if snap.app_key == current.app_key and snap.pid == current.pid:
            if snap.title != current.title or snap.process != current.process or snap.pid != current.pid:
                self._emit("focus_title", process=self._current.label, trigger="", reason="same_app")
            self._current = replace(
                snap, changed_at=current.changed_at,
                visible_text=snap.visible_text if snap.visible_text is not None else current.visible_text,
            )
            self._promote_deferred(now)
            self._finalize(now)
            return
        self._clear_content()
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
        if not self.window_switch_enabled:
            return
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
            if self._away_set_at and now - self._away_set_at >= self.away_max_seconds:
                self._away = False
                self._away_set_at = 0.0
            else:
                return []
        if silence:
            return []
        if self._perception_hold:
            return []
        if idle_seconds < self.idle_threshold_seconds:
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
            if self._content_pending and now >= self._content_quiet_until:
                triggers.append("content")
            if self._next_timer_at > 0:
                due = now >= self._next_timer_at
            else:
                last_timer = now if self._last_timer_check is None else self._last_timer_check
                due = now >= last_timer + self.timer_seconds
            if due:
                triggers.append("timer")
        if (
            not triggers
            and not cooldown
            and self._settled_at == 0
            and idle_seconds >= self.idle_threshold_seconds
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
        visible_text=value.get("visibleText") if isinstance(value.get("visibleText"), str) else None,
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
