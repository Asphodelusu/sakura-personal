"""Prepare an explicit disposable memory copy. Never opens baseline databases."""
import json
from pathlib import Path
import sqlite3

from app.legacy_import.personal_copy import PersonalCopyError, STATE_FILE, _plain_path, prepare_personal_copy
from app.legacy_import.personal_windows_copy import hold_windows_source_files
from plugins.builtin.sakura_mem0.personal_records import WRITE_REHEARSAL


def prepare_rehearsal(baseline, destination):
    baseline, destination = Path(baseline).absolute(), Path(destination).absolute()
    _plain_path(baseline)
    _plain_path(destination)
    if (destination.resolve().is_relative_to(baseline.resolve())
            or baseline.resolve().is_relative_to(destination.resolve())):
        raise PersonalCopyError('PERSONAL_COPY_PATH_INVALID')
    _plain_path(baseline / STATE_FILE)
    state = json.loads((baseline / STATE_FILE).read_text(encoding='utf-8'))
    if state.get('state') != 'complete' or state.get('role') != 'baseline':
        raise PersonalCopyError('PERSONAL_BASELINE_REQUIRED')
    source = baseline / 'data/memory'
    with hold_windows_source_files(source) as lease:
        result = prepare_personal_copy(source, destination, source_is_quiescent=lease.is_held)
    # Recover/checkpoint only the newly copied databases, including retained WAL.
    for path in destination.rglob('*'):
        if path.is_file() and path.suffix in {'.db', '.sqlite', '.sqlite3'}:
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True)
            try:
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise PersonalCopyError('PERSONAL_SQLITE_INTEGRITY_FAILED')
                if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise PersonalCopyError('PERSONAL_SQLITE_BUSY')
            finally:
                db.close()
    with (destination / WRITE_REHEARSAL).open('x', encoding='utf-8') as stream:
        json.dump({'purpose': 'personal-memory-write-rehearsal',
                   'root': str(destination.resolve())}, stream)
    return result
