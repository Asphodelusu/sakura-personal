"""Internal scoped MemoryCurator adapter; does not enable personal trial writes."""
if __package__:
    from .memory import (_memory_metadata, _normalize_memory_record, _required_text,
                         _normalize_memory_layer, _is_core_profile_id,
                         MEMORY_LAYER_CORE_PROFILE, VECTOR_MEMORY_LAYERS, looks_like_sensitive_memory)
    from .personal_records import _scope
else:
    from memory import (_memory_metadata, _normalize_memory_record, _required_text,
                        _normalize_memory_layer, _is_core_profile_id,
                        MEMORY_LAYER_CORE_PROFILE, VECTOR_MEMORY_LAYERS, looks_like_sensitive_memory)
    from personal_records import _scope


class PersonalCurationStore:
    curation_layers = VECTOR_MEMORY_LAYERS

    def __init__(self, records, scope, *, profile_candidates=False):
        self._records = records
        self.scope_id = _scope(scope)
        self.allow_profile_candidates = bool(profile_candidates)

    def _normalize(self, raw):
        return _normalize_memory_record(raw, default_scope=self.scope_id)

    def list_memories(self, *, limit=None):
        return [self._normalize(row) for row in self._records.list(self.scope_id, limit=limit)]

    def _validate(self, arguments, *, allow_sensitive=False, content=True):
        if set(arguments) & {"user_id", "agent_id", "run_id", "scope"}:
            raise ValueError("PERSONAL_MEMORY_METADATA_INVALID")
        if (_normalize_memory_layer(arguments.get("layer")) == MEMORY_LAYER_CORE_PROFILE
                or _is_core_profile_id(str(arguments.get("id") or ""))):
            raise ValueError("PERSONAL_CORE_PROFILE_UNSUPPORTED")
        if content:
            value = _required_text(arguments, "content")
            if not allow_sensitive and looks_like_sensitive_memory(value):
                raise ValueError("PERSONAL_SENSITIVE_MEMORY_REJECTED")
            return value

    def _metadata(self, arguments, existing=None):
        metadata = _memory_metadata(arguments, scope_id=self.scope_id, existing=existing)
        # Scope is owned by this adapter, not by model-generated metadata.
        metadata.pop("scope", None)
        return metadata

    def create_memory(self, arguments, *, allow_sensitive=False):
        content = self._validate(arguments, allow_sensitive=allow_sensitive)
        row = self._records.create(self.scope_id, content, metadata=self._metadata(arguments))
        return {"memory": self._normalize(row), "ok": True}

    def update_memory(self, arguments, *, allow_sensitive=False):
        content = self._validate(arguments, allow_sensitive=allow_sensitive)
        key = _required_text(arguments, "id")
        # Share the records lock across read/merge/write, so concurrent updates
        # cannot lose provenance. Records operations use the same reentrant lock.
        with self._records._lock:
            old = self._records.get(self.scope_id, key)
            if old is None:
                raise ValueError("PERSONAL_MEMORY_SCOPE_OR_ID_INVALID")
            if self._normalize(old)["layer"] == MEMORY_LAYER_CORE_PROFILE:
                raise ValueError("PERSONAL_CORE_PROFILE_UNSUPPORTED")
            row = self._records.update(self.scope_id, key, content,
                                       metadata=self._metadata(arguments, old))
        return {"memory": self._normalize(row), "ok": True}

    def delete_memory(self, arguments):
        self._validate(arguments, content=False)
        key = _required_text(arguments, "id")
        with self._records._lock:
            old = self._records.get(self.scope_id, key)
            if old is None:
                raise ValueError("PERSONAL_MEMORY_SCOPE_OR_ID_INVALID")
            if self._normalize(old)["layer"] == MEMORY_LAYER_CORE_PROFILE:
                raise ValueError("PERSONAL_CORE_PROFILE_UNSUPPORTED")
            self._records.delete(self.scope_id, key)
        return {"memory": self._normalize(old), "ok": True}
