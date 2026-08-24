from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.agent.memory import CoreProfileStorageError, MemoryStore
from app.storage.paths import StoragePaths


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(base_dir=tmp_path, scope_id="Sakura", memory_client=object())


def _core_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_profiles()


def _write_profile(tmp_path: Path, record: dict[str, Any]) -> Path:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": "core_profile:Sakura", **record}
    path.write_text(
        json.dumps({"Sakura": payload}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _saved_record(tmp_path: Path) -> dict[str, Any]:
    return json.loads(_core_path(tmp_path).read_text(encoding="utf-8"))["Sakura"]


def test_v1_read_returns_content_without_touching_file(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {"Sakura": {"id": "core_profile:Sakura", "content": "原文", "memory": "原文", "metadata": {}}},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    path.write_text(original, encoding="utf-8")

    record = _store(tmp_path).core_profile()

    assert record is not None
    assert record["content"] == "原文"
    assert path.read_text(encoding="utf-8") == original


def test_v2_reads_content_without_rewriting(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"schema_version": 2, "content": "缓存正文", "memory": "缓存正文", "sections": {"legacy": "原文"}, "metadata": {}})
    assert _store(tmp_path).core_profile()["content"] == "缓存正文"


def test_v2_missing_content_renders_sections_read_only(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, {"schema_version": 2, "sections": {"legacy": "第一段", "今の関係": "第二段"}, "metadata": {}})
    before = path.read_bytes()
    record = _store(tmp_path).core_profile()
    assert record is not None
    assert record["content"] == "第一段\n\n第二段"
    assert path.read_bytes() == before


def test_unknown_schema_with_content_is_read_only_compatible(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"schema_version": 99, "content": "未来正文", "metadata": {}})
    assert _store(tmp_path).core_profile()["content"] == "未来正文"


def test_unknown_schema_without_content_does_not_interpret_future_sections(
    tmp_path: Path,
) -> None:
    _write_profile(
        tmp_path,
        {"schema_version": 99, "sections": {"future": "未来正文"}, "metadata": {}},
    )

    assert _store(tmp_path).core_profile() is None


@pytest.mark.parametrize(
    "sections",
    [
        {"legacy": "正文", "invalid": 123},
        {"legacy": "正文", "empty": "   "},
    ],
)
def test_v2_invalid_sections_do_not_partially_render(
    tmp_path: Path,
    sections: dict[Any, Any],
) -> None:
    _write_profile(
        tmp_path,
        {"schema_version": 2, "sections": sections, "metadata": {}},
    )

    assert _store(tmp_path).core_profile() is None


def test_corrupt_json_read_returns_none_without_overwrite(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    assert _store(tmp_path).core_profile() is None
    assert path.read_bytes() == before


def test_v1_next_write_migrates_to_v2_and_preserves_created_at(tmp_path: Path) -> None:
    _write_profile(tmp_path, {
        "content": "原文",
        "memory": "原文",
        "metadata": {"created_at": "2026-01-01T00:00:00+08:00", "category": "identity"},
    })

    _store(tmp_path).set_core_profile("原文", {"confidence": 0.9})

    saved = json.loads(_core_path(tmp_path).read_text(encoding="utf-8"))["Sakura"]
    assert saved["schema_version"] == 2
    assert saved["content"] == "原文"
    assert saved["memory"] == "原文"
    assert saved["sections"] == {"legacy": "原文"}
    assert saved["metadata"]["created_at"] == "2026-01-01T00:00:00+08:00"
    assert saved["metadata"]["confidence"] == 0.9


def test_repeated_v2_full_write_keeps_single_legacy_section(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_core_profile("原文")
    store.set_core_profile("原文")
    saved = _saved_record(tmp_path)
    assert saved["sections"] == {"legacy": "原文"}


def test_full_write_replaces_stale_formal_sections_with_legacy(tmp_path: Path) -> None:
    _write_profile(tmp_path, {
        "schema_version": 2,
        "content": "旧缓存",
        "memory": "旧缓存",
        "sections": {"今の関係": "旧关系", "今の私": "旧自我"},
        "metadata": {},
    })
    _store(tmp_path).set_core_profile("新的整段正文")
    assert _saved_record(tmp_path)["sections"] == {"legacy": "新的整段正文"}


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_unknown_schema_rejects_mutation_without_touching_bytes(tmp_path: Path, operation: str) -> None:
    path = _write_profile(tmp_path, {"schema_version": 99, "content": "未来正文", "metadata": {}})
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_corrupt_json_rejects_mutation_without_touching_bytes(tmp_path: Path, operation: str) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_non_object_top_level_rejects_mutation_without_touching_bytes(
    tmp_path: Path,
    operation: str,
) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["not-an-object"]\n', encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)

    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()

    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_non_object_scope_record_rejects_mutation_without_touching_bytes(
    tmp_path: Path,
    operation: str,
) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"Sakura": "not-an-object"}\n', encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)

    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()

    assert path.read_bytes() == before


def test_overwrite_creates_backup_of_previous_file(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, {"content": "旧正文", "memory": "旧正文", "metadata": {}})
    before = path.read_bytes()
    _store(tmp_path).set_core_profile("新正文")
    assert path.with_name(path.name + ".bak").read_bytes() == before


def test_first_write_does_not_create_empty_backup(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    _store(tmp_path).set_core_profile("正文")
    assert path.exists()
    assert not path.with_name(path.name + ".bak").exists()


FORMAL_SECTIONS = (
    "今の関係",
    "あなたについて知っていること",
    "今の私",
    "大切な約束と境界",
)
CREATED_AT = "2026-01-01T00:00:00+08:00"
UPDATED_AT = "2026-01-02T00:00:00+08:00"
SECRET_BODY = "SECRET_BODY_XYZ"


def _formal_v2(sections: dict[str, str], *, content: str = "旧缓存") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "content": content,
        "memory": content,
        "sections": dict(sections),
        "metadata": {"created_at": CREATED_AT, "updated_at": UPDATED_AT, "source": "manual"},
    }


def _legacy_v2(legacy: str) -> dict[str, Any]:
    return _formal_v2({"legacy": legacy}, content=legacy)


def _bak(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _render_formal(sections: dict[str, str]) -> str:
    parts: list[str] = []
    for name in FORMAL_SECTIONS:
        body = str(sections.get(name) or "").strip()
        if not body:
            continue
        parts.append(f"＜{name}＞\n{body}")
    return "\n\n".join(parts)


def test_patch_renders_formal_sections_in_fixed_order(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        _formal_v2(
            {
                "今の関係": "旧关系",
                "あなたについて知っていること": "旧认识",
                "今の私": "旧自我",
                "大切な約束と境界": "旧约定",
            }
        ),
    )

    result = _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {
            "今の私": "新自我",
            "今の関係": "新关系",
        },
        candidate_ids=["cc_1"],
    )

    expected = (
        "＜今の関係＞\n新关系\n\n"
        "＜あなたについて知っていること＞\n旧认识\n\n"
        "＜今の私＞\n新自我\n\n"
        "＜大切な約束と境界＞\n旧约定"
    )
    saved = _saved_record(tmp_path)
    assert saved["content"] == expected
    assert saved["memory"] == expected
    assert list(saved["sections"]) == list(FORMAL_SECTIONS)
    assert saved["sections"]["今の関係"] == "新关系"
    assert saved["sections"]["今の私"] == "新自我"
    assert saved["sections"]["あなたについて知っていること"] == "旧认识"
    assert result["content"] == expected
    assert result["memory"] == expected


def test_patch_preserves_created_at_and_writes_maintainer_metadata(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        _formal_v2({"今の関係": "旧关系", "今の私": "旧自我"}),
    )

    result = _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {"今の関係": "新关系"},
        candidate_ids=["cc_a", "cc_b"],
    )

    saved = _saved_record(tmp_path)
    assert saved["metadata"]["created_at"] == CREATED_AT
    assert saved["metadata"]["updated_at"] != UPDATED_AT
    assert saved["metadata"]["updated_at"] == result["metadata"]["updated_at"]
    assert saved["metadata"]["source"] == "core_maintainer"
    assert saved["metadata"]["candidate_ids"] == ["cc_a", "cc_b"]


def test_successful_patch_backups_previous_bytes(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, _formal_v2({"今の関係": "旧关系"}))
    before = path.read_bytes()

    _store(tmp_path).patch_core_profile_sections(UPDATED_AT, {"今の関係": "新关系"})

    assert _bak(path).read_bytes() == before


def test_true_noop_patch_writes_nothing(tmp_path: Path) -> None:
    sections = {"今の関係": "我们是恋人。"}
    rendered = _render_formal(sections)
    path = _write_profile(tmp_path, _formal_v2(sections, content=rendered))
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()

    result = _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {"今の関係": "  我们是恋人。  \n"},
        candidate_ids=["cc_noop"],
    )

    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before
    assert result["content"] == rendered
    assert result["metadata"]["updated_at"] == UPDATED_AT
    assert result["metadata"]["source"] == "manual"


