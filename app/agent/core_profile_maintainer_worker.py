"""后台常驻档案维护 worker：独立于普通记忆整理线程。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from app.agent.core_profile_candidates import CoreCandidate, CoreCandidateQueue, eligible_since
from app.agent.core_profile_maintainer import (
    CoreMaintainerSettings,
    CoreMaintainerStateStore,
    CoreProfileMaintainer,
    MaintainerTrigger,
)
from app.agent.time_awareness import parse_iso_datetime
from app.core.cancellation import CancellationToken, OperationCancelled


class CoreMaintainerCompletionAdapter:
    """只暴露 complete_raw，避免把历史/心情/卡片/亲密指南传入 maintainer。"""

    def __init__(self, api_client: Any) -> None:
        self._client = api_client

    def complete_raw(self, system_prompt: str, messages: list[dict[str, str]], **kwargs: Any) -> str:
        complete = getattr(self._client, "complete_raw", None)
        if not callable(complete):
            raise RuntimeError("maintainer completion adapter has no complete_raw")
        return complete(system_prompt, messages, **kwargs)


class CoreProfileMaintainerWorker(QObject):
    """在独立后台线程执行一次 maintainer.run_once。"""

    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        maintainer: CoreProfileMaintainer,
        scope_id: str,
        trigger: MaintainerTrigger | None = None,
    ) -> None:
        super().__init__()
        self.maintainer = maintainer
        self.scope_id = scope_id
        self.trigger = trigger
        self._cancel_token = CancellationToken()

    @Slot()
    def cancel(self) -> None:
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        try:
            self._cancel_token.throw_if_cancelled()
            result = self.maintainer.run_once(self.scope_id, self.trigger)
            self._cancel_token.throw_if_cancelled()
        except OperationCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:  # 维护失败不得把已成功的普通整理改写成失败。
            if self._cancel_token.is_cancelled():
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


def maintainer_admission_indicates_work(
    *,
    queue: CoreCandidateQueue,
    state_store: CoreMaintainerStateStore,
    settings: CoreMaintainerSettings,
    scope_id: str,
    trigger: MaintainerTrigger | None,
    busy: bool,
    now: datetime | None = None,
) -> bool:
    """在不消耗 P3.3 租约/trigger 的前提下，判断是否值得启动维护线程。"""

    cfg = settings.normalized()
    if busy or not cfg.enabled:
        return False
    current = now or datetime.now().astimezone()
    if state_store.is_paused(scope_id, current):
        return False
    eligible = queue.eligible_for(scope_id)
    if not eligible:
        return False
    last_invoked = state_store.last_invoked_at(scope_id)
    in_cooldown = last_invoked is not None and (current - last_invoked) < timedelta(
        hours=cfg.normal_cooldown_hours
    )
    explicit_bypass = _explicit_bypass_available(state_store, scope_id, trigger)
    has_pending = trigger is not None
    three = len(eligible) >= 3
    stale = any(_candidate_is_stale(item, now=current, settings=cfg, queue=queue) for item in eligible)
    if not has_pending and not three and not stale:
        return False
    if in_cooldown and not explicit_bypass:
        return False
    return True


def persist_core_candidates(
    queue: CoreCandidateQueue,
    scope_id: str,
    payloads: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    on_error: Callable[[str], None] | None = None,
) -> MaintainerTrigger | None:
    """整理成功后写入队列；单条失败只记元数据，不影响其它候选。"""

    ingested: list[CoreCandidate] = []
    explicit_batch = ""
    for payload in payloads:
        try:
            candidate = queue.ingest(scope_id, payload)
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(str(exc))
            continue
        ingested.append(candidate)
        batch_id = str(payload.get("batch_id") or "").strip()
        if candidate.kind == "explicit" and batch_id:
            explicit_batch = batch_id
    eligible_ids = {item.id for item in queue.eligible_for(scope_id)}
    if explicit_batch:
        return MaintainerTrigger(kind="explicit", batch_id=explicit_batch)
    for candidate in ingested:
        if candidate.id in eligible_ids:
            return MaintainerTrigger(kind="observed", candidate_id=candidate.id)
    return None


def _explicit_bypass_available(
    state_store: CoreMaintainerStateStore,
    scope_id: str,
    trigger: MaintainerTrigger | None,
) -> bool:
    if trigger is None or trigger.kind != "explicit":
        return False
    batch_id = str(trigger.batch_id or "").strip()
    if not batch_id:
        return False
    return not state_store.explicit_batch_used(scope_id, batch_id)


def _candidate_is_stale(
    candidate: CoreCandidate,
    *,
    now: datetime,
    settings: CoreMaintainerSettings,
    queue: CoreCandidateQueue,
) -> bool:
    stamp = eligible_since(candidate, config=queue.config)
    parsed = parse_iso_datetime(stamp or "")
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return current - parsed >= timedelta(hours=settings.stale_eligible_hours)
