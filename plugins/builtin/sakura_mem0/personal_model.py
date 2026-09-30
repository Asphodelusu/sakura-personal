"""Offline SentenceTransformer loading with the personal Qt artifact contract."""
import hashlib
import json
from pathlib import Path


def quiet_model_loading():
    """Weight-loading progress bars reach the host as plugin stderr warnings."""
    try:
        from transformers.utils import logging as transformers_logging

        transformers_logging.disable_progress_bar()
    except Exception:
        pass


def fingerprint_snapshot(snapshot):
    # Preserve app/config/memory_models.py's path/content framing exactly.
    # HF cache file symlinks identify blobs by their bytes, not their link names.
    snapshot = Path(snapshot)
    files = sorted(path for path in snapshot.rglob("*") if path.is_file())
    if not files:
        raise ValueError("PERSONAL_MODEL_SNAPSHOT_MISSING")
    digest = hashlib.sha256()
    for path in files:
        before = path.stat()
        content = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                content.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("PERSONAL_MODEL_SNAPSHOT_CHANGED")
        digest.update(json.dumps([path.relative_to(snapshot).as_posix(), content.hexdigest()],
                                 ensure_ascii=False).encode("utf-8"))
    if files != sorted(path for path in snapshot.rglob("*") if path.is_file()):
        raise ValueError("PERSONAL_MODEL_SNAPSHOT_CHANGED")
    return digest.hexdigest()


class LocalPersonalEncoder:
    def __init__(self, snapshot, identity):
        self.model = None
        snapshot = Path(snapshot).resolve()
        expected = identity.get("artifact_sha256")
        # Old roots recorded model name/dimension only. Do not manufacture a
        # historical artifact fingerprint from today's snapshot.
        if expected is None and identity.get("legacy_marker") != f'{identity["model_id"]}:{identity["dimensions"]}':
            raise ValueError("PERSONAL_MODEL_IDENTITY_INVALID")
        if not snapshot.is_dir() or (expected is not None and fingerprint_snapshot(snapshot) != expected):
            raise ValueError("PERSONAL_MODEL_ARTIFACT_MISMATCH")
        quiet_model_loading()
        from sentence_transformers import SentenceTransformer
        try:
            self.model = SentenceTransformer(str(snapshot), local_files_only=True, trust_remote_code=False)
            self.model.max_seq_length = identity["max_seq_length"]
            dimension = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
            if dimension() != identity["dimensions"]:
                raise ValueError("PERSONAL_MODEL_DIMENSIONS_MISMATCH")
            if expected is not None and fingerprint_snapshot(snapshot) != expected:
                raise ValueError("PERSONAL_MODEL_SNAPSHOT_CHANGED")
        except BaseException:
            self.close()
            raise

    def embed(self, text, memory_action=None):
        if self.model is None:
            raise RuntimeError("PERSONAL_MODEL_CLOSED")
        # Match the original Qt mem0 adapter: no query prefix or normalization.
        return self.model.encode(text, convert_to_numpy=True).tolist()

    def close(self):
        self.model = None