def test_unknown_section_is_rejected_without_touching_files(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, _formal_v2({"今の関係": SECRET_BODY}))
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()

    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError, match="section") as exc_info:
        store.patch_core_profile_sections(UPDATED_AT, {"関係の記録": "新说法"})

    assert SECRET_BODY not in str(exc_info.value)
    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before


def test_more_than_two_ordinary_section_changes_are_rejected(tmp_path: Path) -> None:
    path = _write_profile(
        tmp_path,
        _formal_v2(
            {
                "今の関係": "旧关系",
                "今の私": "旧自我",
                "大切な約束と境界": "旧约定",
            }
        ),
    )
    before = path.read_bytes()

    with pytest.raises(CoreProfileStorageError):
        _store(tmp_path).patch_core_profile_sections(
            UPDATED_AT,
            {
                "今の関係": "新关系",
                "今の私": "新自我",
                "大切な約束と境界": "新约定",
            },
        )

    assert path.read_bytes() == before
    assert not _bak(path).exists()


def test_optimistic_lock_conflict_leaves_primary_and_backup_untouched(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, _formal_v2({"今の関係": SECRET_BODY}))
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()

    with pytest.raises(CoreProfileStorageError) as exc_info:
        _store(tmp_path).patch_core_profile_sections(
            "2026-01-03T00:00:00+08:00",
            {"今の関係": "新关系"},
        )

    assert SECRET_BODY not in str(exc_info.value)
    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before


