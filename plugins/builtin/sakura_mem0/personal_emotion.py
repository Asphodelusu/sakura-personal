"""Rule-based user emotion scoring, ported from the Qt backchannel scorer.

It labels the user's emotion; it is unrelated to the character's reply tone.
"""
import json
from functools import lru_cache
from pathlib import Path

EMOTIONS = (
    "neutral",
    "confused",
    "anxious",
    "frustrated",
    "sad",
    "angry",
    "happy",
    "playful",
    "embarrassed",
    "lonely",
    "tender",
    "warm",
    "determined",
    "defensive",
    "hopeful",
)
DEFAULT_EMOTION = "neutral"
DEFAULT_EMOTION_THRESHOLD = 1.0
_LEXICON_PATH = Path(__file__).resolve().parent / "data" / "emotion_lexicon.json"
# A lexicon hit right after one of these is negated unless the entry itself starts with it.
_NEGATION_MARKERS = (
    "不用", "不必", "无需", "没有", "不会", "不再", "毫不", "并不",
    "不", "没", "无", "别", "莫",
)


def _is_negated_occurrence(content, start, word):
    if word.startswith(_NEGATION_MARKERS):
        return False
    return any(
        start >= len(marker) and content[start - len(marker):start] == marker
        for marker in _NEGATION_MARKERS
    )


def _all_occurrences_negated(word, content):
    index = content.find(word)
    if index == -1:
        return False
    while index != -1:
        if not _is_negated_occurrence(content, index, word):
            return False
        index = content.find(word, index + 1)
    return True


@lru_cache(maxsize=2)
def load_emotion_lexicon(path=_LEXICON_PATH):
    """Invalid or missing lexicon yields an empty table; the scorer then never labels."""
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8")).get("entries")
    except (OSError, UnicodeError, ValueError, AttributeError):
        return {}
    if not isinstance(entries, dict):
        return {}
    lexicon = {}
    for emotion, words in entries.items():
        if emotion not in EMOTIONS or not isinstance(words, dict):
            continue
        cleaned = {}
        for word, weight in words.items():
            try:
                value = float(weight)
            except (TypeError, ValueError):
                continue
            text = str(word).strip()
            if text and value > 0:
                cleaned[text] = value
        if cleaned:
            lexicon[emotion] = cleaned
    return lexicon


class EmotionScorer:
    def __init__(self, lexicon=None, *, threshold=DEFAULT_EMOTION_THRESHOLD):
        self._lexicon = lexicon if lexicon is not None else load_emotion_lexicon()
        self._threshold = threshold

    def scores(self, text):
        """Each word counts once; a word contained in a longer hit does not count."""
        content = (text or "").strip()
        if not content or not self._lexicon:
            return {}
        matched = [
            (word, emotion, weight)
            for emotion, words in self._lexicon.items()
            for word, weight in words.items()
            if word in content and not _all_occurrences_negated(word, content)
        ]
        words_hit = [word for word, _emotion, _weight in matched]
        result = {}
        for word, emotion, weight in matched:
            if any(word != other and word in other for other in words_hit):
                continue
            result[emotion] = result.get(emotion, 0.0) + weight
        return result

    def best(self, text):
        scores = self.scores(text)
        if not scores:
            return None
        emotion = max(scores, key=lambda key: (scores[key], -EMOTIONS.index(key)))
        return emotion if scores[emotion] >= self._threshold else None
