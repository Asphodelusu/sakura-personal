"""Explicit memory tools for the personal plugin; writes reuse the curation store guards."""
from __future__ import annotations

if __package__:
    from .boundary import _project_memory
    from .memory import MEMORY_LAYER_CORE_PROFILE, _is_core_profile_id, _normalize_memory_layer
    from .memory_recall import _is_released
    from .personal_curation import PersonalCurationStore
else:
    from boundary import _project_memory
    from memory import MEMORY_LAYER_CORE_PROFILE, _is_core_profile_id, _normalize_memory_layer
    from memory_recall import _is_released
    from personal_curation import PersonalCurationStore

_WRITE_FIELDS = {"content", "layer", "category", "importance", "confidence"}
_TITLE_CHARS = 44
_MAX_DETAIL_IDS = 10
_MAX_NEIGHBOURS = 5


def _allowed(arguments, allowed):
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ValueError("PERSONAL_MEMORY_ARGUMENT_INVALID")
    return arguments


def _memory_id(arguments):
    value = str(arguments.get("memory_id") or "").strip()
    if not value:
        raise ValueError("PERSONAL_MEMORY_ID_REQUIRED")
    return value


def _bounded(value, default, low, high):
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise ValueError("PERSONAL_MEMORY_ARGUMENT_INVALID")
    return value


class PersonalMemoryTools:
    def __init__(self, records, scope):
        self._records = records
        self._scope = scope
        self._store = PersonalCurationStore(records, scope)

    def _project(self, raw):
        item = _project_memory(raw, self._scope) if raw is not None else None
        if item is not None and raw.get("user_id") not in (None, self._scope):
            return None
        return item

    def _index(self, item):
        content = str(item.get("content") or "")
        return {
            "id": item["id"],
            "title": content[:_TITLE_CHARS],
            "layer": item.get("layer", ""),
            "created_at": item.get("createdAt", ""),
            "importance": item.get("importance"),
            "approx_tokens": max(1, len(content) // 2),
        }

    def search(self, arguments):
        _allowed(arguments, {"query", "limit", "layer", "mode", "include_released"})
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("PERSONAL_MEMORY_QUERY_INVALID")
        limit = _bounded(arguments.get("limit"), 10, 1, 100)
        mode = arguments.get("mode", "full")
        if mode not in {"full", "index"}:
            raise ValueError("PERSONAL_MEMORY_ARGUMENT_INVALID")
        layer = arguments.get("layer")
        include_released = arguments.get("include_released") is True
        memories = []
        for raw in self._records.search(self._scope, query, limit=limit)["results"]:
            item = self._project(raw)
            if item is None or (layer is not None and item["layer"] != layer):
                continue
            if not include_released and _is_released(item, {}):
                continue
            memories.append(self._index(item) if mode == "index" else item)
        return {"status": "ready", "memories": memories}

    def detail(self, arguments):
        _allowed(arguments, {"ids"})
        raw_ids = arguments.get("ids")
        ids = raw_ids.split(",") if isinstance(raw_ids, str) else raw_ids
        if not isinstance(ids, list):
            raise ValueError("PERSONAL_MEMORY_ARGUMENT_INVALID")
        ids = [str(value).strip() for value in ids if str(value).strip()][:_MAX_DETAIL_IDS]
        memories, missing = [], []
        for key in dict.fromkeys(ids):
            item = self._project(self._records.get(self._scope, key))
            (memories.append(item) if item is not None else missing.append(key))
        return {"status": "ready", "memories": memories, "missing": missing}

    def timeline(self, arguments):
        _allowed(arguments, {"memory_id", "before", "after"})
        anchor = _memory_id(arguments)
        if _is_core_profile_id(anchor):
            raise ValueError("PERSONAL_CORE_PROFILE_UNSUPPORTED")
        before = _bounded(arguments.get("before"), 2, 0, _MAX_NEIGHBOURS)
        after = _bounded(arguments.get("after"), 2, 0, _MAX_NEIGHBOURS)
        items = [item for item in (self._project(raw) for raw in self._records.list(self._scope)) if item is not None]
        ordered = sorted(
            (item for item in items if item["id"] == anchor or not _is_released(item, {})),
            key=lambda item: (item.get("createdAt") or "", item["id"]),
        )
        position = next((index for index, item in enumerate(ordered) if item["id"] == anchor), None)
        if position is None:
            raise ValueError("PERSONAL_MEMORY_SCOPE_OR_ID_INVALID")
        window = ordered[max(0, position - before): position + after + 1]
        return {"status": "ready", "anchor": anchor, "memories": window}

    def _write_arguments(self, arguments):
        values = {key: value for key, value in arguments.items() if key in _WRITE_FIELDS}
        return {**values, "source": "explicit"}

    def remember(self, arguments):
        _allowed(arguments, _WRITE_FIELDS)
        return self._store.create_memory(self._write_arguments(arguments))

    def update(self, arguments):
        _allowed(arguments, _WRITE_FIELDS | {"memory_id"})
        return self._store.update_memory({**self._write_arguments(arguments), "id": _memory_id(arguments)})

    def forget(self, arguments):
        _allowed(arguments, {"memory_id"})
        return self._store.delete_memory({"id": _memory_id(arguments)})

    def let_go(self, arguments):
        _allowed(arguments, {"memory_id"})
        key = _memory_id(arguments)
        if _is_core_profile_id(key):
            raise ValueError("PERSONAL_CORE_PROFILE_UNSUPPORTED")
        with self._records._lock:
            raw = self._records.get(self._scope, key)
            if raw is None:
                raise ValueError("PERSONAL_MEMORY_SCOPE_OR_ID_INVALID")
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            if _normalize_memory_layer(metadata.get("layer")) == MEMORY_LAYER_CORE_PROFILE:
                raise ValueError("PERSONAL_CORE_PROFILE_UNSUPPORTED")
            content = str(raw.get("memory") or raw.get("content") or "")
            row = self._records.update(self._scope, key, content, metadata={"status": "released"})
        return {"memory": self._project(row), "ok": True}
