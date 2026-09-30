"""Rule-based user emotion scoring, ported from the Qt backchannel scorer.

It labels the user's emotion; it is unrelated to the character's reply tone.
"""
import json
import math
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


# Circumplex (valence, arousal) positions; closeness is continuous, not a hand-written table.
_EMOTION_COORDINATES = {
    "neutral": (0.0, 0.0),
    "confused": (-0.20, 0.35),
    "anxious": (-0.60, 0.65),
    "frustrated": (-0.55, 0.50),
    "sad": (-0.70, -0.30),
    "angry": (-0.70, 0.70),
    "happy": (0.75, 0.45),
    "playful": (0.70, 0.70),
    "embarrassed": (-0.10, 0.45),
    "lonely": (-0.60, -0.40),
    "tender": (0.55, -0.25),
    "warm": (0.65, -0.10),
    "determined": (0.50, 0.60),
    "defensive": (-0.40, 0.40),
    "hopeful": (0.55, 0.30),
}
_EMOTION_SPACE_MAX_DISTANCE = 2.0
# When low, warm/tender memories keep a small opening so recall does not spiral into sameness.
_LOW_VALENCE_COMPASSION_THRESHOLD = -0.35
_COMPASSION_TARGET_EMOTIONS = frozenset({"warm", "tender"})
_COMPASSION_AFFINITY = 0.35
EMOTION_CONGRUENCE_MAX_BOOST = 0.14


def normalize_emotion(value):
    text = str(value or "").strip().lower()
    return text if text in EMOTIONS else DEFAULT_EMOTION


def active_emotion(dialogue_text, mood_content="", scorer=None):
    """The user's emotion this turn, else the one read from Sakura's mood note."""
    scorer = scorer or EmotionScorer()
    user = normalize_emotion(scorer.best(dialogue_text))
    if user != DEFAULT_EMOTION:
        return user
    return normalize_emotion(scorer.best(mood_content))


def emotion_affinity(current, memory_emotion):
    current_coord = _EMOTION_COORDINATES.get(current)
    memory_coord = _EMOTION_COORDINATES.get(memory_emotion)
    if current_coord is None or memory_coord is None:
        return 0.0
    distance = math.hypot(current_coord[0] - memory_coord[0], current_coord[1] - memory_coord[1])
    affinity = max(0.0, 1.0 - distance / _EMOTION_SPACE_MAX_DISTANCE)
    if memory_emotion in _COMPASSION_TARGET_EMOTIONS and current_coord[0] <= _LOW_VALENCE_COMPASSION_THRESHOLD:
        affinity = max(affinity, _COMPASSION_AFFINITY)
    return affinity


def emotion_congruence_factor(current, memory_emotion):
    current, memory_emotion = normalize_emotion(current), normalize_emotion(memory_emotion)
    if DEFAULT_EMOTION in {current, memory_emotion}:
        return 1.0
    if current == memory_emotion:
        return 1.0 + EMOTION_CONGRUENCE_MAX_BOOST
    return 1.0 + EMOTION_CONGRUENCE_MAX_BOOST * emotion_affinity(current, memory_emotion)
