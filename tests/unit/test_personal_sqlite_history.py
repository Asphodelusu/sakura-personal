import json
import sqlite3
from contextlib import closing

import pytest

from app.legacy_import.history import import_history
from app.legacy_import.incremental import _merge_timeline
from app.legacy_import.errors import LegacyImportError
from app.storage.timeline import TimelineStore


def fixture_source(tmp_path):
    root = tmp_path / "source"
    folder = root / "data/chat_history"
    folder.mkdir(parents=True)
    path = folder / "alice.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE chat_history(id INTEGER PRIMARY KEY,created_at TEXT,role TEXT,content TEXT,translation TEXT,tone TEXT,portrait TEXT,channel TEXT,debug TEXT,extra TEXT)")
        for key, role, text in [(10, "user", "latest human"), (20, "assistant", "first"), (30, "assistant", "second"), (40, "error", "diagnostic")]:
            db.execute("INSERT INTO chat_history VALUES(?,?,?,?,?,?,?,?,?,?)", (key, "2026-09-17T12:00:00+08:00", role, text, "translation", "neutral", "", "chat", "private debug", "unknown"))
        db.commit()
    (folder / "alice.jsonl").write_text('{"created_at":"2020-01-01T00:00:00+00:00","role":"user","content":"stale backup"}\n')
    return root, path


def test_sqlite_authority_preserves_rows_segments_and_unknown_fields_through_merge(tmp_path):
    source, db_path = fixture_source(tmp_path)
    before = db_path.read_bytes()
    converted, merged = tmp_path / "converted", tmp_path / "merged"
    stats = import_history(source, converted, character_ids=("alice",))
    assert stats.source_records == 4 and stats.errors_quarantined == 0
    timeline = TimelineStore(converted / "data/chat_history/timeline.sqlite3")
    entries = timeline.read_all("alice")
    assert len(entries) == 3
    assert entries[0].payload["text"] == "latest human"
    assert len(entries[1].payload["segments"]) == 2
    assert entries[2].payload["eventType"] == "legacy_error"
    _merge_timeline(converted, merged, overwrite_conflicts=False)
    with closing(sqlite3.connect(merged / "data/chat_history/timeline.sqlite3")) as db:
        rows = db.execute("SELECT source_row_id,entry_id,segment_index,record_json FROM personal_history_rows ORDER BY source_row_id").fetchall()
    assert [r[0] for r in rows] == [10, 20, 30, 40]
    assert rows[1][1] == rows[2][1] == entries[1].entry_id
    assert [rows[1][2], rows[2][2]] == [0, 1]
    assert json.loads(rows[1][3])["debug"] == "private debug"
    assert json.loads(rows[1][3])["extra"] == "unknown"
    assert json.loads(rows[3][3])["role"] == "error"
    repeated = tmp_path / "again"
    import_history(source, repeated, character_ids=("alice",), identity_root=merged)
    assert [r.entry_id for r in TimelineStore(repeated / "data/chat_history/timeline.sqlite3").read_all("alice")] == [r.entry_id for r in entries]
    assert db_path.read_bytes() == before


def test_preview_counts_authoritative_sqlite_rows(tmp_path):
    from app.legacy_import.inspector import inspect_installation
    source, _ = fixture_source(tmp_path)
    assert inspect_installation(source, tmp_path / "target").domains["history"].items == 4


def test_prior_jsonl_conversion_rejects_sqlite_identity_switch(tmp_path):
    source, path = fixture_source(tmp_path)
    saved = path.read_bytes()
    path.unlink()
    target = tmp_path / "target"
    import_history(source, target, character_ids=("alice",))
    path.write_bytes(saved)
    with pytest.raises(LegacyImportError, match="LEGACY_PERSONAL_HISTORY_IDENTITY_CONFLICT"):
        import_history(source, tmp_path / "staging", character_ids=("alice",), identity_root=target)
    assert not (tmp_path / "staging").exists()


def test_metadata_only_change_requires_confirmation_and_merges_once(tmp_path):
    from app.legacy_import.history_sqlite import read_personal_history_rows
    from app.legacy_import.incremental import _inspect_timeline, _Plan
    source, path = fixture_source(tmp_path)
    target, changed = tmp_path / "target", tmp_path / "changed"
    import_history(source, target, character_ids=("alice",))
    before = read_personal_history_rows(target / "data/chat_history/timeline.sqlite3")
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE chat_history SET debug='updated' WHERE id=20")
        db.commit()
    import_history(source, changed, character_ids=("alice",), identity_root=target)
    plan = _Plan("synthetic")
    _inspect_timeline(changed, target, plan)
    assert plan.public()["totals"]["historyConflicts"] == 1
    assert plan.public()["totals"]["historyIdentical"] == 2
    with pytest.raises(LegacyImportError, match="LEGACY_DATA_IMPORT_CONFIRMATION_REQUIRED"):
        _merge_timeline(changed, target, overwrite_conflicts=False)
    assert read_personal_history_rows(target / "data/chat_history/timeline.sqlite3") == before
    _merge_timeline(changed, target, overwrite_conflicts=True)
    _merge_timeline(changed, target, overwrite_conflicts=False)
    rows = read_personal_history_rows(target / "data/chat_history/timeline.sqlite3")
    assert len(rows) == 4
    assert json.loads(rows[1][-1])["debug"] == "updated"


@pytest.mark.parametrize("damage", ["naive_time", "unknown_role", "invalid_translation", "wal"])
def test_invalid_sqlite_never_falls_back_to_jsonl_or_creates_target(tmp_path, damage):
    source, path = fixture_source(tmp_path)
    if damage == "wal":
        path.with_name(path.name + "-wal").write_bytes(b"uncheckpointed")
    else:
        field, value = {"naive_time": ("created_at", "2026-09-17T12:00:00"), "unknown_role": ("role", "unknown"), "invalid_translation": ("translation", 100)}[damage]
        with closing(sqlite3.connect(path)) as db:
            if damage == "invalid_translation":
                db.execute("UPDATE chat_history SET translation=NULL WHERE id=20")
            else:
                db.execute(f"UPDATE chat_history SET {field}=? WHERE id=20", (value,))
            db.commit()
    with pytest.raises(LegacyImportError):
        import_history(source, tmp_path / "target", character_ids=("alice",))
    assert not (tmp_path / "target").exists()
