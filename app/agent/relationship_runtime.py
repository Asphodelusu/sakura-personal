"""Per-turn relational drive adapter owned by AgentRuntime.

Appraisal stays in the domain module for source compatibility. This adapter
does not infer appraisal from the main reply.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.relational_drive import (
    DriveAppraisal,
    DriveEffect,
    RelationalDriveProfile,
    build_drive_summary,
)
from app.core.runtime_log import log_event
from app.llm.prompts.types import ContextFragment
from app.storage.relational_drive import RelationalDriveStore

_FRAGMENT_ID = "runtime.relational_drive"
_TOKEN_BUDGET = 140


class RelationshipTurnAdapter:
    def __init__(self) -> None:
        self._store: RelationalDriveStore | None = None
        self._in_turn_enabled = True
        self._character_id = ""
        self._turn_id = ""
        self._snapshot_text: str | None = None
        self._snapshot_ready = False
        self._closed = False

    def configure(
        self,
        *,
        enabled: bool,
        in_turn_enabled: bool,
        profile: RelationalDriveProfile | None,
        state_path: Path | None,
        character_id: str,
    ) -> None:
        if self._closed:
            return
        self.invalidate()
        self._in_turn_enabled = bool(in_turn_enabled)
        self._character_id = str(character_id or "").strip()
        if not enabled or profile is None or state_path is None:
            return
        self._store = RelationalDriveStore(Path(state_path), profile)

    def close(self) -> None:
        self._closed = True
        self.invalidate()

    def invalidate(self) -> None:
        self._store = None
        self._character_id = ""
        self._turn_id = ""
        self._snapshot_text = None
        self._snapshot_ready = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def bound_character_id(self) -> str:
        return self._character_id

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def accepts_effect_instruction(self) -> bool:
        return self._store is not None and not self._closed

    def begin_user_turn(self, interaction_id: str) -> None:
        if self._closed:
            return
        self._turn_id = str(interaction_id or "").strip()
        self._snapshot_text = None
        self._snapshot_ready = False
        store = self._store
        if store is None or not self._turn_id:
            return
        now = datetime.now().astimezone()
        try:
            pre_contact = store.snapshot(now)
            self._snapshot_text = build_drive_summary(
                pre_contact,
                profile=store.profile,
                now=now,
            )
            self._snapshot_ready = True
        except Exception as exc:  # noqa: BLE001 - snapshot failure must not block the turn
            _log_failure("RELATIONSHIP_DRIVE_SNAPSHOT_FAILED", exc)
        try:
            store.note_contact(self._turn_id, now)
        except Exception as exc:  # noqa: BLE001 - contact failure must not block the turn
            _log_failure("RELATIONSHIP_DRIVE_CONTACT_FAILED", exc)

    def summary(self, *, fresh: bool = False) -> str:
        if not fresh and self._snapshot_ready:
            return str(self._snapshot_text or "")
        store = self._store
        if store is None or self._closed:
            if not fresh:
                self._snapshot_text = ""
                self._snapshot_ready = True
            return ""
        try:
            now = datetime.now().astimezone()
            text = build_drive_summary(
                store.snapshot(now),
                profile=store.profile,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            _log_failure("RELATIONSHIP_DRIVE_SNAPSHOT_FAILED", exc)
            text = ""
        if not fresh:
            self._snapshot_text = text
            self._snapshot_ready = True
        return text

    def fragment(self) -> ContextFragment | None:
        if self._closed or self._store is None or not self._in_turn_enabled:
            return None
        text = str(self.summary() or "").strip()
        if not text:
            return None
        return ContextFragment(
            fragment_id=_FRAGMENT_ID,
            source="runtime",
            content=f"[短期内在状态]\n{text}",
            trust="trusted",
            priority=87,
            token_budget=_TOKEN_BUDGET,
            sensitivity="private",
            cache_scope="turn",
            required=False,
        )

    def accept_appraisal(self, interaction_id: str, appraisal: object) -> bool:
        if self._closed or self._store is None:
            return False
        ident = str(interaction_id or "").strip()
        if not ident or ident != self._turn_id or not isinstance(appraisal, DriveAppraisal):
            return False
        try:
            return self._store.settle_appraisal(ident, appraisal, datetime.now().astimezone())
        except Exception as exc:  # noqa: BLE001
            _log_failure("RELATIONSHIP_DRIVE_APPRAISAL_FAILED", exc)
            return False

    def settle(
        self,
        interaction_id: str,
        reply: Any,
        *,
        character_id: str,
    ) -> bool:
        if self._closed:
            _log_ignored("closed")
            return False
        ident = str(interaction_id or "").strip()
        if self._store is None:
            _log_ignored("no_store")
            return False
        if str(character_id or "").strip() != self._character_id:
            _log_ignored("role_mismatch")
            return False
        if not ident:
            _log_ignored("empty_id")
            return False
        if ident != self._turn_id:
            _log_ignored("stale_interaction")
            return False
        if not str(getattr(reply, "text", "") or "").strip():
            _log_ignored("empty_reply")
            return False
        effect = getattr(reply, "drive_effect", None)
        if not isinstance(effect, DriveEffect):
            _log_ignored("no_effect")
            return False
        try:
            settled = self._store.settle_effect(ident, effect, datetime.now().astimezone())
        except Exception as exc:  # noqa: BLE001
            _log_failure("RELATIONSHIP_DRIVE_SETTLEMENT_FAILED", exc)
            return False
        if settled:
            self._snapshot_text = None
            self._snapshot_ready = False
        _log_ignored("settled" if settled else "duplicate")
        return settled


def _log_ignored(reason: str) -> None:
    log_event(
        "RelationalDrive",
        "effect ignored",
        {"code": "RELATIONSHIP_DRIVE_IGNORED", "reason": reason},
        severity="info",
    )


def _log_failure(code: str, exc: BaseException) -> None:
    log_event(
        "RelationalDrive",
        "effect settlement failed",
        {"code": code, "error_type": type(exc).__name__},
        severity="info",
    )
