"""A V1 archive is kept verbatim beside the maintainer's new sections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_personal_core_maintenance import (
    SECTION,
    UPDATED,
    Completion,
    _curate,
    _candidate,
    _entries,
    _maintenance,
    _memory,
    _proposal,
)
from plugins.builtin.sakura_mem0 import personal_core_profile as profiles
from plugins.builtin.sakura_mem0.personal_core_profile import (
    CoreProfileStorageError,
    edit_personal_core_profile_section,
    read_personal_core_profile,
    upgrade_personal_core_profile,
)
from plugins.builtin.sakura_mem0.personal_profile_maintenance import commit_curation_then_maintain

V1_TEXT = "我们是对等的恋人。他叫铭，我叫他铭君。"


def _v1(memory: Path) -> tuple[Path, bytes]:
    path = memory / "core_profiles.json"
    path.write_text(json.dumps({
        "alice": {
            "id": "core_profile:alice",
            "content": V1_TEXT,
            "memory": V1_TEXT,
            "metadata": {"scope": "alice", "updated_at": UPDATED, "created_at": UPDATED, "source": "self_curation"},
        },
        "bob": {"content": "bob stays", "metadata": {"scope": "bob"}},
    }, ensure_ascii=False), encoding="utf-8")
    return path, path.read_bytes()


def _saved(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_v1_archive_is_upgraded_verbatim_with_a_one_time_backup(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    path, original = _v1(memory)

    assert upgrade_personal_core_profile(memory, "alice") is True
    record = _saved(path)["alice"]
    assert record["schema_version"] == 2
    assert record["sections"] == {"legacy": V1_TEXT}
    assert record["content"] == f"＜これまでの記録＞\n{V1_TEXT}"
    assert record["metadata"]["source"] == "self_curation"
    assert _saved(path)["bob"]["content"] == "bob stays"
    backup = memory / "core_profiles.v1-backup.json"
    assert backup.read_bytes() == original

    assert upgrade_personal_core_profile(memory, "alice") is False
    edit_personal_core_profile_section(memory, "alice", SECTION, "新的认识。")
    assert backup.read_bytes() == original


def test_new_sections_render_before_the_archive_and_leave_it_untouched(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    path, _original = _v1(memory)
    upgrade_personal_core_profile(memory, "alice")
    base = _saved(path)["alice"]["metadata"]["updated_at"]

    profiles.patch_personal_core_profile_sections(memory, "alice", base, {SECTION: "我们明确确认了恋人关系。"})

    record = _saved(path)["alice"]
    assert record["sections"]["legacy"] == V1_TEXT
    assert record["content"].index(f"＜{SECTION}＞") < record["content"].index("＜これまでの記録＞")
    assert "我们明确确认了恋人关系。" in read_personal_core_profile(memory, "alice")["content"]


def test_manual_edits_reach_the_archive_and_can_clear_everything(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    path, _original = _v1(memory)

    edit_personal_core_profile_section(memory, "alice", "legacy", "改过的旧档案。")
    record = _saved(path)["alice"]
    assert record["sections"] == {"legacy": "改过的旧档案。"}
    assert record["metadata"]["source"] == "manual"

    edit_personal_core_profile_section(memory, "alice", SECTION, "关系。")
    edit_personal_core_profile_section(memory, "alice", "legacy", "")
    assert _saved(path)["alice"]["sections"] == {SECTION: "关系。"}
    edit_personal_core_profile_section(memory, "alice", SECTION, " ")
    assert "alice" not in _saved(path)
    with pytest.raises(CoreProfileStorageError):
        edit_personal_core_profile_section(memory, "alice", "unknown", "x")


def _run_maintenance(memory: Path, response):
    result, _store, _client = _curate([_candidate()])
    completion = Completion(response)
    commit_curation_then_maintain(
        _maintenance(memory), _entries(), result.core_candidates,
        mark_success=lambda: None, completion=completion,
    )
    return completion


def _queued(memory: Path) -> tuple[str, str]:
    item = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))["scopes"]["alice"]["candidates"][0]
    return item["id"], item["evidence"][0]["id"]


def test_maintainer_writes_beside_a_v1_archive(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    path, original = _v1(memory)

    def respond(system_prompt, messages, **_kwargs):
        prompt = messages[0]["content"]
        assert "これまでの記録（読み取り専用）" in prompt
        assert V1_TEXT in prompt
        assert "migrate_legacy" not in prompt
        base = _saved(path)["alice"]["metadata"]["updated_at"]
        return _proposal(*_queued(memory), base=base)

    _run_maintenance(memory, respond)

    record = _saved(path)["alice"]
    assert record["sections"]["legacy"] == V1_TEXT
    assert record["sections"][SECTION] == "我们明确确认了恋人关系。"
    assert (memory / "core_profiles.v1-backup.json").read_bytes() == original


def test_maintainer_may_not_rewrite_the_archive(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    path, _original = _v1(memory)

    def respond(system_prompt, messages, **_kwargs):
        candidate_id, evidence_id = _queued(memory)
        base = _saved(path)["alice"]["metadata"]["updated_at"]
        return json.dumps({"base_updated_at": base, "operations": [{
            "op": "migrate_legacy",
            "sections": {SECTION: V1_TEXT},
            "reason": "迁移",
            "candidate_ids": [candidate_id],
            "evidence_ids": [evidence_id],
        }]}, ensure_ascii=False)

    _run_maintenance(memory, respond)

    assert _saved(path)["alice"]["sections"] == {"legacy": V1_TEXT}
    state = json.loads((memory / "core_maintainer_state.json").read_text(encoding="utf-8"))
    assert state["scopes"]["alice"]["validation_failure_streak"] == 1


def test_a_refused_write_counts_toward_the_pause(tmp_path: Path, monkeypatch) -> None:
    memory = _memory(tmp_path)
    path, _original = _v1(memory)

    def refuse(*_args, **_kwargs):
        raise CoreProfileStorageError("refused")

    monkeypatch.setattr(
        "plugins.builtin.sakura_mem0.personal_profile_maintenance.patch_personal_core_profile_sections", refuse
    )

    def respond(system_prompt, messages, **_kwargs):
        return _proposal(*_queued(memory), base=_saved(path)["alice"]["metadata"]["updated_at"])

    _run_maintenance(memory, respond)

    state = json.loads((memory / "core_maintainer_state.json").read_text(encoding="utf-8"))
    assert state["scopes"]["alice"]["validation_failure_streak"] == 1


def _management(tmp_path: Path, *, daily: bool):
    import threading

    from plugins.builtin.sakura_mem0.personal_runtime import PersonalRecallBoundary
    from plugins.builtin.sakura_mem0.plugin import SakuraMem0Runtime

    boundary = object.__new__(PersonalRecallBoundary)
    boundary._lock = threading.RLock()
    boundary._closed = False
    boundary._status = "ready"
    boundary._records = None
    boundary._daily = daily
    boundary._memory_dir = _memory(tmp_path)
    boundary.scope = "alice"
    return boundary, SakuraMem0Runtime(tmp_path, "alice", boundary=boundary)


def test_archive_sections_are_editable_in_memory_management(tmp_path: Path, monkeypatch) -> None:
    from plugins.builtin.sakura_mem0 import personal_records

    monkeypatch.setattr(personal_records, "_require_write_mode", lambda *_a, **_k: None)
    boundary, runtime = _management(tmp_path, daily=True)
    path, _original = _v1(boundary._memory_dir)

    items = runtime.query_collection({})["items"]
    assert [item["itemId"] for item in items] == ["core_profile:alice#legacy"]
    assert items[0]["values"]["content"] == V1_TEXT
    assert items[0]["values"]["layer"] == "core_profile"

    edited = runtime.update_collection_item("core_profile:alice#legacy", {"content": "改写后的旧档案。"})
    assert edited["values"]["content"] == "改写后的旧档案。"
    assert _saved(path)["alice"]["sections"] == {"legacy": "改写后的旧档案。"}
    assert runtime.delete_collection_item("core_profile:alice#legacy") == {"deleted": True}
    assert "alice" not in _saved(path)
    assert _saved(path)["bob"]["content"] == "bob stays"


def test_archive_edits_need_the_daily_entry(tmp_path: Path) -> None:
    boundary, runtime = _management(tmp_path, daily=False)
    _v1(boundary._memory_dir)
    with pytest.raises(ValueError, match="READ_ONLY"):
        runtime.update_collection_item("core_profile:alice#legacy", {"content": "x"})
