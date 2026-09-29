import sqlite3
from contextlib import closing

import pytest

from app.legacy_import.errors import LegacyImportError
from app.legacy_import.history import import_history
from app.legacy_import.inspector import inspect_installation
from app.legacy_import.importer import run_legacy_import
from app.legacy_import.incremental import inspect_character_data_import
from app.legacy_import.models import LegacyInspection


def history_source(tmp_path):
    source = tmp_path / "source"
    history = source / "data/chat_history"
    history.mkdir(parents=True)
    (history / "alice.jsonl").write_text('{"role":"user","created_at":"2026-09-14T00:00:00+00:00","content":"old backup"}\n')
    with closing(sqlite3.connect(history / "alice.db")) as db:
        db.execute("CREATE TABLE chat_history(id INTEGER PRIMARY KEY, content TEXT)")
        db.execute("INSERT INTO chat_history VALUES(1,'new private sentinel')")
        db.commit()
    return source


def test_direct_history_conversion_never_falls_back_to_stale_jsonl(tmp_path):
    source = history_source(tmp_path)
    target = tmp_path / "staged"
    with pytest.raises(LegacyImportError, match="LEGACY_PERSONAL_HISTORY_INVALID"):
        import_history(source, target, character_ids=("alice",))
    assert not target.exists()


def test_first_import_rechecks_history_even_with_compatible_cached_preview(tmp_path):
    source = history_source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    preview = LegacyInspection(1, True, "0.9.10", "windows", "synthetic", False, 0, 10000000, {})
    with pytest.raises(LegacyImportError, match="LEGACY_PERSONAL_HISTORY_INVALID"):
        run_legacy_import(source, target, inspection=preview)
    assert list(target.iterdir()) == []


def test_preview_and_incremental_report_sqlite_history_without_private_text(tmp_path):
    source = history_source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    preview = inspect_installation(source, target)
    assert not preview.compatible
    assert any(row["code"] == "LEGACY_PERSONAL_HISTORY_INVALID" for row in preview.blockers)
    assert "private sentinel" not in str(preview.blockers)
    with pytest.raises(LegacyImportError, match="LEGACY_PERSONAL_HISTORY_INVALID"):
        inspect_character_data_import(source, target)
