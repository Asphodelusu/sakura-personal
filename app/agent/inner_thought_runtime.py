"""One outstanding private inner-thought request owned by AgentRuntime.

Close and replacement invalidate immediately. A worker that is still inside the
provider call is not joined while a state lock is held, and it cannot publish
after its epoch is retired.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

from app.agent.inner_thought import (
    InnerThoughtResult,
    InnerThoughtSettings,
    InnerThoughtWindow,
    character_excerpt_from_prompt,
    format_recent_dialogue,
    generate_inner_thought,
    should_generate_inner_thought,
)
from app.core.cancellation import OperationCancelled
from app.core.runtime_log import log_event
from app.llm.prompts.types import ContextFragment

AppraisalSink = Callable[[str, Any], bool]


class _Work:
    def __init__(self, epoch: int, turn_id: str, character_id: str) -> None:
        self.epoch = epoch
        self.turn_id = turn_id
        self.character_id = character_id
        self.cancel = Event()
        self.wake = Event()
        self.done = Event()
        self.abandoned = False
        self.accepted = False
        self.result = None


class InnerThoughtCoordinator:
    def __init__(self) -> None:
        self._lock = Lock()
        self._epoch = 0
        self._closed = False
        self._settings = InnerThoughtSettings()
        self._client: object | None = None
        self._source_slot = ""
        self._character_id = ""
        self._character_name = ""
        self._system_prompt = ""
        self._window = InnerThoughtWindow()
        self._active: _Work | None = None
        self._worker: Thread | None = None
        self._appraisal_sink: AppraisalSink | None = None
        self._incomplete_logged = False
        self._turn_interest: str | None = None
        self._mood_provider: Callable[[], str] | None = None

    def configure(
        self,
        *,
        settings: InnerThoughtSettings,
        client: object | None,
        source_slot: str,
        character_id: str,
        character_name: str,
        system_prompt: str,
        appraisal_sink: AppraisalSink | None,
        mood_provider: Callable[[], str] | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._settings = settings
            self._client = client
            self._mood_provider = mood_provider
            self._source_slot = str(source_slot or "")
            self._character_name = str(character_name or "")
            self._system_prompt = str(system_prompt or "")
            self._appraisal_sink = appraisal_sink
            self._window.configure(max(1, int(settings.window_size or 1)))
            incoming = str(character_id or "").strip()
            if incoming != self._character_id:
                self._retire(clear_window=True)
                self._character_id = incoming
            if not self._incomplete_logged:
                self._incomplete_logged = True
                log_event(
                    "InnerThought",
                    "内心独白上下文不完整",
                    {
                        "code": "INNER_THOUGHT_CONTEXT_INCOMPLETE",
                        "missing": ["sensory_impression"],
                    },
                    severity="info",
                )

    def note_character(
        self,
        *,
        character_id: str,
        character_name: str,
        system_prompt: str,
        replaced: bool,
    ) -> None:
        with self._lock:
            self._character_name = str(character_name or "")
            self._system_prompt = str(system_prompt or "")
            incoming = str(character_id or "").strip()
            if replaced or incoming != self._character_id:
                self._retire(clear_window=True)
                self._character_id = incoming

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._retire(clear_window=False)

    def invalidate(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._retire(clear_window=False)

    def start(
        self,
        turn_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_checker: Any = None,
        turn_tier: str = "standard",
        proactive_mode: bool = False,
    ) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._turn_interest = None
            if not should_generate_inner_thought(
                self._settings,
                api_client=self._client,
                turn_tier=turn_tier,
                proactive_mode=proactive_mode,
            ):
                return False
            if self._worker is not None and self._worker.is_alive():
                self._retire(clear_window=False)
                return False
            self._epoch += 1
            work = _Work(self._epoch, str(turn_id or "").strip(), self._character_id)
            self._active = work
            snapshot = {
                "character_name": self._character_name,
                "character_excerpt": character_excerpt_from_prompt(self._system_prompt),
                "recent_dialogue": format_recent_dialogue(messages),
                "previous": self._window.items(),
                "mood_provider": self._mood_provider,
            }
            client = self._client
            worker = Thread(
                target=self._run,
                args=(work, snapshot, client, cancel_checker),
                name="inner-thought",
                daemon=True,
            )
            self._worker = worker
        worker.start()
        return True

    def join(
        self,
        turn_id: str,
        *,
        character_id: str,
        cancel_checker: Any = None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            work = self._active
            ident = str(turn_id or "").strip()
            if (
                work is None
                or work.turn_id != ident
                or work.epoch != self._epoch
                or work.abandoned
                or work.accepted
            ):
                return
            timeout = float(self._settings.join_timeout_seconds)
        if cancel_checker is not None:
            try:
                cancel_checker()
            except OperationCancelled:
                self._abandon(work)
                raise
        deadline = monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - monotonic())
            signaled = work.wake.wait(min(remaining, 0.05) if cancel_checker else remaining)
            if cancel_checker is not None:
                try:
                    cancel_checker()
                except OperationCancelled:
                    self._abandon(work)
                    raise
            if signaled or monotonic() >= deadline:
                break
        with self._lock:
            if (
                self._closed
                or work.epoch != self._epoch
                or work.cancel.is_set()
                or work.abandoned
                or work.accepted
                or work.turn_id != ident
                or str(character_id or "").strip() != self._character_id
                or not signaled
                or not work.done.is_set()
                or work.result is None
            ):
                work.abandoned = True
                work.cancel.set()
                work.wake.set()
                return
            self._commit(work)

    def fragment(self) -> ContextFragment | None:
        with self._lock:
            from app.agent.inner_thought import build_inner_thought_fragment

            return build_inner_thought_fragment(self._window, character_name=self._character_name)

    def verbosity_fragment(self) -> ContextFragment | None:
        from app.agent.reply_verbosity import decision_from_interest, format_verbosity_guidance

        with self._lock:
            decision = decision_from_interest(self._turn_interest)
        if decision is None:
            return None
        return ContextFragment(
            fragment_id="runtime.reply_verbosity",
            source="runtime",
            content=format_verbosity_guidance(decision),
            trust="trusted",
            priority=87,
            token_budget=160,
            sensitivity="private",
            cache_scope="turn",
            required=False,
        )

    def wait_until_idle(self, timeout: float = 2.0) -> bool:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def _run(
        self,
        work: _Work,
        snapshot: dict[str, Any],
        client: object | None,
        cancel_checker: Any,
    ) -> None:
        try:
            if work.cancel.is_set() or client is None:
                return

            def _cancel() -> None:
                if work.cancel.is_set():
                    raise OperationCancelled()
                if cancel_checker is not None:
                    cancel_checker()

            mood_summary = ""
            mood_provider = snapshot.get("mood_provider")
            if callable(mood_provider):
                try:
                    mood_summary = str(mood_provider() or "")
                except Exception:  # noqa: BLE001 - an unreadable mood leaves the default line
                    mood_summary = ""
            _cancel()
            work.result = generate_inner_thought(
                client,
                character_name=str(snapshot["character_name"]),
                character_excerpt=str(snapshot["character_excerpt"]),
                mood_summary=mood_summary,
                recent_dialogue=str(snapshot["recent_dialogue"]),
                sensory_impression="",
                previous_thoughts=tuple(snapshot["previous"]),
                cancel_checker=_cancel,
            )
        except Exception as exc:  # noqa: BLE001 - worker must exit without publishing text
            log_event(
                "InnerThought",
                "内心独白任务已结束",
                {"code": "INNER_THOUGHT_WORKER_FAILED", "error_type": type(exc).__name__},
                severity="info",
            )
            work.result = InnerThoughtResult(text="")
        finally:
            work.done.set()
            work.wake.set()

    def _commit(self, work: _Work) -> None:
        work.accepted = True
        result = work.result
        if result is None:
            return
        if result.text:
            self._window.push(result.text)
        self._turn_interest = result.interest
        appraisal = result.drive_appraisal
        sink = self._appraisal_sink
        if appraisal is None or sink is None:
            return
        try:
            sink(work.turn_id, appraisal)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "InnerThought",
                "内心评价未写入",
                {"code": "INNER_THOUGHT_APPRAISAL_FAILED", "error_type": type(exc).__name__},
                severity="info",
            )

    def _abandon(self, work: _Work) -> None:
        with self._lock:
            work.abandoned = True
            work.cancel.set()
            work.wake.set()

    def _retire(self, *, clear_window: bool) -> None:
        self._epoch += 1
        self._turn_interest = None
        if clear_window:
            self._window.clear()
        work = self._active
        if work is None:
            return
        work.abandoned = True
        work.cancel.set()
        work.wake.set()
