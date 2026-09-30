"""Qt-era recall quality for the personal memory: rerank, decay, due commitments.

Reranking only uses a local snapshot; without one the relevance gate falls back
to the semantic score, never to the hybrid score (BM25 halves semantic-only hits).
"""
import math
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic

if __package__:
    from .personal_emotion import active_emotion, emotion_congruence_factor
    from .personal_model import quiet_model_loading
    from .personal_query_rewrite import plan_query
    from .support import log_event
else:
    from personal_emotion import active_emotion, emotion_congruence_factor
    from personal_model import quiet_model_loading
    from personal_query_rewrite import plan_query
    from support import log_event

RECALL_CANDIDATES = 30
RELEVANCE_THRESHOLD = 0.3
# CrossEncoder already applies its sigmoid; the Qt build applied a second one, which
# squeezed every score into (0.5, 0.73) and disabled the gate. This only drops clear misses.
RERANK_THRESHOLD = 0.1
DECAY_LAMBDA = 0.1
EXPLICIT_IMPORTANCE = 0.95
MAX_DUE_COMMITMENTS = 2
DUE_COMMITMENT_SCORE = 0.72
DUE_COMMITMENT_DECAY_BOOST = 1.28
COLD_ARCHIVE_IDLE_DAYS = 45
COLD_ARCHIVE_IMPORTANCE_MAX = 0.38
COLD_ARCHIVE_DECAY_FLOOR = 0.52
EXEMPT_COLD_ARCHIVE_KINDS = frozenset({"commitment", "emotional_turn", "core_profile"})
# "感想不能顶事实": reflections are hard-scaled, independent of the soft factors.
REFLECTION_SCORE_FACTOR = 0.3
META_REFLECTION_SCORE_FACTOR = 0.75
MAX_REFLECTIONS = 1
MAX_META_REFLECTIONS = 1
VOLATILE_FACTOR = 1.12
_COLD_ARCHIVE_WEIGHT = 0.55
_EMOTION_WEIGHT = 0.35
_VOLATILE_WEIGHT = 0.55
_PAST_LOOKING = re.compile(
    r"(上次|昨天|前天|之前|早些时候|刚才我们|还记得|记不记得|记得吗|"
    r"以前|从前|那次|那回|那段时间|旧事|当时|那会儿|约定过|约好过|说过吗)"
)
ACCESS_TRACKER_FILE = "access_tracker.db"
RERANK_MAX_LENGTH = 512
# The plugin runs on CPU torch: one pair costs ~60 ms, so only the semantic head
# is reranked and the step is skipped when the recall budget is nearly spent.
RERANK_TOP_N = 12
RERANK_RESERVE_SECONDS = 1.2
# Recall runs inside the memory context call (MEMORY_CONTEXT_TIMEOUT_SECONDS in plugin.py);
# it must degrade before that deadline rather than lose the profile and mood fragments too.
RECALL_BUDGET_SECONDS = 4.5
REWRITE_BUDGET_SECONDS = 1.8