def test_legacy_migration_moves_each_sentence_once_and_removes_legacy(tmp_path: Path) -> None:
    legacy = (
        "他叫我「小樱」。\n"
        "我们确认了恋人关系。\n"
        "我答应晚上 11 点后不打电话！\n"
        "他喜欢抹茶。"
    )
    _write_profile(tmp_path, _legacy_v2(legacy))

    result = _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {
            "大切な約束と境界": "我答应晚上 11 点后不打电话！",
            "今の関係": "我们确认了恋人关系。",
            "あなたについて知っていること": "他叫我「小樱」。\n他喜欢抹茶。",
        },
        candidate_ids=["cc_mig"],
        migrate_legacy=True,
    )

    saved = _saved_record(tmp_path)
    assert "legacy" not in saved["sections"]
    assert list(saved["sections"]) == list(FORMAL_SECTIONS)
    assert saved["sections"]["今の関係"] == "我们确认了恋人关系。"
    assert saved["sections"]["あなたについて知っていること"] == "他叫我「小樱」。\n他喜欢抹茶。"
    assert saved["sections"]["今の私"] == ""
    assert saved["sections"]["大切な約束と境界"] == "我答应晚上 11 点后不打电话！"
    expected = (
        "＜今の関係＞\n我们确认了恋人关系。\n\n"
        "＜あなたについて知っていること＞\n他叫我「小樱」。\n他喜欢抹茶。\n\n"
        "＜大切な約束と境界＞\n我答应晚上 11 点后不打电话！"
    )
    assert saved["content"] == expected
    assert saved["memory"] == expected
    assert result["content"] == expected
    assert saved["metadata"]["created_at"] == CREATED_AT
    assert saved["metadata"]["source"] == "core_maintainer"
    assert saved["metadata"]["candidate_ids"] == ["cc_mig"]


def test_legacy_migration_accepts_collapsed_whitespace_and_reordering(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        _legacy_v2("他叫我  「小樱」。我们确认了恋人关系。"),
    )

    _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {
            "今の関係": "  我们确认了恋人关系。 ",
            "あなたについて知っていること": "他叫我 「小樱」。",
        },
        migrate_legacy=True,
    )

    saved = _saved_record(tmp_path)
    assert "legacy" not in saved["sections"]
    assert saved["sections"]["今の関係"] == "我们确认了恋人关系。"
    assert saved["sections"]["あなたについて知っていること"] == "他叫我 「小樱」。"


