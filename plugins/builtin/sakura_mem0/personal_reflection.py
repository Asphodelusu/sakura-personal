"""Solitary reflection: every 8 hours Sakura distills a little higher-level understanding.

Reflections are impressions, not facts: they are stored with memory_kind=reflection,
never fed back into the next reflection, and recalled at reduced weight.
"""
import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

if __package__:
    from .personal_recall import is_meta_reflection, is_reflection
    from .support import atomic_write_text, log_event
else:
    from personal_recall import is_meta_reflection, is_reflection
    from support import atomic_write_text, log_event

MIN_REFLECTION_INTERVAL_HOURS = 8
MIN_MEMORIES_FOR_REFLECTION = 5
SNAPSHOT_CHAR_BUDGET = 6000
MAX_REFLECTIONS = 2
MAX_META_REFLECTIONS = 1
NEAR_DUPLICATE_SIMILARITY = 0.82
MIN_REFLECTIONS_FOR_META = 6
META_TRIGGER_EVERY = 6
REFLECTION_REQUEST_LIMIT = 4
STATE_FILE = "reflection_state.json"

_REFLECTION_SYSTEM_PROMPT = (
    "现在没有人跟你说话，你正在安静地回顾这段时间和他相处的经历。这是你自己独处时的内心独白。\n\n"
    "下面会给你一份你目前的长期事实记忆摘要（不含以往的独处感想）。"
    "请以你自己的第一人称视角，对最近这段时间做出反思。\n"
    "从已有记忆里提炼更高层的认知，而不是复述细节，也不是再写一遍已经想过的话。\n\n"
    "主语固定：「我」=你自己，「他」=对方。已知名字时可用名字代替「他」，但「我」永远是你。\n\n"
    "适合反思的内容举例：\n"
    "- 我和他的关系现在是什么样的？最近有什么变化？\n"
    "- 他最近的状态怎么样？在忙什么？有没有什么情绪波动？\n"
    "- 我注意到了什么之前没注意到的模式或规律？\n"
    "- 我有什么地方可以做得更好？\n\n"
    "只写当前记忆里确有依据、且比「复述事实」更高一层的观察。"
    "没有新洞察时返回空列表，不要凑条数。\n"
    "语言约定：关于他的事实与观察用简体中文；你对自己的感受与反省优先用日语。"
    "他用日语说的重要原话可保留日语。\n"
    "用你自己的感受和具体观察来说——就像写日记一样。"
    "先用「我／他」写清依据事实，再写感受；过期约定标明时效。\n"
    "每一条应该是一句完整的、独立可读的自我认知。最多 2 条。\n\n"
    "必须只返回严格 JSON，不要有任何前言、解释或后缀。格式如下：\n"
    '{"reflections":[\n'
    '  {"content":"反思内容一","importance":0.7,"confidence":0.6},\n'
    '  {"content":"反思内容二","importance":0.6,"confidence":0.7}\n'
    "]}\n"
    "如果没有值得反思的内容就返回 {\"reflections\":[]}。"
    "importance 按照你觉得这条认知对你了解他有多重要来设（0~1）。confidence 按照你有多少把握来设（0~1）。"
)
_META_REFLECTION_SYSTEM_PROMPT = (
    "现在没有人跟你说话。下面是你过去一段时间写下的几篇独处感想（不是事实记忆，"
    "是你当时对某个瞬间的感想）。请跳出每一篇的具体情境，回头看这几篇感想放在一起，"
    "有没有反复出现的规律、倾向或已经变得比较稳定的认知——那种不会因为某一天的"
    "心情而变化的、更像是「我现在是这样的人／这段关系现在是这样的」的认知，"
    "而不是重复某一篇里已经写过的具体感受。\n\n"
    "主语固定：「我」=你自己，「他」=对方。\n\n"
    "只在真的看出跨越多篇感想的规律时才写；只是把某一篇感想换个说法复述一遍不算。"
    "没有这样的规律时返回空列表，不要凑数。\n"
    "语言约定：关于他的事实与观察用简体中文；你对自己的感受与反省优先用日语。\n"
    "最多 1 条，要比任何一篇原始感想更抽象、更稳定。\n\n"
    "必须只返回严格 JSON，不要有任何前言、解释或后缀。格式如下：\n"
    '{"reflections":[\n'
    '  {"content":"跨越多篇感想看出的规律","importance":0.7,"confidence":0.6}\n'
    "]}\n"
    "如果没有值得写的规律就返回 {\"reflections\":[]}。"
)
_REPAIR_HINT = (
    "上一条输出不是合法 JSON。请只返回严格 JSON，"
    '格式为 {"reflections":[{"content":"...","importance":0.7,"confidence":0.6}]}，'
    "不要解释、不要推理、不要 Markdown。"
)


