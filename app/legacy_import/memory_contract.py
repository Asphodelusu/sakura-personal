"""Import guard shared by preview, initial import and incremental import."""
from pathlib import Path

from plugins.builtin.sakura_mem0.index_contract import (
    PersonalMemoryIndexUnsupported, PersonalCopyIncomplete, require_supported_index_layout,
)
from .errors import LegacyImportError


def require_importable_memory(memory_dir: Path) -> None:
    try:
        require_supported_index_layout(memory_dir)
    except PersonalMemoryIndexUnsupported as exc:
        raise LegacyImportError("LEGACY_PERSONAL_MEMORY_UNSUPPORTED", "inspect") from exc
    except PersonalCopyIncomplete as exc:
        raise LegacyImportError("LEGACY_PERSONAL_COPY_INCOMPLETE", "inspect") from exc


def require_importable_memory_roots(source: Path, target: Path) -> None:
    for root in (source, target):
        require_importable_memory(root / "data" / "memory")


class _InspectionEncoder:
    """One owner across admission rejection and backend shutdown."""
    def __init__(self, encoder):
        self.encoder = encoder
        self.closed = False

    def embed(self, *args, **kwargs):
        return self.encoder.embed(*args, **kwargs)

    def close(self):
        if not self.closed:
            self.closed = True
            close = getattr(self.encoder, "close", None)
            if callable(close):
                close()


def inspect_personal_migration(work_root, *, snapshot=None, identity=None, encoder=None, cancel_event=None):
    """Inspect an exclusively owned, completed copy with converted Timeline.

    Use snapshot for local artifact verification, or identity/encoder for synthetic
    rehearsal (owned and closed on every outcome). Does not convert, publish, switch runtime or
    bypass the default import guard. Backend opening may initialize internal DB
    bookkeeping, so this must never be used on the baseline or daily data root.
    """
    from .personal_copy import _inventory, _plain_path
    from plugins.builtin.sakura_mem0.index_contract import COPY_STATE_FILE, require_complete_copy
    from plugins.builtin.sakura_mem0.personal_audit import read_timeline_source_entries
    from plugins.builtin.sakura_mem0.personal_records import open_personal_memory, open_personal_memory_from_snapshot

    owned = _InspectionEncoder(encoder)
    records = None
    try:
        if (snapshot is not None and (identity is not None or encoder is not None)
                or snapshot is None and (identity is None or encoder is None)):
            raise ValueError("PERSONAL_MIGRATION_ENCODER_SELECTION_INVALID")
        def check():
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("PERSONAL_AUDIT_CANCELLED")

        check()
        root = Path(work_root).absolute()
        _plain_path(root)
        if not root.is_dir() or not (root / COPY_STATE_FILE).is_file():
            raise ValueError("PERSONAL_MIGRATION_COPY_REQUIRED")
        require_complete_copy(root)
        # Reject nested links/hardlinks before any database can be opened.
        _inventory(root, check)
        sources = read_timeline_source_entries(root / "data/chat_history/timeline.sqlite3",
                                               cancel_event=cancel_event)
        check()
        if snapshot is not None:
            records = open_personal_memory_from_snapshot(root / "data/memory", snapshot=snapshot)
        else:
            records = open_personal_memory(root / "data/memory", identity=identity, encoder=owned)
        return records.audit_references(sources, cancel_event=cancel_event)
    finally:
        try:
            if records is not None:
                records.close()
        finally:
            owned.close()
