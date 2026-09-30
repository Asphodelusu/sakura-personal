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
    from .personal_core_profile import read_personal_core_profile
else:
    from boundary import MemoryBoundary, _project_memory
    from personal_curation import PersonalCurationStore
    from support import log_event
    from memory import MEMORY_LAYERS
    import personal_records
    from index_contract import COPY_STATE_FILE, require_complete_copy
    from personal_core_profile import read_personal_core_profile


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
    def __init__(self, memory_dir, character_id, snapshot, *, curation_options=None, daily=False):
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

    def close(self):
        with self._lock:
            self._closed = True
            self._status = "stopped"
            self._pending_timeline = None
        self._thread.join()
        # Do not hold the recall lock while joining the curation worker.
        if self._curation is not None:
            self._curation.close()
        with self._lock:
            records, self._records = self._records, None
            if records is not None and self._curation is None:
                records.close()
