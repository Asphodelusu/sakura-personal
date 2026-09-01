"""Adopted-reply drive settlement stays exact-once and fail-open."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agent.runtime import AgentRuntime
from app.config.relationship_drive import RelationshipDriveSettings
from app.core.interaction import clear_interaction_id, set_interaction_id
from app.core.relational_drive import DriveEffect, RelationalDriveProfile
from app.llm.chat_reply import ChatReply, ChatSegment


@pytest.fixture(autouse=True)
def _reset_interaction_id():  # type: ignore[no-untyped-def]
    clear_interaction_id()
    yield
    clear_interaction_id()


def _runtime(tmp_path: Path) -> AgentRuntime:
    runtime = AgentRuntime(MagicMock(), "system")
    runtime.set_relationship_drive(
        RelationshipDriveSettings(enabled=True),
        RelationalDriveProfile.natural_default(),
        tmp_path / "relational-drive.json",
    )
    return runtime


def _reply(effect: DriveEffect | None = None) -> ChatReply:
    return ChatReply(
        [ChatSegment(ja="こっち。", zh="过来。", tone="温柔")],
        drive_effect=effect,
    )


def test_adopted_reply_effect_settles_once_for_current_user_interaction(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    set_interaction_id("interaction-1")
    runtime._begin_relationship_drive_user_turn()
    reply = _reply(DriveEffect(event="mutual_affection", strength="mild"))

    assert runtime.settle_adopted_reply_drive("interaction-1", reply) is True
    assert runtime.settle_adopted_reply_drive("interaction-1", reply) is False

    store = runtime._relationship_drive_store
    assert store is not None
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["settled_keys"].count("interaction-1:effect") == 1
    assert payload["last_affectionate_contact_at"] is not None


def test_adopted_reply_without_effect_is_ignored(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    set_interaction_id("interaction-2")
    runtime._begin_relationship_drive_user_turn()

    assert runtime.settle_adopted_reply_drive("interaction-2", _reply()) is False

    store = runtime._relationship_drive_store
    assert store is not None
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert "interaction-2:effect" not in payload["settled_keys"]


@pytest.mark.parametrize("adopted_id", ["", "   ", "other-interaction"])
def test_adopted_reply_rejects_empty_or_noncurrent_interaction_id(
    tmp_path: Path,
    adopted_id: str,
) -> None:
    runtime = _runtime(tmp_path)
    set_interaction_id("interaction-3")
    runtime._begin_relationship_drive_user_turn()
    store = runtime._relationship_drive_store
    assert store is not None
    before = store.path.read_text(encoding="utf-8")

    settled = runtime.settle_adopted_reply_drive(
        adopted_id,
        _reply(DriveEffect(event="mutual_escalation", strength="mild")),
    )

    assert settled is False
    assert store.path.read_text(encoding="utf-8") == before


def test_adopted_reply_store_error_fails_open(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    set_interaction_id("interaction-4")
    runtime._begin_relationship_drive_user_turn()
    store = runtime._relationship_drive_store
    assert store is not None
    store.settle_effect = MagicMock(side_effect=OSError("disk unavailable"))  # type: ignore[method-assign]

    settled = runtime.settle_adopted_reply_drive(
        "interaction-4",
        _reply(DriveEffect(event="fulfilled", strength="mild")),
    )

    assert settled is False


def test_fresh_summary_reads_current_state_without_replacing_turn_cache_or_calling_model(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    store = runtime._relationship_drive_store
    assert store is not None
    old_contact = datetime.now().astimezone() - timedelta(days=3)
    store.settle_effect(
        "past-contact",
        DriveEffect(event="mutual_affection", strength="mild"),
        old_contact,
    )
    set_interaction_id("interaction-5")
    runtime._begin_relationship_drive_user_turn()
    calls = 0
    original_snapshot = store.snapshot

    def _snapshot(now: datetime):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original_snapshot(now)

    store.snapshot = _snapshot  # type: ignore[method-assign]

    cached = runtime.relationship_drive_summary()
    assert runtime.relationship_drive_summary() == cached
    fresh = runtime.relationship_drive_summary(fresh=True)
    assert fresh
    assert calls == 2
    assert runtime.api_client.complete_raw.call_count == 0
    assert runtime.api_client.chat.call_count == 0
    assert runtime.api_client.complete_with_tools.call_count == 0

