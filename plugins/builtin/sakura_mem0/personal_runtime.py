"""Personal recall and explicitly admitted rehearsal or daily curation."""
import threading
import stat
from pathlib import Path

if __package__:
    from .boundary import MemoryBoundary, _project_memory
    from .personal_curation import PersonalCurationStore
    from .support import log_event
    from .memory import MEMORY_LAYERS
    from . import personal_records
    from .index_contract import COPY_STATE_FILE, require_complete_copy
    from .personal_core_profile import (
        CORE_PROFILE_FORMAL_SECTIONS,
        CORE_PROFILE_LEGACY_HEADING,
        CORE_PROFILE_LEGACY_SECTION,
        edit_personal_core_profile_section,
        load_personal_core_profile_record,
        read_personal_core_profile,
    )
    from .personal_emotion import DEFAULT_EMOTION, EmotionScorer
    from .personal_recall import CrossEncoderReranker, PersonalRecallPolicy
    from .personal_mood import (
        PersonalEmotionStore,
        PersonalMoodStore,
        build_mood_fragment,
        build_user_emotion_fragment,
    )
else:
    from boundary import MemoryBoundary, _project_memory
    from personal_curation import PersonalCurationStore
    from support import log_event
    from memory import MEMORY_LAYERS
    import personal_records
    from index_contract import COPY_STATE_FILE, require_complete_copy
    from personal_core_profile import (
        CORE_PROFILE_FORMAL_SECTIONS,
        CORE_PROFILE_LEGACY_HEADING,
        CORE_PROFILE_LEGACY_SECTION,
        edit_personal_core_profile_section,
        load_personal_core_profile_record,
        read_personal_core_profile,
    )
    from personal_emotion import DEFAULT_EMOTION, EmotionScorer
    from personal_recall import CrossEncoderReranker, PersonalRecallPolicy
    from personal_mood import (
        PersonalEmotionStore,
        PersonalMoodStore,
        build_mood_fragment,
        build_user_emotion_fragment,
    )


CORE_PROFILE_ITEM_PREFIX = "core_profile:"


class _CurationStore(PersonalCurationStore):
    """Already-open records owned by the enclosing personal boundary.

    No model lifecycle is exposed on this internal curation-only surface.
    """
    personal_daily = False

    def add_status_listener(self, listener):
        pass  # Ready before construction; no asynchronous model state remains.

    def remove_status_listener(self, listener):
        pass

    def is_ready(self):
        return True

    def needs_embedding_model_download(self):
        return False

    def scoped(self, scope):
        if scope != self.scope_id:
            raise ValueError("PERSONAL_MEMORY_SCOPE_INVALID")
        return self

    def close(self):
        self._records.close()