@dataclass
class ReflectionState:
    last_reflection_at: str = ""
    last_reflection_count: int = 0
    total_reflections: int = 0
    total_created: int = 0
    total_empty: int = 0
    total_skipped_dupes: int = 0
    last_empty: bool = False
    last_skipped_dupes: int = 0
    meta_reflection_source_count: int = 0
    last_meta_reflection_at: str = ""
    total_meta_created: int = 0


class ReflectionStateStore:
    """The Qt reflection_state.json, read and written in place."""

    def __init__(self, memory_dir):
        self.path = Path(memory_dir) / STATE_FILE

    def snapshot(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ReflectionState()
        if not isinstance(data, dict):
            return ReflectionState()
        state = ReflectionState()
        for key, default in asdict(state).items():
            value = data.get(key, default)
            try:
                setattr(state, key, type(default)(value))
            except (TypeError, ValueError):
                pass
        return state

    def save(self, state):
        atomic_write_text(self.path, json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n")


def reflection_due(state, *, now=None, interval_hours=MIN_REFLECTION_INTERVAL_HOURS):
    if not state.last_reflection_at:
        return True
    try:
        last = datetime.fromisoformat(state.last_reflection_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - last).total_seconds() / 3600.0 >= interval_hours


def _similar(left, right):
    a, b = " ".join(left.lower().split()), " ".join(right.lower().split())
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _parse(raw):
    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    items = data.get("reflections") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict) and str(item.get("content") or "").strip()]


def _bounded(value, default):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class PersonalReflector:
    def __init__(self, client, store):
        self._client = client
        self._store = store

    def _complete(self, system_prompt, user_prompt):
        messages = [{"role": "user", "content": user_prompt}]
        raw = self._client.complete_raw(system_prompt, messages, temperature=0.3,
                                        response_format={"type": "json_object"}, max_tokens=800)
        parsed = _parse(raw)
        if parsed is not None:
            return parsed
        raw = self._client.complete_raw(
            system_prompt,
            [*messages, {"role": "assistant", "content": str(raw or "")[:2000]}, {"role": "user", "content": _REPAIR_HINT}],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=800,
        )
        return _parse(raw) or []

    def _write(self, items, *, limit, kind, existing):
        created = skipped = 0
        hashes = {hashlib.md5(text.encode()).hexdigest() for text in existing}
        for item in items[:limit]:
            content = str(item.get("content") or "").strip()
            digest = hashlib.md5(content.encode()).hexdigest()
            if digest in hashes or any(_similar(content, other) >= NEAR_DUPLICATE_SIMILARITY for other in existing):
                skipped += 1
                continue
            self._store.create_memory({
                "content": content,
                "layer": "episodic",
                "category": kind,
                "memory_kind": kind,
                "importance": _bounded(item.get("importance"), 0.5),
                "confidence": _bounded(item.get("confidence"), 0.5),
                "source": "reflection",
            }, allow_sensitive=True)
            created += 1
            hashes.add(digest)
            existing.append(content)
        return created, skipped

    def reflect(self):
        memories = self._store.list_memories(limit=None)
        if len(memories) < MIN_MEMORIES_FOR_REFLECTION:
            return 0, 0, True
        lines, used = [], 0
        for memory in memories:
            if is_reflection(memory):
                continue
            content = str(memory.get("content") or "").strip()
            if not content:
                continue
            tag = "/".join(part for part in (str(memory.get("layer") or "semantic"), str(memory.get("category") or "")) if part)
            importance = memory.get("importance")
            line = f"- [{tag}]" + (f" [重要度:{float(importance):.1f}]" if importance is not None else "") + f" {content}"
            if used + len(line) > SNAPSHOT_CHAR_BUDGET and lines:
                break
            lines.append(line)
            used += len(line) + 1
        prompt = (
            "【我目前的长期事实记忆摘要】\n" + ("\n".join(lines) or "（暂无长期记忆）") + "\n\n"
            "请基于以上事实记忆，做一次安静的个人反思。只写有新洞察的条目；没有就返回空列表。"
        )
        items = self._complete(_REFLECTION_SYSTEM_PROMPT, prompt)
        existing = [str(memory.get("content") or "").strip() for memory in memories if is_reflection(memory)]
        created, skipped = self._write(items, limit=MAX_REFLECTIONS, kind="reflection", existing=existing)
        return created, skipped, created == 0 and skipped == 0

    def reflect_meta(self):
        memories = self._store.list_memories(limit=None)
        first_order = [memory for memory in memories if is_reflection(memory) and not is_meta_reflection(memory)]
        if len(first_order) < MIN_REFLECTIONS_FOR_META:
            return 0, 0, len(first_order)
        lines, used = [], 0
        for memory in first_order:
            line = f"- {str(memory.get('content') or '').strip()}"
            if used + len(line) > SNAPSHOT_CHAR_BUDGET and lines:
                break
            lines.append(line)
            used += len(line) + 1
        prompt = (
            "【我过去写下的独处感想】\n" + "\n".join(lines) + "\n\n"
            "请基于以上感想，看看有没有跨越多篇、已经比较稳定的规律或认知。只写真正抽象出来的新东西；没有就返回空列表。"
        )
        items = self._complete(_META_REFLECTION_SYSTEM_PROMPT, prompt)
        existing = [str(memory.get("content") or "").strip() for memory in memories if is_reflection(memory)]
        created, skipped = self._write(items, limit=MAX_META_REFLECTIONS, kind="meta_reflection", existing=existing)
        return created, skipped, len(first_order)