class AccessTracker:
    """memory id → last recalled time; the Qt SQLite schema, opened in place."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_access (memory_id TEXT PRIMARY KEY, last_accessed TEXT NOT NULL)"
        )

    def last_accessed(self, memory_ids):
        ids = [str(item).strip() for item in memory_ids if str(item).strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT memory_id, last_accessed FROM memory_access WHERE memory_id IN ({placeholders})", ids
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def record(self, memory_ids, *, when):
        ids = [str(item).strip() for item in memory_ids if str(item).strip()]
        if not ids:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO memory_access (memory_id, last_accessed) VALUES (?, ?)",
                    [(item, when) for item in ids],
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._conn.close()


class CrossEncoderReranker:
    """bge-reranker from a local snapshot directory; never downloads.

    Loading takes seconds, longer than a recall may wait, so it happens on a
    background thread and scoring returns None until the model is resident.
    """

    def __init__(self, snapshot):
        self._snapshot = Path(snapshot)
        self._lock = threading.Lock()
        self._loaded = threading.Event()
        self._model = None
        self._failed = False
        self._closed = False
        self._loader = None

    def available(self):
        return not self._failed and self._snapshot.is_dir()

    def ready(self):
        return self._loaded.is_set() and self._model is not None

    def warm(self):
        with self._lock:
            if self._loader is not None or self._failed or self._closed:
                return
            self._loader = threading.Thread(target=self._load, name="sakura-memory-rerank-load", daemon=True)
            self._loader.start()

    def wait_ready(self, timeout):
        self._loaded.wait(timeout)
        return self.ready()

    def _load_encoder(self):
        quiet_model_loading()
        from sentence_transformers import CrossEncoder

        return CrossEncoder(
            str(self._snapshot), device=_prefer_device(), max_length=RERANK_MAX_LENGTH,
            local_files_only=True, trust_remote_code=False,
        )

    def _load(self):
        try:
            model = self._load_encoder()
        except Exception as exc:
            model = None
            log_event("Memory", "记忆精排模型加载失败，改用语义分",
                      {"code": "MEMORY_RERANK_LOAD_FAILED", "error_type": type(exc).__name__},
                      event="memory.rerank.load_failed", severity="warning")
        with self._lock:
            if model is None:
                self._failed = True
            elif not self._closed:
                self._model = model
        self._loaded.set()

    def score(self, query, texts):
        if not self.ready():
            self.warm()
            return None
        predicted = self._model.predict([[query, text] for text in texts],
                                        batch_size=min(32, len(texts)), show_progress_bar=False)
        return [min(1.0, max(0.0, float(value))) for value in predicted]

    def close(self):
        with self._lock:
            self._closed = True
            self._model = None


def _prefer_device():
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _field(raw, *names):
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    for name in names:
        for source in (raw, metadata):
            value = source.get(name)
            if value not in (None, ""):
                return value
    return None


def _text(raw, *names):
    return str(_field(raw, *names) or "").strip()


def memory_kind(raw):
    return _text(raw, "memoryKind", "memory_kind").lower()


def is_meta_reflection(raw):
    return "meta_reflection" in {memory_kind(raw), _text(raw, "category").lower()}


def is_reflection(raw):
    return (
        _text(raw, "source").lower() == "reflection"
        or _text(raw, "category").lower() in {"reflection", "meta_reflection"}
        or memory_kind(raw) in {"reflection", "meta_reflection"}
    )


def _parse_datetime(value, now):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=now.tzinfo)


def event_date(value, now):
    text = str(value or "").strip()
    if not text:
        return None
    moment = _parse_datetime(text, now)
    if moment is not None:
        return moment.astimezone(now.tzinfo).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_expired(raw, now):
    for names in (("expiresAt", "expires_at"), ("validUntil", "valid_until")):
        moment = _parse_datetime(_field(raw, *names), now)
        if moment is not None and moment <= now:
            return True
    if memory_kind(raw) == "commitment":
        day = event_date(_field(raw, "eventTime", "event_time"), now)
        return day is not None and day < now.date()
    return False


def is_released(raw):
    return _text(raw, "status").lower() == "released"


def query_looks_past(query):
    return bool(_PAST_LOOKING.search(str(query or "")))


def relative_age(value, now):
    moment = _parse_datetime(value, now)
    if moment is None:
        return ""
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 90:
        return "刚才"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 6:
        return f"约{hours}小时前"
    local = moment.astimezone(now.tzinfo)
    if local.date() == now.date():
        return "今天稍早"
    if local.date() == (now - timedelta(days=1)).date():
        return "昨天"
    days = (now - moment).days
    if days < 7:
        return f"约{max(days, 1)}天前"
    if days // 7 < 5:
        return f"约{days // 7}周前"
    return f"约{max(1, days // 30)}个月前"


def annotate(memory, now):
    content = memory["content"]
    parts = []
    if memory.get("expired"):
        kind = memory.get("kind")
        parts.append("已过期的约定" if kind == "commitment" else "已失效的近况" if kind == "recent_status" else "已失效")
    age = relative_age(memory.get("event_time") or memory.get("created_at") or memory.get("updated_at"), now)
    if age:
        parts.append(age)
    annotated = f"（{' · '.join(parts)}）{content}" if parts else content
    if memory.get("is_meta_reflection"):
        return f"（长期认知，非具体经历）{annotated}"
    if memory.get("is_reflection"):
        return f"（独处感想，可影响语气）{annotated}"
    return annotated


def fragment_priority(memory):
    if memory.get("is_meta_reflection"):
        return 55
    if memory.get("is_reflection"):
        return 45
    return 80 if memory["source"] == "explicit" else 70


def fragment_budget(memory):
    if memory.get("is_meta_reflection"):
        return 320
    if memory.get("is_reflection"):
        return 280
    return 512


def _importance(raw, source):
    try:
        value = float(_field(raw, "importance"))
    except (TypeError, ValueError):
        value = 0.5
    value = max(0.0, min(1.0, value))
    return max(value, EXPLICIT_IMPORTANCE) if source == "explicit" else value


def _days_since(value, now):
    moment = _parse_datetime(value, now)
    return 0.0 if moment is None else max(0.0, (now - moment).total_seconds() / 86400.0)


def _decay_weight(importance, days):
    return importance + (1.0 - importance) * math.exp(-DECAY_LAMBDA * days)


def _cold_archive_factor(importance, idle_days, kind):
    if kind in EXEMPT_COLD_ARCHIVE_KINDS or idle_days < COLD_ARCHIVE_IDLE_DAYS or importance >= COLD_ARCHIVE_IMPORTANCE_MAX:
        return 1.0
    fade = min(1.0, (idle_days - COLD_ARCHIVE_IDLE_DAYS) / 120.0) * (COLD_ARCHIVE_IMPORTANCE_MAX - importance)
    return max(COLD_ARCHIVE_DECAY_FLOOR, 1.0 - fade)


def _combine_soft(*pairs):
    """1 + Σ w·(f − 1): gentle factors add their own effect instead of compounding."""
    return max(0.0, 1.0 + sum(weight * (factor - 1.0) for factor, weight in pairs))


def _semantic_rank(raw):
    semantic = _score(raw.get("semanticScore"))
    value = semantic if semantic is not None else _score(raw.get("score"))
    return (value is None, -(value or 0.0))


def _score(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized(raw, now, *, include_expired, last_accessed, excluded_turn_id, emotion=""):
    content = _text(raw, "content", "memory")
    memory_id = _text(raw, "id", "memory_id")
    if not content or not memory_id or is_released(raw):
        return None
    expired = is_expired(raw, now)
    if expired and not include_expired:
        return None
    if excluded_turn_id and _text(raw, "createdInTurnId", "created_in_turn_id") == excluded_turn_id:
        return None
    source = _text(raw, "source").lower() or "inferred"
    kind = memory_kind(raw)
    importance = _importance(raw, source)
    updated_at = _text(raw, "updatedAt", "updated_at")
    created_at = _text(raw, "createdAt", "created_at")
    days = _days_since(last_accessed.get(memory_id) or updated_at or created_at, now)
    memory_emotion = _text(raw, "emotion")
    soft = _combine_soft(
        (_cold_archive_factor(importance, days, kind), _COLD_ARCHIVE_WEIGHT),
        (emotion_congruence_factor(emotion, memory_emotion) if emotion and memory_emotion else 1.0, _EMOTION_WEIGHT),
        (VOLATILE_FACTOR if _field(raw, "volatile") is True else 1.0, _VOLATILE_WEIGHT),
    )
    weight = min(1.0, max(0.0, _decay_weight(importance, days) * soft))
    meta = is_meta_reflection(raw)
    reflection = is_reflection(raw)
    if meta:
        weight *= META_REFLECTION_SCORE_FACTOR
    elif reflection:
        weight *= REFLECTION_SCORE_FACTOR
    rerank = _score(raw.get("rerankScore"))
    semantic = _score(raw.get("semanticScore"))
    return {
        "id": memory_id,
        "content": content,
        "source": source,
        "kind": kind,
        "updated_at": updated_at,
        "created_at": created_at,
        "event_time": _text(raw, "eventTime", "event_time"),
        "relevance": rerank if rerank is not None else semantic if semantic is not None else _score(raw.get("score")),
        "reranked": rerank is not None,
        "weight": weight,
        "expired": expired,
        "is_reflection": reflection and not meta,
        "is_meta_reflection": meta,
    }


class PersonalRecallPolicy:
    """Candidate selection for one character; stateless apart from the access table."""

    candidates = RECALL_CANDIDATES
    budget_seconds = RECALL_BUDGET_SECONDS

    def __init__(self, memory_dir, *, reranker=None, list_memories=None, record_access=False,
                 threshold=RELEVANCE_THRESHOLD, clock=None, rewrite_client=None, mood_reader=None):
        self._memory_dir = Path(memory_dir)
        self._reranker = reranker
        self._list_memories = list_memories
        self._rewrite_client = rewrite_client
        self._mood_reader = mood_reader
        self._record_access = bool(record_access)
        self._threshold = threshold
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._tracker = None
        self._tracker_failed = False
        self._lock = threading.Lock()

    def _access(self):
        with self._lock:
            if self._tracker is None and not self._tracker_failed:
                path = self._memory_dir / ACCESS_TRACKER_FILE
                if not path.is_file() and not self._record_access:
                    return None
                try:
                    self._tracker = AccessTracker(path)
                except Exception:
                    self._tracker_failed = True
            return self._tracker

    def warm(self):
        warm = getattr(self._reranker, "warm", None)
        if callable(warm) and self._reranker.available():
            warm()

    def plan_query(self, request):
        client = None
        if self._rewrite_client is not None:
            try:
                client = self._rewrite_client()
            except Exception:
                client = None
        if client is None:
            return plan_query(request, None)
        # The client's socket timeout bounds each read, not the whole request.
        planned = []
        worker = threading.Thread(target=lambda: planned.append(plan_query(request, client)),
                                  name="sakura-memory-query-rewrite", daemon=True)
        worker.start()
        worker.join(REWRITE_BUDGET_SECONDS)
        return planned[0] if planned else plan_query(request, None)

    def _active_emotion(self, query):
        mood = ""
        if self._mood_reader is not None:
            try:
                mood = str((self._mood_reader() or {}).get("content") or "")
            except Exception:
                mood = ""
        return active_emotion(query, mood)

    def annotate(self, memory):
        return annotate(memory, self._clock())

    def priority(self, memory):
        return fragment_priority(memory)

    def budget(self, memory):
        return fragment_budget(memory)

    def rerank(self, query, memories, *, deadline=None):
        reranker = self._reranker
        if reranker is None or len(memories) < 2 or not reranker.available():
            return memories
        ready = getattr(reranker, "ready", None)
        if callable(ready) and not ready():
            self.warm()
            return memories
        if deadline is not None and deadline - monotonic() < RERANK_RESERVE_SECONDS:
            return memories
        # Rerank and semantic scores are on different scales, so the unscored tail is dropped.
        head = sorted(memories, key=_semantic_rank)[:RERANK_TOP_N]
        texts = [_text(item, "content", "memory") for item in head]
        if not all(texts):
            return memories
        try:
            scores = reranker.score(query, texts)
        except Exception as exc:
            log_event("Memory", "记忆精排失败，改用语义分", {"code": "MEMORY_RERANK_FAILED", "error_type": type(exc).__name__},
                      event="memory.rerank.failed", severity="warning")
            return memories
        if scores is None or len(scores) != len(head):
            return memories
        rescored = [dict(item, rerankScore=score) for item, score in zip(head, scores)]
        rescored.sort(key=lambda item: item["rerankScore"], reverse=True)
        return rescored

    def select(self, query, memories, limit, *, excluded_turn_id="", deadline=None):
        now = self._clock()
        include_expired = query_looks_past(query)
        memories = self.rerank(query, [item for item in memories if isinstance(item, dict)], deadline=deadline)
        tracker = self._access()
        try:
            accessed = tracker.last_accessed([_text(item, "id") for item in memories]) if tracker else {}
        except Exception:
            accessed = {}
        seen = set()
        normalized = []
        emotion = self._active_emotion(query)
        for raw in memories:
            item = _normalized(raw, now, include_expired=include_expired, last_accessed=accessed,
                               excluded_turn_id=excluded_turn_id, emotion=emotion)
            if item is None:
                continue
            key = " ".join(item["content"].lower().split())
            gate = RERANK_THRESHOLD if item["reranked"] else self._threshold
            if key in seen or (item["relevance"] is not None and item["relevance"] < gate):
                continue
            seen.add(key)
            normalized.append(item)
        normalized.sort(key=lambda item: (
            item["relevance"] is None,
            -(item["relevance"] or 0.0) * item["weight"],
            item["source"] != "explicit",
            item["updated_at"],
        ))
        selected, reflections, metas = [], 0, 0
        for item in normalized:
            if item["is_meta_reflection"]:
                if metas >= MAX_META_REFLECTIONS:
                    continue
                metas += 1
            elif item["is_reflection"]:
                if reflections >= MAX_REFLECTIONS:
                    continue
                reflections += 1
            selected.append(item)
            if len(selected) >= limit:
                break
        selected = self._merge_due_commitments(selected, now, limit)
        if self._record_access and tracker is not None:
            try:
                tracker.record([item["id"] for item in selected], when=now.isoformat())
            except Exception:
                pass
        return selected

    def _merge_due_commitments(self, selected, now, limit):
        if self._list_memories is None:
            return selected
        try:
            rows = self._list_memories()
        except Exception:
            return selected
        due = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict) or memory_kind(raw) != "commitment" or is_released(raw):
                continue
            day = event_date(_field(raw, "eventTime", "event_time"), now)
            if day not in {now.date(), now.date() + timedelta(days=1)}:
                continue
            item = _normalized(raw, now, include_expired=False, last_accessed={}, excluded_turn_id="")
            if item is not None:
                due.append(dict(item, relevance=DUE_COMMITMENT_SCORE, weight=DUE_COMMITMENT_DECAY_BOOST))
        due.sort(key=lambda item: (item["updated_at"], item["content"]))
        merged = list(selected)
        seen_ids = {item["id"] for item in merged}
        seen_content = {" ".join(item["content"].lower().split()) for item in merged}
        for item in due[:MAX_DUE_COMMITMENTS]:
            key = " ".join(item["content"].lower().split())
            if item["id"] in seen_ids or key in seen_content:
                continue
            merged.insert(0, item)
            seen_ids.add(item["id"])
            seen_content.add(key)
        return merged[:limit]

    def close(self):
        with self._lock:
            tracker, self._tracker = self._tracker, None
        if tracker is not None:
            tracker.close()
        if self._reranker is not None:
            self._reranker.close()
