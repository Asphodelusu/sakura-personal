"""Read-only reference audit under the records owner's exclusive operation lease."""
from collections import Counter
from contextlib import closing
import sqlite3

if __package__:
    from .index_contract import require_complete_copy
    from .personal_backend import _sqlite_uri
else:
    from index_contract import require_complete_copy
    from personal_backend import _sqlite_uri


def read_timeline_source_entries(path, *, cancel_event=None):
    """Read IDs from an existing offline Timeline without initializing its schema."""
    require_complete_copy(path)
    if not path.is_file():
        raise ValueError("PERSONAL_TIMELINE_MISSING")
    try:
        with closing(sqlite3.connect(_sqlite_uri(path) + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            if db.execute("PRAGMA application_id").fetchone()[0] <= 0:
                raise ValueError("PERSONAL_TIMELINE_INVALID")
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("PERSONAL_TIMELINE_INVALID")
            result, seen = {}, set()
            for key, scope in db.execute("SELECT entry_id,character_id FROM timeline_entries"):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("PERSONAL_AUDIT_CANCELLED")
                if (not isinstance(key, str) or not key or not isinstance(scope, str) or not scope
                        or any(c.isspace() for c in scope) or key in seen):
                    raise ValueError("PERSONAL_TIMELINE_INVALID")
                seen.add(key)
                result.setdefault(scope, set()).add(key)
            return result
    except sqlite3.Error as exc:
        raise ValueError("PERSONAL_TIMELINE_INVALID") from exc


def audit_references(backend, metadata, source_entries, *, cancel_event=None):
    if not isinstance(source_entries, dict) or any(
        not isinstance(scope, str) or not scope or not isinstance(ids, (set, frozenset))
        or any(not isinstance(key, str) or not key for key in ids)
        for scope, ids in source_entries.items()
    ):
        raise ValueError("PERSONAL_AUDIT_SOURCE_MAP_INVALID")
    source_entries = {scope: frozenset(ids) for scope, ids in source_entries.items()}
    issues = Counter()
    counts = Counter(memories_without_source_references=0, verified_source_references=0)

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("PERSONAL_AUDIT_CANCELLED")

    client = backend.vector_store.client

    def pages(collection):
        offset, seen = None, set()
        while True:
            check()
            rows, next_offset = client.scroll(collection, offset=offset, limit=128,
                                               with_payload=True, with_vectors=False)
            yield from rows
            if next_offset is None:
                return
            if not rows or next_offset in seen:
                raise RuntimeError("PERSONAL_AUDIT_PAGINATION_INVALID")
            seen.add(next_offset)
            offset = next_offset

    memories = {}
    for row in pages(backend.collection_name):
        check()
        payload = row.payload or {}
        scope = payload.get("user_id")
        if not isinstance(scope, str) or not scope or any(c.isspace() for c in scope):
            issues["MEMORY_SCOPE"] += 1
            scope = ""
        memories[str(row.id)] = scope
        counts["memories"] += 1
        refs = payload.get("source_entry_ids", [])
        if not isinstance(refs, list):
            issues["SOURCE_REFERENCE"] += 1
        else:
            if not refs:
                # Qt stored evidence excerpts, not history row IDs. Missing
                # provenance is not corruption, nor proof of a verified link.
                counts["memories_without_source_references"] += 1
            for ref in refs:
                counts["source_references"] += 1
                if not isinstance(ref, str) or ref not in source_entries.get(scope, set()):
                    issues["SOURCE_REFERENCE"] += 1
                else:
                    counts["verified_source_references"] += 1

    entities = backend.collection_name + "_entities"
    if client.collection_exists(entities):
        for row in pages(entities):
            check()
            counts["vector_entities"] += 1
            payload = row.payload or {}
            links = payload.get("linked_memory_ids")
            if not isinstance(links, list) or not links:
                issues["ENTITY_REFERENCE"] += 1
                continue
            for key in links:
                if not isinstance(key, str) or key not in memories:
                    issues["ENTITY_REFERENCE"] += 1
                elif payload.get("user_id") != memories[key]:
                    issues["ENTITY_SCOPE"] += 1

    for db, table, label, code in (
        (metadata.entities, "entity_memory", "entity_rows", "ENTITY_REFERENCE"),
        (metadata.access, "memory_access", "access_rows", "ACCESS_REFERENCE"),
    ):
        for (key,) in db.execute(f"SELECT memory_id FROM {table}"):
            check()
            counts[label] += 1
            if key not in memories:
                issues[code] += 1

    last_history = {}
    for key, event, deleted in backend.db.connection.execute(
        "SELECT memory_id,event,is_deleted FROM history ORDER BY rowid"
    ):
        check()
        counts["history_rows"] += 1
        if not isinstance(key, str) or not key or event not in {"ADD", "UPDATE", "DELETE"}:
            issues["HISTORY_REFERENCE"] += 1
        else:
            if (event == "DELETE") != (deleted == 1):
                issues["HISTORY_REFERENCE"] += 1
            last_history[key] = event == "DELETE" and deleted == 1
    for key in memories:
        if key not in last_history or last_history[key]:
            issues["HISTORY_REFERENCE"] += 1
    for key, deleted in last_history.items():
        if key not in memories and not deleted:
            issues["HISTORY_REFERENCE"] += 1
    check()
    return {"ok": not issues, "counts": dict(counts), "issues": dict(issues)}
