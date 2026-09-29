"""User-turn drive binding, cached summary, fragment, and effect preservation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent.actions import AgentEvent
from app.agent.runtime import AgentRuntime
from app.core.relational_drive import DriveEffect, RelationalDriveProfile, build_drive_summary
from app.llm.chat_reply import ChatReply, ChatSegment, parse_chat_reply_result, sanitize_reply_tones
from app.llm.prompts.recipes import build_agent_reply_protocol
from app.llm.prompts.runtime import estimate_prompt_tokens
from app.llm.prompts.types import ContextMessage, ContextRequest
from app.storage.paths import StoragePaths


def test_missing_profile_or_global_switch_creates_no_store_or_file(tmp_path: Path) -> None:
    runtime = _runtime()
    path = _path(tmp_path, "demo")
    runtime.configure_relationship_drive(
        enabled=False,
        in_turn_enabled=True,
        profile=RelationalDriveProfile.natural_default(),
        state_path=path,
        character_id="demo",
    )
    assert runtime._relationship._store is None
    assert not path.exists()
    runtime.configure_relationship_drive(
        enabled=True,
        in_turn_enabled=True,
        profile=None,
        state_path=path,
        character_id="demo",
    )
    assert runtime._relationship._store is None
    assert not path.exists()
    assert "drive_effect" not in runtime._build_tool_system_prompt()
    assert "drive_effect" not in build_agent_reply_protocol(["中性"])


def test_existing_state_is_not_reset_and_pre_contact_summary_is_cached(tmp_path: Path) -> None:
    path = _path(tmp_path, "demo")
    path.parent.mkdir(parents=True)
    updated = datetime(2026, 9, 1, tzinfo=timezone.utc)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "updated_at": updated.isoformat(),
                "last_meaningful_contact_at": (updated - timedelta(hours=72)).isoformat(),
                "last_affectionate_contact_at": updated.isoformat(),
                "physical_arousal": 0.42,
                "erotic_salience": 0.2,
                "attachment_longing": 0.8,
                "afterglow": 0.0,
                "inhibition": 0.0,
                "settled_keys": ["old:contact"],
            }
        ),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    runtime = _bound(tmp_path, character_id="demo")
    assert path.read_text(encoding="utf-8") == before
    store = runtime._relationship._store
    assert store is not None
    pre_contact = store.snapshot(datetime.now().astimezone())
    expected = build_drive_summary(pre_contact, profile=store.profile, now=datetime.now().astimezone())
    runtime.begin_relationship_user_turn("turn-1")
    cached = runtime._relationship.summary()
    assert cached == expected
    assert store.snapshot(datetime.now().astimezone()).attachment_longing == store.profile.longing_baseline
    assert runtime._relationship.summary() == cached
    assert runtime._relationship.fragment() is not None
    assert runtime._relationship.fragment().content == f"[短期内在状态]\n{cached}"


def test_in_turn_off_still_records_contact_without_fragment_or_numbers(tmp_path: Path) -> None:
    runtime = _bound(tmp_path, in_turn_enabled=False)
    runtime.begin_relationship_user_turn("turn-off")
    assert runtime._relationship.fragment() is None
    payload = json.loads(_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert any(key.endswith(":turn-off:contact") for key in payload["settled_keys"])
    runtime._relationship._in_turn_enabled = True
    runtime._relationship._snapshot_ready = False
    fragment = runtime._relationship.fragment()
    assert fragment is not None
    assert fragment.fragment_id == "runtime.relational_drive"
    assert fragment.sensitivity == "private"
    assert fragment.token_budget == 140
    assert estimate_prompt_tokens(fragment.content) <= 140
    assert not any(char.isdigit() for char in fragment.content)
    for banned in (
        "physical_arousal",
        "erotic_salience",
        "attachment_longing",
        "afterglow",
        "inhibition",
    ):
        assert banned not in fragment.content


def test_fragment_survives_history_early_returns(tmp_path: Path) -> None:
    runtime = _bound(tmp_path)
    runtime.begin_relationship_user_turn("turn-fragment")
    absent = runtime._session_state_fragments(ContextRequest(current_input="在吗"))
    assert [item.fragment_id for item in absent] == ["runtime.relational_drive"]
    runtime.history_store = SimpleNamespace(load=lambda: (_ for _ in ()).throw(OSError("disk")))
    long_request = ContextRequest(
        recent_messages=tuple(ContextMessage(role="user", content=f"m{index}") for index in range(4))
    )
    long_window = runtime._session_state_fragments(long_request)
    assert [item.fragment_id for item in long_window] == ["runtime.relational_drive"]
    assert "drive_effect" in runtime._build_tool_system_prompt()


def test_events_and_unbegun_user_handler_do_not_record_contact(tmp_path: Path) -> None:
    runtime = _bound(tmp_path)
    try:
        runtime.handle_event(AgentEvent(type="reminder_due", payload={}))
    except Exception:
        pass
    assert not _path(tmp_path, "demo").exists()


def test_effect_parse_survives_tone_repair_and_segment_rebuild(tmp_path: Path) -> None:
    parsed = parse_chat_reply_result(
        json.dumps(
            {
                "segments": [{"ja": "……好き。", "zh": "……喜欢。", "tone": "en"}],
                "drive_effect": {"event": "mutual_affection", "strength": "mild"},
            },
            ensure_ascii=False,
        )
    )
    assert parsed.ok is True
    assert parsed.needs_retry is False
    assert parsed.reply.drive_effect == DriveEffect(event="mutual_affection", strength="mild")
    invalid = parse_chat_reply_result(
        json.dumps(
            {
                "segments": [{"ja": "……好き。", "zh": "……喜欢。", "tone": "中性"}],
                "drive_effect": {"event": "mutual_affection", "strength": "mild", "extra": 1},
            },
            ensure_ascii=False,
        )
    )
    assert invalid.ok is True
    assert invalid.reply.drive_effect is None
    assert invalid.reply.segments[0].text == "……好き。"
    sanitized = sanitize_reply_tones(parsed.reply, ["中性"])
    assert sanitized.segments[0].tone == "中性"
    assert sanitized.drive_effect == parsed.reply.drive_effect
    rebuilt = ChatReply([replace(segment, control=None) for segment in sanitized.segments])
    assert rebuilt.drive_effect == parsed.reply.drive_effect
    safe = parse_chat_reply_result("{")
    assert safe.reply.drive_effect is None

    runtime = _runtime()
    runtime.api_client.complete_with_tools.return_value = SimpleNamespace(
        content=json.dumps(
            {"segments": [{"ja": "……好き。", "zh": "……喜欢。", "tone": "中性"}]},
            ensure_ascii=False,
        ),
        tool_calls=[],
    )
    runtime.api_client.resolve_dialogue_params.return_value = (0.2, {})
    repaired = runtime._parse_final_reply_with_retry(
        "system",
        [{"role": "user", "content": "在吗"}],
        json.dumps(
            {
                "segments": [{"ja": "……好き。", "tone": "中性"}],
                "drive_effect": {"event": "aftercare", "strength": "strong"},
            },
            ensure_ascii=False,
        ),
    )
    assert repaired.segments[0].translation == "……喜欢。"
    assert repaired.drive_effect == DriveEffect(event="aftercare", strength="strong")


def test_close_and_role_replacement_reject_effect_and_characters_stay_independent(
    tmp_path: Path,
) -> None:
    first = _bound(tmp_path, character_id="alpha")
    second = _bound(tmp_path, character_id="beta")
    effect = DriveEffect(event="mutual_escalation", strength="mild")
    reply = ChatReply([ChatSegment("こっち。", "中性", "过来。")], drive_effect=effect)
    first.begin_relationship_user_turn("alpha-turn")
    assert first.settle_relationship_reply("alpha-turn", reply, character_id="alpha") is True
    assert first.settle_relationship_reply("alpha-turn", reply, character_id="alpha") is False
    assert not _path(tmp_path, "beta").exists()
    second.begin_relationship_user_turn("beta-turn")
    second.update_character("other", character_id="other")
    assert second.settle_relationship_reply("beta-turn", reply, character_id="beta") is False
    beta = json.loads(_path(tmp_path, "beta").read_text(encoding="utf-8"))
    assert not any(key.endswith(":effect") for key in beta["settled_keys"])
    closed = _bound(tmp_path, character_id="gamma")
    closed.begin_relationship_user_turn("gamma-turn")
    closed.close()
    assert closed.settle_relationship_reply("gamma-turn", reply, character_id="gamma") is False
    gamma = json.loads(_path(tmp_path, "gamma").read_text(encoding="utf-8"))
    assert not any(key.endswith(":effect") for key in gamma["settled_keys"])


def _runtime() -> AgentRuntime:
    client = MagicMock()
    client.complete_with_tools.return_value = SimpleNamespace(content="", tool_calls=[])
    client.resolve_dialogue_params.return_value = (0.8, {})
    return AgentRuntime(client, "system", character_id="demo", character_name="Demo")


def _bound(tmp_path: Path, *, character_id: str = "demo", in_turn_enabled: bool = True) -> AgentRuntime:
    runtime = _runtime()
    runtime.configure_relationship_drive(
        enabled=True,
        in_turn_enabled=in_turn_enabled,
        profile=RelationalDriveProfile.natural_default(),
        state_path=_path(tmp_path, character_id),
        character_id=character_id,
    )
    return runtime


def _path(tmp_path: Path, character_id: str) -> Path:
    return StoragePaths(tmp_path).relational_drive_for(character_id)