class PersonalRecallBoundary:
    def __init__(self, memory_dir, character_id, snapshot, *, curation_options=None, daily=False,
                 reranker_snapshot=None):
        memory_dir = Path(memory_dir).absolute()
        root = memory_dir.parent.parent
        # Plugin workers cannot import Core's migration modules. Check only the
        # owned memory tree and ancestors; other copy domains are not opened here.
        for path in (memory_dir, *memory_dir.parents, *memory_dir.rglob("*")):
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise ValueError("PERSONAL_RUNTIME_LINK_UNSUPPORTED")
        if not (root / COPY_STATE_FILE).is_file():
            raise ValueError("PERSONAL_MIGRATION_COPY_REQUIRED")
        require_complete_copy(root)
        if daily and curation_options is None:
            raise ValueError("PERSONAL_DAILY_CURATION_REQUIRED")
        personal_records._require_write_mode(memory_dir, character_id, daily=daily,
                                             write_rehearsal=curation_options is not None and not daily)
        personal_records._scope(character_id)
        self.scope = character_id
        self._memory_dir = memory_dir
        self._lock = threading.RLock()
        self._records = None
        self._curation = None
        self._curation_options = curation_options
        self._daily = daily
        self._pending_timeline = None
        self._status = "loading"
        self._closed = False
        self.recall_policy = PersonalRecallPolicy(
            memory_dir,
            reranker=CrossEncoderReranker(reranker_snapshot) if reranker_snapshot is not None else None,
            list_memories=lambda: self.list_memories(limit=200),
            record_access=daily,
        )
        self._thread = threading.Thread(target=self._load, args=(memory_dir, snapshot),
                                        name="sakura-personal-memory-load", daemon=True)
        self._thread.start()

    def _load(self, memory_dir, snapshot):
        records = None
        curation = None
        try:
            records = personal_records.open_personal_memory_from_snapshot(memory_dir, snapshot=snapshot,
                write_rehearsal=self._curation_options is not None and not self._daily,
                daily=self._daily, scope=self.scope if self._daily else None)
            if self._curation_options is not None:
                store = _CurationStore(records, self.scope, profile_candidates=True)
                store.personal_daily = self._daily
                if self._daily:
                    store.mood_store = PersonalMoodStore(memory_dir, self.scope, admit=self._admit_state_write)
                    store.emotion_store = PersonalEmotionStore(memory_dir, self.scope)
                curation = MemoryBoundary(memory_dir.parent.parent, self.scope,
                    memory_dir=memory_dir,
                    memory_store=store,
                    **self._curation_options)
            with self._lock:
                if not self._closed:
                    self._records = records
                    self._curation = curation
                    records = None
                    curation = None
                    self._status = "ready"
                    pending, self._pending_timeline = self._pending_timeline, None
                else:
                    pending = None
            log_event("Memory", "个人记忆加载完成", {"curation_enabled": self._curation_options is not None},
                      event="memory.personal.loaded", severity="info")
            if pending is not None:
                self.note_timeline_changed(pending)
        except Exception as exc:
            log_event("Memory", "个人记忆加载失败", {"error_type": type(exc).__name__},
                      event="memory.personal.load_failed", severity="warning")
            with self._lock:
                if not self._closed:
                    self._status = "degraded"
        finally:
            if curation is not None:
                curation.close()
            elif records is not None:
                records.close()

    def note_timeline_changed(self, timeline):
        with self._lock:
            log_event("Memory", "个人记忆收到历史通知", {"status": self._status},
                      event="memory.personal.timeline_changed", severity="debug")
            if self._closed or self._curation_options is None:
                return
            if self._status == "loading":
                self._pending_timeline = timeline
                return
            curation = self._curation if self._status == "ready" else None
        if curation is not None:
            curation.note_timeline_changed(timeline)

    def search_memory(self, arguments, *, wait=False):
        if set(arguments) - {"query", "limit", "layer"}:
            raise ValueError("PERSONAL_MEMORY_QUERY_INVALID")
        query, limit = arguments.get("query"), arguments.get("limit", 10)
        layer = arguments.get("layer")
        if (not isinstance(query, str) or not query.strip() or len(query) > 4000
                or type(limit) is not int or not 1 <= limit <= 100
                or layer is not None and layer not in MEMORY_LAYERS):
            raise ValueError("PERSONAL_MEMORY_QUERY_INVALID")
        with self._lock:
            if self._status != "ready":
                return {"status": self._status, "memories": []}
            try:
                result = self._records.search(self.scope, query, limit=limit)
                memories = []
                for raw in result["results"]:
                    if raw.get("user_id") != self.scope:
                        raise ValueError("PERSONAL_MEMORY_SCOPE_INVALID")
                    item = _project_memory(raw, self.scope)
                    if item and (layer is None or item["layer"] == layer):
                        memories.append(item)
                return {"status": "ready", "memories": memories}
            except Exception:
                self._status = "degraded"
                return {"status": "degraded", "memories": []}

    _READ_TOOLS = frozenset({"search", "detail", "timeline"})
    _WRITE_TOOLS = frozenset({"remember", "update", "forget", "let_go"})

    def memory_tool(self, name, arguments):
        if name not in self._READ_TOOLS | self._WRITE_TOOLS:
            raise ValueError("PERSONAL_MEMORY_TOOL_UNKNOWN")
        with self._lock:
            if self._status != "ready" or self._records is None:
                if name in self._WRITE_TOOLS:
                    return {"status": self._status, "ok": False}
                return {"status": self._status, "memories": []}
            if name in self._WRITE_TOOLS and not self._daily:
                raise ValueError("PERSONAL_MEMORY_READ_ONLY")
            if __package__:
                from .personal_tools import PersonalMemoryTools
            else:
                from personal_tools import PersonalMemoryTools
            return getattr(PersonalMemoryTools(self._records, self.scope), name)(dict(arguments))

    # --- memory management collection (daily entry only) --------------------

    def status(self):
        with self._lock:
            return {"status": self._status, "message": ""}

    def list_memories(self, *, limit=None):
        with self._lock:
            if self._status != "ready" or self._records is None:
                return []
            return self._records.list(self.scope, limit=limit)

    def _management_store(self):
        if self._status != "ready" or self._records is None:
            raise RuntimeError("PERSONAL_MEMORY_NOT_READY")
        if not self._daily:
            raise ValueError("PERSONAL_MEMORY_READ_ONLY")
        return PersonalCurationStore(self._records, self.scope)

    def upsert(self, values):
        with self._lock:
            store = self._management_store()
            arguments = {key: value for key, value in dict(values).items()
                         if key in {"id", "content", "layer", "category", "source", "importance", "confidence"}}
            saved = (store.update_memory(arguments) if arguments.get("id")
                     else store.create_memory({key: value for key, value in arguments.items() if key != "id"}))
            raw = self._records.get(self.scope, saved["memory"]["id"])
            return {"memory": _project_memory(raw, self.scope)}

    def delete(self, values):
        with self._lock:
            store = self._management_store()
            key = str(dict(values).get("id") or "").strip()
            if not key or self._records.get(self.scope, key) is None:
                return {"alreadyMissing": True}
            store.delete_memory({"id": key})
            return {"alreadyMissing": False}

    def core_profile_fragment(self):
        with self._lock:
            if self._closed:
                return None
            memory_dir, scope = self._memory_dir, self.scope
        try:
            fragment = read_personal_core_profile(memory_dir, scope)
        except Exception:
            log_event("Memory", "个人常驻档案不可读", {"code": "CORE_PROFILE_UNREADABLE"},
                      event="memory.personal.core_profile_unreadable", severity="warning")
            return None
        with self._lock:
            if self._closed:
                return None
        return fragment

    def core_profile_items(self):
        """Archive sections as management items; the verbatim V1 archive is one "legacy" item."""
        with self._lock:
            if self._closed:
                return []
            memory_dir, scope = self._memory_dir, self.scope
        record = load_personal_core_profile_record(memory_dir, scope)
        if not isinstance(record, dict):
            return []
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        sections = record.get("sections")
        if not isinstance(sections, dict) or not sections:
            text = str(record.get("content") or record.get("memory") or "").strip()
            sections = {CORE_PROFILE_LEGACY_SECTION: text} if text else {}
        items = []
        for name in (*CORE_PROFILE_FORMAL_SECTIONS, CORE_PROFILE_LEGACY_SECTION):
            text = str(sections.get(name) or "").strip()
            if not text:
                continue
            items.append({
                "id": f"{CORE_PROFILE_ITEM_PREFIX}{scope}#{name}",
                "content": text,
                "layer": "core_profile",
                "category": CORE_PROFILE_LEGACY_HEADING if name == CORE_PROFILE_LEGACY_SECTION else name,
                "source": str(metadata.get("source") or ""),
                "importance": 1.0,
                "confidence": 1.0,
                "updatedAt": str(metadata.get("updated_at") or ""),
            })
        return items

    def edit_core_profile_item(self, item_id, content):
        with self._lock:
            memory_dir, scope = self._memory_dir, self.scope
        prefix = f"{CORE_PROFILE_ITEM_PREFIX}{scope}#"
        if not isinstance(item_id, str) or not item_id.startswith(prefix):
            raise ValueError("MEMORY_NOT_FOUND")
        if not self._daily:
            raise ValueError("PERSONAL_MEMORY_READ_ONLY")
        edit_personal_core_profile_section(memory_dir, scope, item_id[len(prefix):], str(content or ""), daily=True)
        return next((item for item in self.core_profile_items() if item["id"] == item_id), None)

    def _admit_state_write(self):
        personal_records._require_write_mode(self._memory_dir, self.scope, daily=True, write_rehearsal=False)

    def continuity_fragments(self):
        """Sakura's mood and the user's emotion trajectory; read-only, no memory search."""
        with self._lock:
            if self._closed:
                return []
            memory_dir, scope = self._memory_dir, self.scope
        try:
            mood = PersonalMoodStore(memory_dir, scope).current()
            emotion = PersonalEmotionStore(memory_dir, scope).current()
        except Exception as exc:
            log_event("Memory", "心情状态不可读", {"code": "PERSONAL_STATE_UNREADABLE", "error_type": type(exc).__name__},
                      event="memory.personal.state_unreadable", severity="warning")
            return []
        fragments = (build_mood_fragment(scope, mood), build_user_emotion_fragment(scope, emotion))
        return [fragment for fragment in fragments if fragment is not None]

    def note_user_input(self, text):
        """Daily entry only: record the user's emotion when the scorer is confident."""
        with self._lock:
            if self._closed or not self._daily:
                return
            memory_dir, scope = self._memory_dir, self.scope
        emotion = EmotionScorer().best(text)
        if emotion is None or emotion == DEFAULT_EMOTION:
            return
        try:
            self._admit_state_write()
            PersonalEmotionStore(memory_dir, scope).record(emotion)
        except Exception as exc:
            log_event("Memory", "用户情绪未记录", {"code": "PERSONAL_EMOTION_WRITE_FAILED", "error_type": type(exc).__name__},
                      event="memory.personal.emotion_write_failed", severity="info")

    def close(self):
        with self._lock:
            self._closed = True
            self._status = "stopped"
            self._pending_timeline = None
        self._thread.join()
        self.recall_policy.close()
        # Do not hold the recall lock while joining the curation worker.
        if self._curation is not None:
            self._curation.close()
        with self._lock:
            records, self._records = self._records, None
            if records is not None and self._curation is None:
                records.close()