@pytest.mark.parametrize(
    "sections",
    [
        {"今の関係": "我们确认了恋人关系。\n他叫我「小樱」。\n多了一句。"},
        {"今の関係": "他称呼我「小樱」。\n我们确认了恋人关系。"},
        {"今の関係": "他叫我「小樱」。\n他叫我「小樱」。\n我们确认了恋人关系。"},
        {"今の関係": "他叫我「小樱」。"},
        {"今の関係": "他叫我小樱。\n我们确认了恋人关系。"},
        {"今の関係": "他叫我「小樱」。\n我们确认了恋人关系，大概。"},
        {"今の関係": "他叫我「小樱」。\n我们确认了恋人关系。约定 11 点。"},
    ],
)
def test_legacy_migration_rejects_rewrite_add_duplicate_or_omission(
    tmp_path: Path,
    sections: dict[str, str],
) -> None:
    legacy = "他叫我「小樱」。\n我们确认了恋人关系。约定 3.14 见面。"
    path = _write_profile(tmp_path, _legacy_v2(legacy))
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()

    with pytest.raises(CoreProfileStorageError) as exc_info:
        _store(tmp_path).patch_core_profile_sections(
            UPDATED_AT,
            sections,
            migrate_legacy=True,
        )

    message = str(exc_info.value)
    assert "小樱" not in message
    assert "3.14" not in message
    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before


def test_legacy_migration_preserves_names_quotes_and_numbers(tmp_path: Path) -> None:
    legacy = "他叫我「小樱」。约定 3.14 见面。我答应晚上 11 点后不打电话！"
    _write_profile(tmp_path, _legacy_v2(legacy))

    _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {
            "あなたについて知っていること": "他叫我「小樱」。约定 3.14 见面。",
            "大切な約束と境界": "我答应晚上 11 点后不打电话！",
        },
        migrate_legacy=True,
    )

    saved = _saved_record(tmp_path)
    joined = "\n".join(saved["sections"][name] for name in FORMAL_SECTIONS)
    assert "「小樱」" in joined
    assert "3.14" in joined
    assert "11" in joined
    assert saved["content"].count("「小樱」") == 1
    assert saved["content"].count("3.14") == 1


def test_ordinary_patch_rejects_legacy_profile_without_migration(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, _legacy_v2("原文"))
    before = path.read_bytes()

    with pytest.raises(CoreProfileStorageError):
        _store(tmp_path).patch_core_profile_sections(UPDATED_AT, {"今の関係": "新关系"})

    assert path.read_bytes() == before


def test_v1_profile_rejects_section_patch_without_touching_bytes(tmp_path: Path) -> None:
    path = _write_profile(
        tmp_path,
        {"content": "原文", "memory": "原文", "metadata": {"updated_at": UPDATED_AT}},
    )
    before = path.read_bytes()

    with pytest.raises(CoreProfileStorageError):
        _store(tmp_path).patch_core_profile_sections(UPDATED_AT, {"今の関係": "新关系"})

    assert path.read_bytes() == before


def test_same_second_writes_change_optimistic_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 8, 24, 18, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr("app.agent.memory._core_profile_timestamp_now", lambda: frozen)
    sections = {
        "今の関係": "一",
        "あなたについて知っていること": "认识",
        "今の私": "自我",
        "大切な約束と境界": "约定",
    }
    _write_profile(tmp_path, _formal_v2(sections, content=_render_formal(sections)))

    first = _store(tmp_path).patch_core_profile_sections(UPDATED_AT, {"今の関係": "二"})
    token1 = first["metadata"]["updated_at"]
    second = _store(tmp_path).patch_core_profile_sections(token1, {"今の関係": "三"})
    token2 = second["metadata"]["updated_at"]

    assert token1 != token2
    with pytest.raises(CoreProfileStorageError):
        _store(tmp_path).patch_core_profile_sections(token1, {"今の関係": "四"})
    assert _saved_record(tmp_path)["sections"]["今の関係"] == "三"


def test_independent_stores_share_path_lock_and_reread(tmp_path: Path) -> None:
    sections = {
        "今の関係": "旧关系",
        "今の私": "旧自我",
        "あなたについて知っていること": "旧认识",
        "大切な約束と境界": "旧约定",
    }
    _write_profile(tmp_path, _formal_v2(sections, content=_render_formal(sections)))
    successes: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            store = MemoryStore(base_dir=tmp_path, scope_id="Sakura", memory_client=object())
            result = store.patch_core_profile_sections(
                UPDATED_AT,
                {"今の関係": f"新关系{index}"},
            )
            successes.append(result)
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(errors) == 7
    assert all(isinstance(item, CoreProfileStorageError) for item in errors)
    saved = _saved_record(tmp_path)
    assert saved["sections"]["今の関係"] == successes[0]["sections"]["今の関係"]
    assert saved["metadata"]["updated_at"] == successes[0]["metadata"]["updated_at"]


