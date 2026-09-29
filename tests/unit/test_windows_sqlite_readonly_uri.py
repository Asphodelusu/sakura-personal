"""Synthetic SQLite read-only URI coverage. No personal databases."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.legacy_import.errors import LegacyImportError
from app.legacy_import.history_sqlite import read_personal_history_rows, read_sqlite_history


def _history_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.execute(
            "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, created_at TEXT, role TEXT, content TEXT)"
        )
        database.execute(
            "INSERT INTO chat_history VALUES (1, '2020-01-01T00:00:00+00:00', 'user', 'hello')"
        )
        database.commit()


def _extended(path: Path) -> Path:
    return Path("\\\\?\\" + str(path.resolve()))


@pytest.fixture(params=[
    pytest.param(False, id="ordinary"),
    pytest.param(True, id="extended", marks=pytest.mark.skipif(
        os.name != "nt", reason="Windows extended path syntax",
    )),
])
def path_spelling(request: pytest.FixtureRequest):
    return _extended if request.param else Path


def test_readonly_uri_reads_extended_ordinary_and_reserved_characters(tmp_path: Path, path_spelling) -> None:
    from app.legacy_import.files import sqlite_readonly_uri

    ordinary = tmp_path / "plain" / "chat.db"
    reserved = tmp_path / "dir name" / "a#b%c.db"
    _history_db(ordinary)
    _history_db(reserved)
    for path in (path_spelling(ordinary), path_spelling(reserved)):
        uri = sqlite_readonly_uri(path)
        assert "%3F" not in uri.split("?", 1)[0]
        assert uri.endswith("?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as database:
            assert database.execute("SELECT content FROM chat_history").fetchone()[0] == "hello"
            with pytest.raises(sqlite3.OperationalError):
                database.execute("CREATE TABLE rejected(x)")


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC URI construction")
def test_unc_uri_is_constructed_without_opening_a_share() -> None:
    from app.legacy_import.files import sqlite_uri_from_resolved

    uri = sqlite_uri_from_resolved(r"\\?\UNC\server\share\dir name\a#b%.db")
    assert uri == "file://server/share/dir%20name/a%23b%25.db?mode=ro"
    assert not uri.startswith("file://%3F")


def test_history_sqlite_read_keeps_immutable_and_wal_rejection(tmp_path: Path, path_spelling) -> None:
    database = tmp_path / "dir name" / "chat.db"
    _history_db(database)
    before = {path.name for path in database.parent.iterdir()}
    rows = list(read_sqlite_history(path_spelling(database)))
    after = {path.name for path in database.parent.iterdir()}
    assert rows[0]["content"] == "hello"
    assert not any(name.endswith(("-shm", "-journal")) for name in after - before)

    wal_database = tmp_path / "wal" / "chat.db"
    wal_database.parent.mkdir()
    holder = sqlite3.connect(wal_database)
    writer = sqlite3.connect(wal_database)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE chat_history (id INTEGER PRIMARY KEY, created_at TEXT, role TEXT, content TEXT)")
        holder.commit()
        writer.execute("BEGIN")
        holder.execute(
            "INSERT INTO chat_history VALUES (1, '2020-01-01T00:00:00+00:00', 'user', 'hello')"
        )
        holder.commit()
        wal = wal_database.with_name(wal_database.name + "-wal")
        assert wal.stat().st_size > 0
        before_wal = {path.name for path in wal_database.parent.iterdir()}
        with pytest.raises(LegacyImportError) as caught:
            list(read_sqlite_history(path_spelling(wal_database)))
        assert caught.value.code == "LEGACY_PERSONAL_HISTORY_WAL_PENDING"
        assert {path.name for path in wal_database.parent.iterdir()} == before_wal
    finally:
        writer.close()
        holder.close()


def test_personal_rows_and_incremental_snapshot_use_readonly_uri(tmp_path: Path, path_spelling) -> None:
    from app.legacy_import.incremental import _sqlite_snapshot

    database = tmp_path / "dir name" / "mem0_history.db"
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as source:
        source.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, memory_id TEXT)")
        source.execute("INSERT INTO history VALUES (1, 'row')")
        source.commit()
    destination = tmp_path / "copy" / "mem0_history.db"
    assert _sqlite_snapshot(path_spelling(database), destination)
    with closing(sqlite3.connect(destination)) as copied:
        assert copied.execute("SELECT memory_id FROM history").fetchone()[0] == "row"

    personal = tmp_path / "personal rows" / "timeline.sqlite3"
    personal.parent.mkdir()
    with closing(sqlite3.connect(personal)) as target:
        target.execute(
            """CREATE TABLE personal_history_rows (
                character_id TEXT, source_file TEXT, source_row_id INTEGER,
                entry_id TEXT, segment_index INTEGER, record_json TEXT
            )"""
        )
        target.execute("INSERT INTO personal_history_rows VALUES ('c', 'f', 1, 'e', 0, '{}')")
        target.commit()
    assert read_personal_history_rows(path_spelling(personal))[0][3] == "e"

    from app.legacy_import.history import read_history_identities

    root = path_spelling(tmp_path / "history-root")
    identity_db = root / "data" / "chat_history" / "timeline.sqlite3"
    identity_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(identity_db)) as target:
        target.execute(
            "CREATE TABLE legacy_history_identities (source_identity TEXT, kind TEXT, item_id TEXT)"
        )
        target.execute("INSERT INTO legacy_history_identities VALUES ('id', 'human', 'item')")
        target.commit()
    assert read_history_identities(root) == {("id", "human"): "item"}