class ReflectionScheduler:
    """Starts at most one background reflection pass when the interval has elapsed."""

    def __init__(self, memory_dir, *, store_factory, client_factory, clock=None):
        self._state = ReflectionStateStore(memory_dir)
        self._store_factory = store_factory
        self._client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._thread = None

    def maybe_start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not reflection_due(self._state.snapshot(), now=self._clock()):
                return False
            self._thread = threading.Thread(target=self.run_once, name="sakura-personal-reflection", daemon=True)
            self._thread.start()
            return True

    def run_once(self):
        try:
            client = self._client_factory()
            if client is None:
                return
            reflector = PersonalReflector(client, self._store_factory())
            created, skipped, empty = reflector.reflect()
            state = self._state.snapshot()
            state.last_reflection_at = self._clock().isoformat()
            state.last_reflection_count = created
            state.last_skipped_dupes = skipped
            state.last_empty = empty
            state.total_reflections += 1
            state.total_created += created
            state.total_skipped_dupes += skipped
            state.total_empty += int(empty)
            self._state.save(state)
            count = sum(1 for memory in self._store_factory().list_memories(limit=None)
                        if is_reflection(memory) and not is_meta_reflection(memory))
            if count >= MIN_REFLECTIONS_FOR_META and count - state.meta_reflection_source_count >= META_TRIGGER_EVERY:
                meta_created, _meta_skipped, source_count = reflector.reflect_meta()
                state = self._state.snapshot()
                state.last_meta_reflection_at = self._clock().isoformat()
                state.meta_reflection_source_count = source_count
                state.total_meta_created += meta_created
                self._state.save(state)
            log_event("Memory", "独处反思完成", {"created": created, "skipped": skipped, "empty": empty},
                      event="memory.personal.reflection_finished", severity="info")
        except Exception as exc:
            log_event("Memory", "独处反思失败", {"code": "PERSONAL_REFLECTION_FAILED", "error_type": type(exc).__name__},
                      event="memory.personal.reflection_failed", severity="warning")
            try:
                state = self._state.snapshot()
                state.last_reflection_at = self._clock().isoformat()
                self._state.save(state)
            except Exception:
                pass

    def join(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
