"""Relational drive in-turn context: bind, contact, fragment, no extra API calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agent.inner_thought import InnerThoughtResult, InnerThoughtSettings
from app.agent.runtime import AgentRuntime
from app.agent.turn_routing import TurnPlan, TurnState
from app.config.relationship_drive import RelationshipDriveSettings
from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.core.interaction import set_interaction_id
from app.core.relational_drive import (
    DriveAppraisal,
    DriveDirection,
    DriveKind,
    DriveStrength,
    RelationalDriveProfile,
    build_drive_summary,
)
from app.llm.api_client import ChatCompletionTurn, OpenAICompatibleClient
from app.llm.chat_reply import ChatReply, ChatSegment
from app.llm.prompts.runtime import estimate_prompt_tokens
from app.llm.prompts.types import ContextRequest


def _now() -> datetime:
    return datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _drive_profile() -> RelationalDriveProfile:
    return RelationalDriveProfile.natural_default()


def _appraisal() -> DriveAppraisal:
    return DriveAppraisal(
        kind=DriveKind.EROTIC_SALIENCE,
        direction=DriveDirection.RISE,
        strength=DriveStrength.SUBTLE,
    )


def _dummy_client() -> MagicMock:
    client = MagicMock(spec=OpenAICompatibleClient)
    reply = ChatReply(
        segments=[ChatSegment(ja="おはよう", zh="早安", tone="开心", portrait="站立待机")]
    )
    client.complete_with_tools.return_value = MagicMock(
        spec=ChatCompletionTurn,
        content=json.dumps(
            {"segments": [{"ja": "おはよう", "zh": "早安", "tone": "开心", "portrait": "站立待机"}]},
            ensure_ascii=False,
        ),
        tool_calls=[],
    )
    client.chat.return_value = reply
    client.complete_raw.return_value = client.complete_with_tools.return_value.content
    client.resolve_dialogue_params.return_value = (0.8, {})
    return client


def _runtime(tmp_path: Path, *, enabled: bool = True, in_turn: bool = True) -> AgentRuntime:
    client = _dummy_client()
    runtime = AgentRuntime(
        client,
        "system",
        inner_thought_api_client=client,
        inner_thought_settings=InnerThoughtSettings(enabled=False),
    )
    runtime.set_relationship_initiative(RelationshipInitiativeSettings(in_turn_enabled=in_turn))
    profile = _drive_profile() if enabled else None
    settings = RelationshipDriveSettings(enabled=enabled)
    runtime.set_relationship_drive(settings, profile, tmp_path / "alice-relational-drive.json")
    return runtime


def _standard_turn() -> TurnState:
    return TurnState(
        turn_plan=TurnPlan(
            tier="standard",
            modality="text",
            client_key="chat",
            decided_by="test",
        ),
        recall_decision="light",
    )


def _drive_fragments(runtime: AgentRuntime) -> list:
    request = ContextRequest(current_input="在吗", recent_messages=())
    return [
        fragment
        for fragment in runtime._session_state_fragments(request)
        if fragment.fragment_id == "runtime.relational_drive"
    ]


def test_set_relationship_drive_binds_and_clears(tmp_path: Path) -> None:
    runtime = AgentRuntime(_dummy_client(), "system")
    path = tmp_path / "alice-relational-drive.json"
    runtime.set_relationship_drive(RelationshipDriveSettings(enabled=True), _drive_profile(), path)
    assert runtime._relationship_drive_store is not None
    assert runtime._relationship_drive_store.path == path
    first = runtime._relationship_drive_store
    runtime.update_character("other", character_profile=None)
    assert runtime._relationship_drive_store is None
    runtime.set_relationship_drive(RelationshipDriveSettings(enabled=True), _drive_profile(), path)
    assert runtime._relationship_drive_store is not first
    runtime.set_relationship_drive(RelationshipDriveSettings(enabled=False), _drive_profile(), path)
    assert runtime._relationship_drive_store is None
    runtime.set_relationship_drive(RelationshipDriveSettings(enabled=True), None, path)
    assert runtime._relationship_drive_store is None
    source = Path("app/agent/runtime.py").read_text(encoding="utf-8")
    assert "Sakura" not in source


def test_real_interaction_id_records_contact_once(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    store = runtime._relationship_drive_store
    assert store is not None
    calls: list[str] = []
    original = store.note_contact

    def _spy(interaction_id: str, now: datetime):
        calls.append(interaction_id)
        return original(interaction_id, now)

    store.note_contact = _spy  # type: ignore[method-assign]
    set_interaction_id("interaction-7")
    runtime.handle_user_message([{"role": "user", "content": "在吗"}])
    _drive_fragments(runtime)
    _drive_fragments(runtime)
    assert calls == ["interaction-7"]
    snapshot = store.snapshot(_now())
    assert snapshot.last_meaningful_contact_at is not None


def test_joined_appraisal_settles_once_with_captured_id(tmp_path: Path) -> None:
    client = _dummy_client()
    runtime = AgentRuntime(
        client,
        "system",
        inner_thought_api_client=client,
        inner_thought_settings=InnerThoughtSettings(enabled=True, skip_fast_tier=True),
    )
    runtime.set_relationship_initiative(RelationshipInitiativeSettings(in_turn_enabled=True))
    runtime.set_relationship_drive(
        RelationshipDriveSettings(enabled=True),
        _drive_profile(),
        tmp_path / "alice-relational-drive.json",
    )
    store = runtime._relationship_drive_store
    assert store is not None
    settled: list[tuple[str, DriveAppraisal]] = []
    original = store.settle_appraisal

    def _spy(interaction_id: str, appraisal: DriveAppraisal, now: datetime) -> bool:
        settled.append((interaction_id, appraisal))
        return original(interaction_id, appraisal, now)

    store.settle_appraisal = _spy  # type: ignore[method-assign]
    set_interaction_id("interaction-3")
    runtime._begin_relationship_drive_user_turn()
    appraisal = _appraisal()
    with patch(
        "app.agent.context_builder.generate_inner_thought",
        return_value=InnerThoughtResult(
            text="昨夜のことを、少し思い出した。",
            interest="high",
            drive_appraisal=appraisal,
        ),
    ):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "在吗"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        assert launch.interaction_id == "interaction-3"
        runtime._finalize_inner_thought_worker(launch)
        runtime._finalize_inner_thought_worker(launch)
    assert [(item[0], item[1]) for item in settled] == [("interaction-3", appraisal)]


def test_timeout_empty_id_and_store_error_fail_open(tmp_path: Path) -> None:
    client = _dummy_client()
    runtime = AgentRuntime(
        client,
        "system",
        inner_thought_api_client=client,
        inner_thought_settings=InnerThoughtSettings(
            enabled=True,
            skip_fast_tier=True,
            join_timeout_seconds=1,
        ),
    )
    runtime.set_relationship_drive(
        RelationshipDriveSettings(enabled=True),
        _drive_profile(),
        tmp_path / "alice-relational-drive.json",
    )
    store = runtime._relationship_drive_store
    assert store is not None
    store.settle_appraisal = MagicMock(side_effect=OSError("disk"))  # type: ignore[method-assign]
    set_interaction_id("")
    runtime._begin_relationship_drive_user_turn()
    with patch(
        "app.agent.context_builder.generate_inner_thought",
        return_value=InnerThoughtResult(
            text="まだ少し気になる。",
            interest="mid",
            drive_appraisal=_appraisal(),
        ),
    ):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "hi"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        runtime._finalize_inner_thought_worker(launch)
    store.settle_appraisal.assert_not_called()

    set_interaction_id("interaction-9")
    runtime._inner_thought_done_for_turn = False
    runtime._begin_relationship_drive_user_turn()

    def _slow(*_args: object, **_kwargs: object) -> InnerThoughtResult:
        import time

        time.sleep(2.5)
        return InnerThoughtResult(text="遅すぎ", interest="mid", drive_appraisal=_appraisal())

    with patch("app.agent.context_builder.generate_inner_thought", side_effect=_slow):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "在吗"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        runtime._finalize_inner_thought_worker(launch)
        try:
            launch.future.result(timeout=3)
        except Exception:
            pass
    store.settle_appraisal.assert_not_called()

    runtime._inner_thought_done_for_turn = False
    with patch(
        "app.agent.context_builder.generate_inner_thought",
        return_value=InnerThoughtResult(
            text="まだ少し気になる。",
            interest="mid",
            drive_appraisal=_appraisal(),
        ),
    ):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "hi"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        runtime._finalize_inner_thought_worker(launch)


def test_global_off_and_missing_profile_have_no_store_or_fragment(tmp_path: Path) -> None:
    off = _runtime(tmp_path, enabled=False)
    assert off._relationship_drive_store is None
    assert _drive_fragments(off) == []
    missing = AgentRuntime(_dummy_client(), "system")
    missing.set_relationship_initiative(RelationshipInitiativeSettings(in_turn_enabled=True))
    missing.set_relationship_drive(
        RelationshipDriveSettings(enabled=True),
        None,
        tmp_path / "alice-relational-drive.json",
    )
    assert missing._relationship_drive_store is None
    assert _drive_fragments(missing) == []


def test_a_off_keeps_store_but_omits_fragment(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, enabled=True, in_turn=False)
    assert runtime._relationship_drive_store is not None
    set_interaction_id("interaction-11")
    runtime._begin_relationship_drive_user_turn()
    assert runtime.relationship_drive_summary()
    assert _drive_fragments(runtime) == []
    snapshot = runtime._relationship_drive_store.snapshot(_now())
    assert snapshot.last_meaningful_contact_at is not None


def test_a_on_injects_one_private_qualitative_fragment(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, enabled=True, in_turn=True)
    set_interaction_id("interaction-12")
    runtime._begin_relationship_drive_user_turn()
    fragments = _drive_fragments(runtime)
    assert len(fragments) == 1
    fragment = fragments[0]
    assert fragment.source == "runtime"
    assert fragment.sensitivity == "private"
    assert fragment.cache_scope == "turn"
    assert fragment.token_budget <= 140
    assert estimate_prompt_tokens(fragment.content) <= 140
    text = fragment.content
    assert "短期内在" in text or build_drive_summary(
        runtime._relationship_drive_store.snapshot(_now())
    ) in text
    assert not any(char.isdigit() for char in text)
    for banned in (
        "physical_arousal",
        "erotic_salience",
        "attachment_longing",
        "afterglow",
        "inhibition",
        "RelationalDrive",
        "drive_effect",
        "mutual_affection",
        "mutual_escalation",
        "hormone",
        "0.",
        "=",
        "必须",
        "不要",
        "请",
    ):
        assert banned not in text


def test_repeated_tool_steps_reuse_one_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    store = runtime._relationship_drive_store
    assert store is not None
    snapshots = {"count": 0}
    original = store.snapshot

    def _spy(now: datetime):
        snapshots["count"] += 1
        return original(now)

    store.snapshot = _spy  # type: ignore[method-assign]
    set_interaction_id("interaction-13")
    runtime._begin_relationship_drive_user_turn()
    first = _drive_fragments(runtime)
    second = _drive_fragments(runtime)
    assert first == second
    assert snapshots["count"] <= 1
    set_interaction_id("interaction-14")
    runtime._begin_relationship_drive_user_turn()
    later = _drive_fragments(runtime)
    assert later
    assert snapshots["count"] <= 2


def test_enabling_drive_adds_zero_model_calls(tmp_path: Path) -> None:
    off_client = _dummy_client()
    on_client = _dummy_client()
    off = AgentRuntime(
        off_client,
        "system",
        inner_thought_api_client=off_client,
        inner_thought_settings=InnerThoughtSettings(enabled=False),
    )
    on = AgentRuntime(
        on_client,
        "system",
        inner_thought_api_client=on_client,
        inner_thought_settings=InnerThoughtSettings(enabled=False),
    )
    on.set_relationship_initiative(RelationshipInitiativeSettings(in_turn_enabled=True))
    on.set_relationship_drive(
        RelationshipDriveSettings(enabled=True),
        _drive_profile(),
        tmp_path / "alice-relational-drive.json",
    )
    messages = [{"role": "user", "content": "在吗"}]
    set_interaction_id("interaction-off")
    off.handle_user_message(messages)
    set_interaction_id("interaction-on")
    on.handle_user_message(messages)
    assert off_client.complete_raw.call_count == on_client.complete_raw.call_count
    assert off_client.chat.call_count == on_client.chat.call_count
    assert off_client.complete_with_tools.call_count == on_client.complete_with_tools.call_count


def test_pet_window_rebinds_drive_on_character_switch(tmp_path: Path) -> None:
    from app.storage.paths import StoragePaths
    from app.ui.pet_window import PetWindow

    calls: list[tuple[object, object, Path]] = []

    class RuntimeStub:
        def set_relationship_drive(self, settings, profile, state_path) -> None:
            calls.append((settings, profile, Path(state_path)))

    class SettingsStub:
        def load_relationship_drive_settings(self):
            return RelationshipDriveSettings(enabled=True)

    profile_a = SimpleNamespace(id="alice", relationship_drive_profile=_drive_profile())
    profile_b = SimpleNamespace(id="bob", relationship_drive_profile=_drive_profile())
    window = SimpleNamespace(
        agent_runtime=RuntimeStub(),
        settings_service=SettingsStub(),
        base_dir=tmp_path,
    )
    PetWindow._configure_relationship_drive(window, profile_a)
    PetWindow._configure_relationship_drive(window, profile_b)
    assert len(calls) == 2
    assert calls[0][1] is profile_a.relationship_drive_profile
    assert calls[1][1] is profile_b.relationship_drive_profile
    assert calls[0][2] == StoragePaths(tmp_path).relational_drive_for("alice")
    assert calls[1][2] == StoragePaths(tmp_path).relational_drive_for("bob")
    assert calls[0][2] != calls[1][2]
    assert tmp_path in calls[0][2].parents
    assert "Sakura" not in str(calls[0][2])