def test_stored_unknown_section_is_rejected_without_deleting(tmp_path: Path) -> None:
    path = _write_profile(
        tmp_path,
        _formal_v2({"今の関係": SECRET_BODY, "関係の記録": "旧章"}),
    )
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()

    with pytest.raises(CoreProfileStorageError, match="section") as exc_info:
        _store(tmp_path).patch_core_profile_sections(UPDATED_AT, {"今の関係": "新关系"})

    assert SECRET_BODY not in str(exc_info.value)
    assert "旧章" not in str(exc_info.value)
    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before


def test_mixed_legacy_and_formal_sections_are_rejected_without_deleting(tmp_path: Path) -> None:
    path = _write_profile(
        tmp_path,
        _formal_v2({"legacy": SECRET_BODY, "今の関係": "旧关系"}),
    )
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()
    store = _store(tmp_path)

    with pytest.raises(CoreProfileStorageError) as ordinary:
        store.patch_core_profile_sections(UPDATED_AT, {"今の関係": "新关系"})
    with pytest.raises(CoreProfileStorageError) as migrated:
        store.patch_core_profile_sections(
            UPDATED_AT,
            {"今の関係": SECRET_BODY},
            migrate_legacy=True,
        )

    assert SECRET_BODY not in str(ordinary.value)
    assert SECRET_BODY not in str(migrated.value)
    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before


def test_stale_content_cache_is_repaired_when_sections_unchanged(tmp_path: Path) -> None:
    sections = {"今の関係": "我们是恋人。", "今の私": "我更依赖他了。"}
    path = _write_profile(tmp_path, _formal_v2(sections, content="旧缓存"))
    before = path.read_bytes()

    result = _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {"今の関係": "我们是恋人。"},
        candidate_ids=["cc_repair"],
    )

    expected = _render_formal(sections)
    saved = _saved_record(tmp_path)
    assert path.read_bytes() != before
    assert saved["content"] == expected
    assert saved["memory"] == expected
    assert saved["sections"]["今の関係"] == "我们是恋人。"
    assert saved["sections"]["今の私"] == "我更依赖他了。"
    assert saved["metadata"]["updated_at"] != UPDATED_AT
    assert saved["metadata"]["source"] == "core_maintainer"
    assert saved["metadata"]["candidate_ids"] == ["cc_repair"]
    assert result["content"] == expected
    assert _bak(path).read_bytes() == before


def test_backup_failure_aborts_before_primary_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_profile(tmp_path, _formal_v2({"今の関係": SECRET_BODY}))
    before = path.read_bytes()
    _bak(path).write_bytes(b'{"stale": true}\n')
    bak_before = _bak(path).read_bytes()
    original = Path.write_bytes

    def fail_backup(self: Path, data: bytes) -> int:
        if self.name.endswith(".bak"):
            raise OSError("disk full")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_backup)

    with pytest.raises(CoreProfileStorageError) as exc_info:
        _store(tmp_path).patch_core_profile_sections(UPDATED_AT, {"今の関係": "新关系"})

    assert SECRET_BODY not in str(exc_info.value)
    assert path.read_bytes() == before
    assert _bak(path).read_bytes() == bak_before


def test_legacy_migration_splits_ascii_period_but_keeps_decimals(tmp_path: Path) -> None:
    legacy = "He likes matcha. We are partners. 约定 3.14 见面。"
    _write_profile(tmp_path, _legacy_v2(legacy))

    _store(tmp_path).patch_core_profile_sections(
        UPDATED_AT,
        {
            "あなたについて知っていること": "He likes matcha. 约定 3.14 见面。",
            "今の関係": "We are partners.",
        },
        migrate_legacy=True,
    )

    saved = _saved_record(tmp_path)
    assert saved["sections"]["今の関係"] == "We are partners."
    assert "3.14" in saved["sections"]["あなたについて知っていること"]
    assert "He likes matcha." in saved["sections"]["あなたについて知っていること"]


def test_legacy_migration_rejects_dropping_intrasentence_whitespace(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, _legacy_v2("他叫我  小樱。"))
    before = path.read_bytes()

    with pytest.raises(CoreProfileStorageError):
        _store(tmp_path).patch_core_profile_sections(
            UPDATED_AT,
            {"今の関係": "他叫我小樱。"},
            migrate_legacy=True,
        )

    assert path.read_bytes() == before
