"""Scoped personal records and existing Qt metadata in an isolated work copy.

Cross-store failures are fail-closed, not advertised as rolled back. The durable
pending marker remains until a separate explicit recovery has been implemented.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

if __package__:
    from .personal_backend import _check_sqlite, _owned, open_personal_backend, _index_config, _read_object, _sqlite_uri, legacy_identity
    from .personal_entities import (extract_entities, expand_entity_aliases, extract_paren_alias_pairs,
                                    _MAX_INDEX_KEYS_PER_MEMORY, _alias_group_for)
    from .personal_audit import audit_references, read_timeline_source_entries
    from .personal_model import LocalPersonalEncoder
else:
    from personal_backend import _check_sqlite, _owned, open_personal_backend, _index_config, _read_object, _sqlite_uri, legacy_identity
    from personal_entities import (extract_entities, expand_entity_aliases, extract_paren_alias_pairs,
                                  _MAX_INDEX_KEYS_PER_MEMORY, _alias_group_for)
    from personal_audit import audit_references, read_timeline_source_entries
    from personal_model import LocalPersonalEncoder

PENDING = "personal_write_pending.json"
WRITE_REHEARSAL = ".personal-write-rehearsal.json"


def _admission_path(value):
    # Only equivalent Win32 spellings; do not resolve aliases or weaken link checks.
    if isinstance(value, str) and os.name == "nt" and value.startswith("\\\\?\\"):
        if value[4:8].upper() == "UNC\\":
            return "\\\\" + value[8:]
        if len(value) >= 7 and value[4].isalpha() and value[5:7] == ":\\":
            return value[4:]
    return value


def _require_write_rehearsal(root):
    # Explicit opt-in plus a completed, path-bound disposable copy. A marker
    # alone never changes normal plugin behavior; this is not an ACL boundary.
    try:
        for path in (root, *root.parents, root / WRITE_REHEARSAL, root / ".sakura-personal-copy.json"):
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("linked path")
        marker = _read_object(root / WRITE_REHEARSAL)
        copied = _read_object(root / ".sakura-personal-copy.json")
        if ({**marker, "root": _admission_path(marker.get("root"))}
                != {"purpose": "personal-memory-write-rehearsal", "root": _admission_path(str(root))}
                or copied.get("state") != "complete"):
            raise ValueError("invalid admission")
    except (OSError, ValueError) as exc:
        raise ValueError("PERSONAL_WRITE_REHEARSAL_REQUIRED") from exc


def _scope(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("PERSONAL_MEMORY_SCOPE_INVALID")
    return value


def _check_pending(root):
    if os.path.lexists(root / PENDING):
        raise RuntimeError("PERSONAL_MEMORY_WRITE_INCOMPLETE")


def _metadata_paths(root):
    result = []
    for name, table, columns in (
        ("entity_index.db", "entity_memory", {"entity", "memory_id", "updated_at"}),
        ("access_tracker.db", "memory_access", {"memory_id", "last_accessed"}),
    ):
        try:
            path = _owned(root, root / name)
            if not path.is_file():
                raise ValueError("missing")
            _check_sqlite(path, required_table=table)
            with closing(sqlite3.connect(_sqlite_uri(path) + "?mode=ro", uri=True)) as db:
                actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns.issubset(actual):
                    raise ValueError("schema")
                if name == "access_tracker.db":
                    migrated = db.execute("SELECT value FROM _meta WHERE key='migrated_from_json'").fetchone()
                    if migrated != ("1",):
                        raise ValueError("legacy JSON conversion is not complete")
            result.append(path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise ValueError("PERSONAL_METADATA_INVALID") from exc
    return result


class _Metadata:
    def __init__(self, paths):
        self.connections = []
        try:
            for path in paths:
                # mode=rw cannot silently create a replacement database.
                self.connections.append(sqlite3.connect(_sqlite_uri(path) + "?mode=rw", uri=True, check_same_thread=False))
        except BaseException:
            self.close()
            raise
        self.entities, self.access = self.connections

    def replace_entities(self, key, content, when):
        names = expand_entity_aliases(extract_entities(content), content=content)
        for left, right in extract_paren_alias_pairs(content):
            names.update((left, right))
            names.update(expand_entity_aliases((left, right), content=content))
        names = sorted(names, key=lambda name: (0 if _alias_group_for(name) else 1, len(name), name))[:_MAX_INDEX_KEYS_PER_MEMORY]
        with self.entities:
            self.entities.execute("DELETE FROM entity_memory WHERE memory_id=?", (key,))
            self.entities.executemany("INSERT INTO entity_memory(entity,memory_id,updated_at) VALUES(?,?,?)",
                                      [(name, key, when) for name in names])

    def remove(self, key):
        with self.entities:
            self.entities.execute("DELETE FROM entity_memory WHERE memory_id=?", (key,))
        with self.access:
            self.access.execute("DELETE FROM memory_access WHERE memory_id=?", (key,))

    def close(self):
        error = None
        for db in self.connections:
            try:
                db.close()
            except sqlite3.Error as exc:
                error = error or exc
        self.connections.clear()
        if error is not None:
            raise error


class PersonalMemoryRecords:
    def __init__(self, root, session, metadata, *, recall_only=False):
        self._root = root
        self._session = session
        self._metadata = metadata
        self._lock = threading.RLock()
        self._recall_only = recall_only

    @contextmanager
    def _operation(self, scope):
        _scope(scope)
        with self._lock:
            with self._session.operation() as backend:
                _check_pending(self._root)
                yield backend

    @contextmanager
    def _write(self, operation):
        if self._recall_only:
            raise RuntimeError("PERSONAL_LEGACY_RECALL_ONLY")
        marker = self._root / PENDING
        # Exclusive creation is also protection against overlapping writers.
        with marker.open("x", encoding="utf-8") as output:
            json.dump({"schema": 1, "operation": operation}, output)
            output.flush()
            os.fsync(output.fileno())
        yield
        marker.unlink()  # Only a fully successful write clears the marker.

    @staticmethod
    def _get(backend, scope, key, *, required=False):
        record = backend.get(key)
        if record is None or record.get("user_id") != scope:
            if required:
                raise ValueError("PERSONAL_MEMORY_SCOPE_OR_ID_INVALID")
            return None
        return record

    @contextmanager
    def _sparse_write(self, backend, content):
        if self._recall_only:
            raise RuntimeError("PERSONAL_LEGACY_RECALL_ONLY")
        vectors = backend.vector_store
        if not vectors._has_bm25_slot:
            yield
            return
        from mem0.memory.main import lemmatize_for_bm25
        text = lemmatize_for_bm25(content) or content
        original = vectors._encode_bm25
        # Prepare before changing vectors, entity links, history or the pending
        # marker. Reuse this result so a second encoding cannot silently fail.
        try:
            sparse = original(text)
        except Exception as exc:
            raise RuntimeError("PERSONAL_BM25_WRITE_UNAVAILABLE") from exc
        if sparse is None:
            raise RuntimeError("PERSONAL_BM25_WRITE_UNAVAILABLE")

        def prepared(value):
            if value != text:
                raise RuntimeError("PERSONAL_BM25_WRITE_TEXT_MISMATCH")
            return sparse

        # All record operations hold self._lock; no concurrent search observes
        # this operation-local adapter to the vendored insertion/update path.
        vectors._encode_bm25 = prepared
        try:
            yield
        finally:
            vectors._encode_bm25 = original

    def get(self, scope, key):
        with self._operation(scope) as backend:
            return self._get(backend, scope, key)

    def search(self, scope, query, *, limit=10):
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("PERSONAL_MEMORY_QUERY_INVALID")
        with self._operation(scope) as backend:
            return backend.search(query, filters={"user_id": scope}, top_k=limit)

    def list(self, scope, *, limit=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("PERSONAL_MEMORY_LIMIT_INVALID")
        with self._operation(scope) as backend:
            size = limit or 128
            while True:
                rows = backend.get_all(filters={"user_id": scope}, top_k=size)["results"]
                if any(row.get("user_id") != scope for row in rows):
                    raise ValueError("PERSONAL_MEMORY_SCOPE_OR_ID_INVALID")
                if limit is not None or len(rows) < size:
                    return rows
                size *= 2

    def create(self, scope, content, *, metadata=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("PERSONAL_MEMORY_CONTENT_INVALID")
        if metadata is not None and (not isinstance(metadata, dict) or set(metadata) & {"user_id", "agent_id", "run_id", "scope"}):
            raise ValueError("PERSONAL_MEMORY_METADATA_INVALID")
        with self._operation(scope) as backend:
            with self._sparse_write(backend, content), self._write("create"):
                key = backend.add(content, user_id=scope, metadata=metadata, infer=False)["results"][0]["id"]
                self._metadata.replace_entities(key, content, datetime.now(timezone.utc).isoformat())
            return self._get(backend, scope, key, required=True)

    def update(self, scope, key, content, *, metadata=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("PERSONAL_MEMORY_CONTENT_INVALID")
        if metadata is not None and (not isinstance(metadata, dict) or set(metadata) & {"user_id", "agent_id", "run_id", "scope"}):
            raise ValueError("PERSONAL_MEMORY_METADATA_INVALID")
        with self._operation(scope) as backend:
            old = self._get(backend, scope, key, required=True)
            merged = {**(old.get("metadata") or {}), **(metadata or {})}
            with self._sparse_write(backend, content), self._write("update"):
                self._unlink_entities(backend, scope, key)
                backend.update(key, content, metadata=merged)
                self._metadata.replace_entities(key, content, datetime.now(timezone.utc).isoformat())
            return self._get(backend, scope, key, required=True)

    def delete(self, scope, key):
        with self._operation(scope) as backend:
            self._get(backend, scope, key, required=True)
            with self._write("delete"):
                backend.delete(key)
                self._unlink_entities(backend, scope, key)
                self._metadata.remove(key)

    @staticmethod
    def _unlink_entities(backend, scope, key):
        # The vendored helper skips unopened entity stores and swallows errors.
        # Use the shared client, keep vectors unchanged, and propagate failures.
        from qdrant_client import models
        client = backend.vector_store.client
        collection = backend.collection_name + "_entities"
        if not client.collection_exists(collection):
            return
        query = models.Filter(must=[
            models.FieldCondition(key="user_id", match=models.MatchValue(value=scope)),
            models.FieldCondition(key="linked_memory_ids", match=models.MatchValue(value=key)),
        ])
        offset = None
        while True:
            rows, next_offset = client.scroll(collection, scroll_filter=query, offset=offset,
                                               limit=128, with_payload=True, with_vectors=False)
            for row in rows:
                links = (row.payload or {}).get("linked_memory_ids")
                if not isinstance(links, list):
                    raise ValueError("PERSONAL_ENTITY_LINKS_INVALID")
                remaining = [value for value in links if value != key]
                if remaining:
                    client.set_payload(collection, payload={"linked_memory_ids": remaining}, points=[row.id], wait=True)
                else:
                    client.delete(collection, points_selector=[row.id], wait=True)
            if next_offset is None:
                break
            if not rows or next_offset == offset:
                raise ValueError("PERSONAL_ENTITY_PAGINATION_INVALID")
            offset = next_offset

    def record_access(self, scope, keys, *, when):
        keys = list(dict.fromkeys(keys))
        if not isinstance(when, str) or not when.strip():
            raise ValueError("PERSONAL_MEMORY_ACCESS_TIME_INVALID")
        with self._operation(scope) as backend:
            for key in keys:
                self._get(backend, scope, key, required=True)
            with self._write("access"):
                with self._metadata.access:
                    self._metadata.access.executemany("INSERT OR REPLACE INTO memory_access(memory_id,last_accessed) VALUES(?,?)",
                                                      [(key, when) for key in keys])

    def last_accessed(self, scope, keys):
        with self._operation(scope) as backend:
            result = {}
            for key in dict.fromkeys(keys):
                if self._get(backend, scope, key) is not None:
                    row = self._metadata.access.execute("SELECT last_accessed FROM memory_access WHERE memory_id=?", (key,)).fetchone()
                    if row:
                        result[key] = row[0]
            return result

    def lookup_entities(self, scope, entities, *, limit=20):
        names = sorted(expand_entity_aliases(entities))
        with self._operation(scope) as backend:
            if not names or limit <= 0:
                return []
            placeholders = ",".join("?" for _ in names)
            rows = self._metadata.entities.execute(
                f"SELECT memory_id,MAX(updated_at) FROM entity_memory WHERE entity IN ({placeholders}) GROUP BY memory_id ORDER BY MAX(updated_at) DESC",
                names)
            result = []
            for key, _ in rows:
                if self._get(backend, scope, key) is not None:
                    result.append(key)
                    if len(result) >= limit:
                        break
            return result

    def audit_references(self, source_entries, *, cancel_event=None):
        with self._lock:
            with self._session.operation() as backend:
                _check_pending(self._root)
                return audit_references(backend, self._metadata, source_entries, cancel_event=cancel_event)

    def audit_timeline(self, timeline_path, *, cancel_event=None):
        path = _owned(self._root.parent, Path(timeline_path))
        with self._lock:
            with self._session.operation() as backend:
                _check_pending(self._root)
                sources = read_timeline_source_entries(path, cancel_event=cancel_event)
                return audit_references(backend, self._metadata, sources, cancel_event=cancel_event)

    def close(self):
        with self._lock:
            try:
                self._metadata.close()
            finally:
                self._session.close()


def open_personal_memory(memory_dir, *, identity, encoder, write_rehearsal=False):
    if write_rehearsal:
        _require_write_rehearsal(Path(memory_dir).absolute())
    root = Path(memory_dir).resolve()
    _check_pending(root)
    paths = _metadata_paths(root)
    session = open_personal_backend(root, identity=identity, encoder=encoder)
    try:
        metadata = _Metadata(paths)
    except BaseException:
        session.close()
        raise
    return PersonalMemoryRecords(root, session, metadata,
                                 recall_only="legacy_marker" in identity and not write_rehearsal)


def open_personal_memory_from_snapshot(memory_dir, *, snapshot, write_rehearsal=False):
    """Open an exclusive work copy with a local encoder and layout admission.

    No model discovery, download, index rebuild, or default plugin activation.
    Generation roots verify their stored artifact binding; legacy roots compare
    a bounded sample of existing dense vectors and default to recall-only.
    Explicit write_rehearsal additionally requires a path-bound disposable copy.
    """
    if write_rehearsal:
        _require_write_rehearsal(Path(memory_dir).absolute())
    root = Path(memory_dir).resolve()
    _check_pending(root)
    _metadata_paths(root)
    try:
        identity = (_read_object(_owned(root, root / "active_index.json"))["encoding_identity"]
                    if os.path.lexists(root / "active_index.json") else legacy_identity(root))
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError("PERSONAL_INDEX_INVALID") from exc
    _index_config(root, identity)
    encoder = LocalPersonalEncoder(snapshot, identity)
    try:
        return open_personal_memory(root, identity=identity, encoder=encoder, write_rehearsal=write_rehearsal)
    except BaseException:
        encoder.close()
        raise
