"""Internal personal-index admission for isolated compatibility rehearsal.

Used by the opt-in personal plugin on an exclusive work copy. Generation layouts
require their recorded encoding binding; legacy roots require an explicit model
marker and bounded vector compatibility samples. Neither path relabels an index.
"""
from contextlib import closing, contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace
if __package__:
    from .index_contract import require_complete_copy
else:
    from index_contract import require_complete_copy


def _read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("PERSONAL_INDEX_INVALID")
    return value


def _owned(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("PERSONAL_INDEX_PATH_ESCAPE")
    return resolved


def _sqlite_uri(path):
    # Tauri canonicalizes local Windows paths with a verbatim prefix. It is a
    # filesystem spelling, not a URI authority; SQLite rejects file://%3F/...
    text = str(path)
    if os.name == "nt" and re.match(r"^\\\\\?\\[A-Za-z]:\\", text):
        text = text[4:]
    return Path(text).as_uri()


def _check_sqlite(path, *, required_table=None):
    # Run only on the caller's exclusive work copy. mode=ro prevents creation
    # or repair of a missing/invalid database; do not ignore pending WAL data.
    with closing(sqlite3.connect(_sqlite_uri(path) + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("PERSONAL_INDEX_SQLITE_INVALID")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables or (required_table is not None and required_table not in tables):
            raise ValueError("PERSONAL_INDEX_SQLITE_SCHEMA_MISSING")


PROFILES = {"BAAI/bge-m3": (1024, 512), "sentence-transformers/all-MiniLM-L6-v2": (384, 256)}


def legacy_identity(root):
    """Read the old explicit model marker, without inventing a revision binding."""
    root = Path(root)
    if os.path.lexists(root / "active_index.json"):
        raise ValueError("PERSONAL_INDEX_ACTIVE_POINTER_PRESENT")
    try:
        marker = _owned(root, root / "embedding_version.txt").read_text(encoding="utf-8").strip()
        for model, (dimensions, length) in PROFILES.items():
            if marker == f"{model}:{dimensions}":
                return {"model_id": model, "dimensions": dimensions,
                        "max_seq_length": length, "legacy_marker": marker}
    except OSError as exc:
        raise ValueError("PERSONAL_INDEX_LEGACY_MARKER_INVALID") from exc
    raise ValueError("PERSONAL_INDEX_LEGACY_MARKER_INVALID")


def _index_config(memory_dir, identity):
    root = Path(memory_dir)
    require_complete_copy(root)
    if os.path.lexists(root / "personal_write_pending.json"):
        raise RuntimeError("PERSONAL_MEMORY_WRITE_INCOMPLETE")
    try:
        profile = PROFILES.get(identity.get("model_id"))
        if profile is None:
            raise ValueError("PERSONAL_INDEX_MODEL_UNSUPPORTED")
        dimensions, length = profile
        legacy = "legacy_marker" in identity
        if legacy:
            if identity != legacy_identity(root):
                raise ValueError("PERSONAL_INDEX_ENCODING_MISMATCH")
            qdrant = _owned(root, root / "qdrant")
            journal = None
        else:
            binding = _read_object(_owned(root, root / "active_index.json"))
            index_id = binding["index_id"]
            if not isinstance(index_id, str) or re.fullmatch(r"[0-9a-f]{32}", index_id) is None:
                raise ValueError("PERSONAL_INDEX_ID_INVALID")
            qdrant = _owned(root, root / "indexes" / index_id / "qdrant")
            journal = _read_object(_owned(root, qdrant.parent / "journal.json"))
        artifact = identity.get("artifact_sha256")
        expected = {
            "schema": 1, "model_id": identity["model_id"], "artifact_sha256": artifact,
            "dimensions": dimensions, "max_seq_length": length,
            "encoder": "mem0-huggingface-sentence-transformers-encode-v1",
            "document_encoding": "encode-default", "query_encoding": "encode-default",
            "normalize_embeddings": False,
        }
        if not legacy and (not isinstance(artifact, str) or re.fullmatch(r"[0-9a-f]{64}", artifact) is None
                or identity != expected or binding["encoding_identity"] != expected
                or type(identity.get("schema")) is not int
                or type(identity.get("normalize_embeddings")) is not bool):
            raise ValueError("PERSONAL_INDEX_ENCODING_MISMATCH")
        if not legacy and (journal["state"] != "validated" or journal["encoding_identity"] != expected):
            raise ValueError("PERSONAL_INDEX_NOT_VALIDATED")
        collections = _read_object(_owned(root, qdrant / "meta.json"))["collections"]
        allowed = {"sakura_memories", "sakura_memories_entities"}
        if (not isinstance(collections, dict) or "sakura_memories" not in collections
                or set(collections) - allowed):
            raise ValueError("PERSONAL_INDEX_COLLECTIONS_INVALID")
        if not legacy and (not isinstance(journal["counts"], dict)
                           or not set(journal["counts"]).issubset(collections)):
            raise ValueError("PERSONAL_INDEX_COLLECTIONS_INVALID")
        for name, config in collections.items():
            storage = _owned(root, qdrant / "collection" / name / "storage.sqlite")
            if not storage.is_file() or config["vectors"]["size"] != dimensions:
                raise ValueError("PERSONAL_INDEX_STORAGE_INVALID")
            _check_sqlite(storage)
        history = _owned(root, root / "mem0_history.db")
        if not history.is_file():
            raise ValueError("PERSONAL_INDEX_HISTORY_MISSING")
        _check_sqlite(history, required_table="history")
    except (OSError, KeyError, TypeError, AttributeError, ValueError, sqlite3.Error) as exc:
        raise ValueError("PERSONAL_INDEX_INVALID: 未打开或重建记忆索引。") from exc
    return {
        "vector_store": {"provider": "qdrant", "config": {
            "path": qdrant.as_posix(), "collection_name": "sakura_memories",
            "embedding_model_dims": dimensions, "on_disk": True}},
        "embedder": {"provider": "huggingface", "config": {
            "model": identity["model_id"], "embedding_dims": dimensions}},
        "history_db_path": str(history),
    }


class _CheckedEncoder:
    def __init__(self, encoder, dimensions):
        self.encoder = encoder
        self.dimensions = dimensions

    def embed(self, *args, **kwargs):
        vector = self.encoder.embed(*args, **kwargs)
        if len(vector) != self.dimensions or any(
            isinstance(value, bool) or not math.isfinite(float(value)) for value in vector
        ):
            raise ValueError("PERSONAL_INDEX_VECTOR_INVALID")
        return vector

    def close(self):
        close = getattr(self.encoder, "close", None)
        if callable(close):
            close()


class PersonalBackendSession:
    """Lease includes caller's associated metadata writes, not only mem0 calls."""
    def __init__(self, backend):
        self._backend = backend
        self._condition = threading.Condition()
        self._local = threading.local()
        self._active = 0
        self._closed = False
        self.closing = threading.Event()

    @contextmanager
    def operation(self):
        with self._condition:
            depth = getattr(self._local, "depth", 0)
            if self.closing.is_set() and not depth:
                raise RuntimeError("PERSONAL_BACKEND_CLOSED")
            self._active += 1
            self._local.depth = depth + 1
        try:
            yield self._backend
        finally:
            with self._condition:
                self._local.depth -= 1
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        with self._condition:
            if getattr(self._local, "depth", 0):
                raise RuntimeError("PERSONAL_BACKEND_ACTIVE_OPERATION")
            if self.closing.is_set():
                self._condition.wait_for(lambda: self._closed)
                return
            self.closing.set()
            self._condition.wait_for(lambda: not self._active)
        try:
            try:
                self._backend.close()
            finally:
                try:
                    self._backend.vector_store.client.close()
                finally:
                    self._backend.embedding_model.close()
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()


def open_personal_backend(memory_dir, *, identity, encoder):
    """Own encoder after admission; rejected layout leaves it with the caller.

Identity is a caller assertion, not proof that an arbitrary injected encoder is
the real model. Use the records snapshot entry for artifact-verified local loading;
default plugin activation and actual model runtime acceptance remain separate gates.
"""
    config = _index_config(memory_dir, identity)
    checked = _CheckedEncoder(encoder, config["vector_store"]["config"]["embedding_model_dims"])
    try:
        checked.embed("sakura-personal-compatibility-check", "search")
        if __package__:
            from .memory import _create_raw_memory_backend, _import_mem0_dependencies
        else:
            from memory import _create_raw_memory_backend, _import_mem0_dependencies
        memory_type, vendor, _, vector_factory = _import_mem0_dependencies()
    except BaseException:
        checked.close()
        raise
    backend = _create_raw_memory_backend(memory_type, vendor,
        SimpleNamespace(create=lambda *args: checked), vector_factory, config)
    session = PersonalBackendSession(backend)
    if "legacy_marker" in identity:
        try:
            _verify_legacy_vectors(backend, checked)
        except BaseException:
            session.close()
            raise
    return session


def _verify_legacy_vectors(backend, encoder):
    """Reject incompatible legacy encoders using a bounded sample, not a binding.

    Legacy roots never recorded an artifact revision. This check does not claim
    to validate every vector or replace the recorded model/dimension checks.
    """
    rows, _ = backend.vector_store.client.scroll(
        backend.collection_name, limit=3, with_vectors=True, with_payload=True)
    if not rows:
        raise ValueError("PERSONAL_LEGACY_VECTOR_PROBE_UNAVAILABLE")
    for row in rows:
        content = (row.payload or {}).get("data")
        stored = row.vector.get("") if isinstance(row.vector, dict) else row.vector
        if not isinstance(content, str) or not content.strip() or not isinstance(stored, list):
            raise ValueError("PERSONAL_LEGACY_VECTOR_MISMATCH")
        actual = encoder.embed(content, "search")
        if len(stored) != len(actual) or any(not math.isfinite(float(x)) for x in stored):
            raise ValueError("PERSONAL_LEGACY_VECTOR_MISMATCH")
        norm = math.sqrt(sum(x * x for x in actual) * sum(x * x for x in stored))
        cosine = sum(x * y for x, y in zip(actual, stored)) / norm if norm else 0.0
        if not math.isfinite(cosine) or cosine < 0.999:
            raise ValueError("PERSONAL_LEGACY_VECTOR_MISMATCH")
