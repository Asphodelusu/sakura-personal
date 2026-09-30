"""Sakura's mood notes and the user's emotion trajectory, one record per character.

Both files are plain JSON under memory_dir, keyed by character id. Diagnostics
never include their text.
"""
import json
import re
import stat
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

if __package__:
    from .support import atomic_write_text, log_event
else:
    from support import atomic_write_text, log_event

MOOD_FILE = "mood_state.json"
USER_EMOTION_FILE = "user_emotion_state.json"
HISTORY_LIMIT = 5
MOOD_CONTEXT_BUDGET = 500
MOOD_DUPLICATE_SIMILARITY = 0.80
MOOD_STALE_SECONDS = 3 * 3600
CONTINUITY_BUDGET = 1200
_MOOD_INFLUENCE = (
    "这是你现在的心情，它会自然影响你的语气和节奏——高兴时轻快，"
    "低落时句子更短更安静，害羞时停顿多。但它只是此刻感受的一部分，不是全部。"
)
_STALE_MOOD_INFLUENCE = "这是你不久前记下的心情，仍可能影响语气，但不必当成刚刚发生的事。"
_USER_EMOTION_HINT = (
    "如果对方连续出现了相似的情绪，那可能是 ta 最近状态的一种信号。"
    "你可以自然地留意，但不必刻意追问。"
)


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _seconds_since(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return (datetime.now().astimezone() - moment).total_seconds()


def _clip(text, budget):
    value = str(text or "").strip()
    return value if len(value) <= budget else value[: max(0, budget - 1)].rstrip() + "…"


def _tokens(text):
    normalized = text.lower()
    ascii_tokens = set(re.findall(r"[a-z0-9_./:-]{2,}", normalized))
    cjk_pairs = {
        normalized[index:index + 2]
        for index in range(max(0, len(normalized) - 1))
        if any("\u3040" <= char <= "\u9fff" for char in normalized[index:index + 2])
    }
    return ascii_tokens | cjk_pairs


def similarity(left, right):
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    union = left_tokens | right_tokens
    token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
    return max(token_score, SequenceMatcher(None, left, right).ratio())


class _ScopedStateFile:
    def __init__(self, memory_dir, name, scope):
        self._path = Path(memory_dir).absolute() / name
        self._scope = str(scope)

    def _plain(self):
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return True
        return not (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
        )

    def load_all(self):
        if not self._plain():
            log_event("Memory", "心情状态文件被拒绝", {"code": "PERSONAL_STATE_LINK", "file": self._path.name},
                      event="memory.personal.state_unreadable", severity="warning")
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError):
            log_event("Memory", "心情状态文件不可读", {"code": "PERSONAL_STATE_UNREADABLE", "file": self._path.name},
                      event="memory.personal.state_unreadable", severity="warning")
            return None
        return data if isinstance(data, dict) else None

    def entry(self):
        data = self.load_all()
        if not data:
            return None
        entry = data.get(self._scope)
        if isinstance(entry, dict):
            return entry
        # Qt builds wrote both "Sakura" and "sakura"; the latest same-name entry wins.
        folded = [
            value for key, value in data.items()
            if isinstance(key, str) and key.casefold() == self._scope.casefold() and isinstance(value, dict)
        ]
        folded.sort(key=lambda value: str(value.get("updated_at") or ""))
        return folded[-1] if folded else None

    def save_entry(self, entry):
        data = self.load_all()
        if data is None:
            raise RuntimeError("PERSONAL_STATE_UNWRITABLE")
        for key in [key for key in data if isinstance(key, str) and key.casefold() == self._scope.casefold()]:
            data.pop(key)
        data[self._scope] = entry
        atomic_write_text(self._path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", backup=True)


def _history(entry):
    raw = entry.get("history") if isinstance(entry, dict) else None
    return [
        {"content": str(item.get("content") or "").strip(), "timestamp": str(item.get("timestamp") or "")}
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    ]


class PersonalMoodStore:
    def __init__(self, memory_dir, scope, *, admit=None):
        self._file = _ScopedStateFile(memory_dir, MOOD_FILE, scope)
        self._admit = admit

    def current(self):
        entry = self._file.entry()
        content = str((entry or {}).get("content") or "").strip()
        if not content:
            return None
        return {"content": content, "updated_at": str(entry.get("updated_at") or ""), "history": _history(entry)}

    def history(self):
        return _history(self._file.entry())

    def is_duplicate(self, content):
        text = str(content or "").strip()
        current = self.current()
        recent = ([current["content"]] if current else []) + [item["content"] for item in self.history()[:3]]
        return any(similarity(text, previous) >= MOOD_DUPLICATE_SIMILARITY for previous in recent)

    def set(self, content):
        text = str(content or "").strip()
        if not text:
            raise ValueError("PERSONAL_MOOD_EMPTY")
        if self._admit is not None:
            self._admit()
        entry = self._file.entry() or {}
        history = _history(entry)
        previous = str(entry.get("content") or "").strip()
        if previous:
            history.insert(0, {"content": previous, "timestamp": str(entry.get("updated_at") or "")})
        self._file.save_entry({"content": text, "updated_at": _now_iso(), "history": history[:HISTORY_LIMIT]})


class PersonalEmotionStore:
    def __init__(self, memory_dir, scope):
        self._file = _ScopedStateFile(memory_dir, USER_EMOTION_FILE, scope)

    def current(self):
        entry = self._file.entry()
        content = str((entry or {}).get("content") or "").strip()
        if not content:
            return None
        return {"content": content, "updated_at": str(entry.get("updated_at") or ""), "history": _history(entry)}

    def record(self, emotion):
        """A repeated emotion only refreshes the time; it does not fake a new history item."""
        label = str(emotion or "").strip()
        if not label:
            return
        entry = self._file.entry() or {}
        history = _history(entry)
        previous = str(entry.get("content") or "").strip()
        if previous and previous != label:
            history.insert(0, {"content": previous, "timestamp": str(entry.get("updated_at") or "")})
        self._file.save_entry({"content": label, "updated_at": _now_iso(), "history": history[:HISTORY_LIMIT]})


def build_mood_fragment(scope, mood):
    """Qt continuity mood block; None when there is nothing to say."""
    if not mood:
        return None
    body = _clip(mood["content"], MOOD_CONTEXT_BUDGET)
    age = _seconds_since(mood.get("updated_at"))
    stale = age is not None and age >= MOOD_STALE_SECONDS
    heading = "【不久前的心情】" if stale else "【今の気持ち】"
    lines = [heading, body, f"（{_STALE_MOOD_INFLUENCE if stale else _MOOD_INFLUENCE}）"]
    history = mood.get("history") or []
    if history:
        lines.append("之前的心情：")
        lines.extend(f"· [{item['timestamp'][:16]}] {item['content']}" for item in history[:HISTORY_LIMIT])
    return {
        "id": f"mood:{scope}",
        "content": _clip("\n".join(lines), CONTINUITY_BUDGET),
        "priority": 90,
        "budgetHint": 600,
        "sensitivity": "private",
    }


def build_user_emotion_fragment(scope, emotion):
    if not emotion:
        return None
    lines = ["【对方的情绪】", f"对方刚才的情绪偏向：{emotion['content']}"]
    history = emotion.get("history") or []
    if history:
        lines.append("最近几次的情绪：")
        lines.extend(f"· [{item['timestamp'][:16]}] {item['content']}" for item in history[:3])
        lines.append(_USER_EMOTION_HINT)
    return {
        "id": f"user_emotion:{scope}",
        "content": "\n".join(lines),
        "priority": 85,
        "budgetHint": 200,
        "sensitivity": "private",
    }
