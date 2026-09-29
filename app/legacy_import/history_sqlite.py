"""Personal Qt SQLite history input and lossless per-row Timeline provenance."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from .errors import LegacyImportError
from .files import is_link_or_junction, sqlite_readonly_uri


def sqlite_history_files(source):
    folder = Path(source) / "data/chat_history"
    if not folder.is_dir():
        return []
    files = []
    for path in folder.iterdir():
        name = path.name.casefold()
        if name.endswith((".db-wal", ".db-shm")):
            database = path.with_name(path.name.rsplit("-", 1)[0])
            if not database.is_file() or is_link_or_junction(path) or (name.endswith("-wal") and path.stat().st_size):
                raise LegacyImportError("LEGACY_PERSONAL_HISTORY_WAL_PENDING", "inspect")
        elif name.endswith(".db"):
            if is_link_or_junction(path) or not path.is_file():
                raise LegacyImportError("LEGACY_PERSONAL_HISTORY_INVALID", "inspect")
            files.append(path)
    folded = [path.stem.casefold() for path in files]
    if len(folded) != len(set(folded)):
        raise LegacyImportError("LEGACY_PERSONAL_HISTORY_INVALID", "inspect")
    return sorted(files)


def read_sqlite_history(path):
    # The caller supplies an offline copy. Refuse outstanding WAL; immutable
    # avoids creating SHM/journal files in a source directory during inspection.
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise LegacyImportError("LEGACY_PERSONAL_HISTORY_WAL_PENDING", "inspect")
    try:
        with closing(sqlite3.connect(sqlite_readonly_uri(path) + "&immutable=1", uri=True)) as db:
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("database integrity")
            db.row_factory = sqlite3.Row
            columns = {row[1] for row in db.execute("PRAGMA table_info(chat_history)")}
            if not {"id", "created_at", "role", "content"}.issubset(columns):
                raise ValueError("schema")
            seen = set()
            for row in db.execute("SELECT * FROM chat_history ORDER BY id"):
                value = dict(row)
                key = value["id"]
                if type(key) is not int or key <= 0 or key in seen:
                    raise ValueError("identity")
                seen.add(key)
                if any(not isinstance(value[name], str) for name in ("created_at", "role", "content")):
                    raise ValueError("required text")
                if value["role"] not in {"user", "assistant", "system", "error"}:
                    raise ValueError("role")
                for name in ("translation", "tone", "portrait", "channel", "debug"):
                    if name in value and not isinstance(value[name], str):
                        raise ValueError("optional text")
                stamp = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError("timezone required")
                # Preserve extra scalar columns too; reject BLOB/non-JSON data
                # explicitly rather than discarding it during conversion.
                json.dumps(value, ensure_ascii=False, allow_nan=False)
                yield value
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise LegacyImportError("LEGACY_PERSONAL_HISTORY_INVALID", "inspect") from exc


def validate_sqlite_history_source(source):
    for path in sqlite_history_files(source):
        for _ in read_sqlite_history(path):
            pass


def write_personal_history_rows(connection, rows):
    connection.execute("""CREATE TABLE IF NOT EXISTS personal_history_rows (
        character_id TEXT NOT NULL, source_file TEXT NOT NULL, source_row_id INTEGER NOT NULL,
        entry_id TEXT NOT NULL, segment_index INTEGER, record_json TEXT NOT NULL,
        PRIMARY KEY(character_id,source_file,source_row_id,entry_id))""")
    connection.executemany("INSERT OR REPLACE INTO personal_history_rows VALUES(?,?,?,?,?,?)", rows)


def read_personal_history_rows(path):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(sqlite_readonly_uri(path), uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='personal_history_rows' AND type='table'").fetchone():
            return []
        return db.execute("SELECT character_id,source_file,source_row_id,entry_id,segment_index,record_json FROM personal_history_rows ORDER BY character_id,source_file,source_row_id,entry_id").fetchall()


def personal_rows_by_entry(path):
    result = {}
    for row in read_personal_history_rows(path):
        result.setdefault(row[3], []).append(row)
    return result
