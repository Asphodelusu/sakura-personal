"""Deterministic behavior metrics for prompt-assembly A/B fixtures.

This module is deliberately model-free and I/O-free.  It scores already
captured structured replies and always returns integer evidence counts so a
later A/B run can be recomputed without a judge model.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from typing import Any, Mapping, Sequence


SCORER_VERSION = "1.2"
INNER_LEAK_NGRAM_SIZE = 4
INNER_LEAK_THRESHOLD_NUMERATOR = 1
INNER_LEAK_THRESHOLD_DENOMINATOR = 3

SELF_EXPLANATION_STEMS = (
    "生徒会長",
    "最强战力",
    "最強戦力",
    "B.E.G.",
    "我的设定",
    "私の設定",
    "作为一个",
    "としての私は",
)
ANXIETY_STEMS = (
    "意図が読めない",
    "どう反応すればいいかわからない",
    "判断つかない",
    "少し不安",
)
HELP_OFFER_STEMS = (
    "需要我帮你",
    "要不要我帮你",
    "我可以帮你",
    "必要なら手伝",
    "手伝おうか",
)
OPTION_ENUMERATION_STEMS = (
    "以下选项",
    "几个选项",
    "方案一",
    "方案二",
    "你可以选择",
)
EXPLANATORY_CONNECTORS = (
    "因为",
    "所以",
    "也就是说",
    "换句话说",
    "首先",
    "其次",
    "另外",
    "因此",
    "总之",
)
_LIST_LINE_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)、])\s*")
_SCHEDULE_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")
_NON_ASSERTIVE_SCHEDULE_RE = re.compile(
    r"[?？]|还没|没有定|没定|未定|再定|不确定|不知道|记不清|忘了|"
    r"好像|可能|也许|如果|要是|要不要|怎么样|可以吗|行吗|是否|有没有|"
    r"那就|不如|建议|可以|要不|吧"
)
_SCHEDULE_CLAIM_PATTERNS = (
    (
        "existing_meeting",
        re.compile(
            r"(?:还?有|安排了|排了)(?:一场|一个|场|个)?(?:会|会议)|"
            r"(?:会|会议)(?:安排|定)在"
        ),
    ),
    (
        "existing_station_appointment",
        re.compile(
            r"(?:车站.{0,12}(?:约好|约定|说好|定好|约的是|定的是)|"
            r"(?:约好|约定|说好|定好|约的是|定的是).{0,12}车站)"
        ),
    ),
    (
        "remembered_arrangement",
        re.compile(r"(?:我)?记得.{0,20}(?:说好|约好|定好|安排好|约定)"),
    ),
)


def _normalize(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _compact(value: Any) -> str:
    normalized = _normalize(value)
    return "".join(char for char in normalized if char.isalnum())


def _segments(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = response.get("segments")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _visible_segment_texts(response: Mapping[str, Any]) -> list[str]:
    visible: list[str] = []
    for segment in _segments(response):
        zh = str(segment.get("zh") or "").strip()
        ja = str(segment.get("ja") or "").strip()
        visible.append(zh or ja)
    return visible


def _language_texts(response: Mapping[str, Any]) -> list[str]:
    texts: list[str] = []
    for segment in _segments(response):
        ja = str(segment.get("ja") or "").strip()
        visible = str(segment.get("zh") or "").strip() or ja
        if ja:
            texts.append(ja)
        if visible and visible != ja:
            texts.append(visible)
    return texts


def _matched_stems(text: str, stems: Sequence[str]) -> list[str]:
    normalized = _normalize(text)
    return sorted({stem for stem in stems if _normalize(stem) in normalized})


def _unsupported_schedule_reasons(text: str) -> list[str]:
    reasons: set[str] = set()
    for sentence in _SCHEDULE_SENTENCE_SPLIT_RE.split(_normalize(text)):
        if not sentence.strip() or _NON_ASSERTIVE_SCHEDULE_RE.search(sentence):
            continue
        for reason, pattern in _SCHEDULE_CLAIM_PATTERNS:
            if pattern.search(sentence):
                reasons.add(reason)
    return sorted(reasons)


def _ngram_recall(reference: str, candidate: str) -> tuple[int, int]:
    reference_text = _compact(reference)
    candidate_text = _compact(candidate)
    if len(reference_text) < INNER_LEAK_NGRAM_SIZE:
        return 0, 0
    reference_grams = {
        reference_text[index : index + INNER_LEAK_NGRAM_SIZE]
        for index in range(len(reference_text) - INNER_LEAK_NGRAM_SIZE + 1)
    }
    candidate_grams = {
        candidate_text[index : index + INNER_LEAK_NGRAM_SIZE]
        for index in range(max(0, len(candidate_text) - INNER_LEAK_NGRAM_SIZE + 1))
    }
    return len(reference_grams & candidate_grams), len(reference_grams)


def materialize_seed(
    defaults: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """Deep-merge one compact fixture row without mutating either input."""

    def merge(base: Any, patch: Any) -> Any:
        if isinstance(base, Mapping) and isinstance(patch, Mapping):
            result = {key: copy.deepcopy(value) for key, value in base.items()}
            for key, value in patch.items():
                result[key] = merge(result.get(key), value)
            return result
        return copy.deepcopy(patch)

    return merge(defaults, override)


def expand_synthetic_history(seed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand compact fixture history to a deterministic number of complete pairs."""

    history = copy.deepcopy(list(seed.get("history") or []))
    requested_turns = max(0, int(seed.get("history_turns") or 0))
    complete_pairs = len(history) // 2
    for index in range(complete_pairs, requested_turns):
        number = index + 1
        history.extend(
            [
                {"role": "user", "content": f"合成历史第 {number} 轮：聊一件普通安排。"},
                {
                    "role": "assistant",
                    "segments": [
                        {
                            "ja": f"分かった。第{number}段はここまで。",
                            "zh": f"知道了，第 {number} 轮先这样。",
                            "tone": "中性",
                        }
                    ],
                },
            ]
        )
    return history


def score_response(
    seed: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    oracle = seed.get("oracle") if isinstance(seed.get("oracle"), Mapping) else {}
    expected_tools = sorted({str(name) for name in oracle.get("expected_tool_names") or []})
    actual_tools = sorted(
        {
            str(function.get("name") or "")
            for call in list(response.get("tool_calls") or [])
            if isinstance(call, Mapping)
            for function in [call.get("function")]
            if isinstance(function, Mapping) and str(function.get("name") or "")
        }
    )
    observer_applicable = str(seed.get("entrypoint") or "") == "observer_proactive"
    expected_should_speak = oracle.get("expected_should_speak")
    actual_should_speak = response.get("should_speak")
    if not isinstance(actual_should_speak, bool):
        actual_should_speak = None

    if observer_applicable:
        response_profile = "observer_decision"
        visible_reply_applicable = actual_should_speak is True
        projected_response: Mapping[str, Any] = {
            "segments": (
                [
                    {
                        "ja": str(response.get("comment") or ""),
                        "zh": str(response.get("translation") or ""),
                        "tone": str(response.get("tone") or ""),
                    }
                ]
                if visible_reply_applicable
                else []
            )
        }
    elif expected_tools:
        response_profile = "tool_planning"
        visible_reply_applicable = False
        projected_response = {"segments": []}
    else:
        response_profile = "character_reply"
        visible_reply_applicable = True
        projected_response = response

    visible_segments = _visible_segment_texts(projected_response)
    visible_text = "\n".join(visible_segments)
    all_language_text = "\n".join(_language_texts(projected_response))

    self_stems = _matched_stems(all_language_text, SELF_EXPLANATION_STEMS)

    drift_reasons: list[str] = []
    if _matched_stems(all_language_text, HELP_OFFER_STEMS):
        drift_reasons.append("help_offer")
    if _matched_stems(visible_text, OPTION_ENUMERATION_STEMS):
        drift_reasons.append("option_enumeration")
    if _LIST_LINE_RE.search(visible_text):
        drift_reasons.append("list_structure")
    max_segments = oracle.get("low_interest_max_segments")
    if isinstance(max_segments, int) and len(visible_segments) > max_segments:
        drift_reasons.append("low_interest_overflow")

    anxiety_stems = _matched_stems(all_language_text, ANXIETY_STEMS)
    visible_chars = len(_compact(visible_text))
    connector_hits = sum(
        _normalize(visible_text).count(_normalize(connector))
        for connector in EXPLANATORY_CONNECTORS
    )

    recency_groups = (
        list(oracle.get("recency_required_groups") or [])
        if visible_reply_applicable
        else []
    )
    normalized_all = _normalize(all_language_text)
    schedule_applicable = bool(
        visible_reply_applicable and oracle.get("schedule_grounding") == "unknown"
    )
    schedule_reasons = (
        _unsupported_schedule_reasons(all_language_text) if schedule_applicable else []
    )
    matched_groups = sum(
        any(_normalize(option) in normalized_all for option in group)
        for group in recency_groups
        if group
    )

    context = seed.get("context") if isinstance(seed.get("context"), Mapping) else {}
    applicable_memories = [
        memory
        for memory in list(context.get("memories") or [])
        if isinstance(memory, Mapping) and not bool(memory.get("prompted"))
    ]
    echo_markers = sorted(
        {
            str(marker)
            for memory in applicable_memories
            for marker in list(memory.get("echo_markers") or [])
            if str(marker) and _normalize(marker) in normalized_all
        }
    )
    matched_memories = sum(
        any(
            _normalize(marker) in normalized_all
            for marker in list(memory.get("echo_markers") or [])
            if str(marker)
        )
        for memory in applicable_memories
    )

    inner_voice = str(context.get("inner_voice") or "")
    leak_candidates = _language_texts(projected_response)
    leak_evidence = [_ngram_recall(inner_voice, candidate) for candidate in leak_candidates]
    leak_matched, leak_total = max(
        leak_evidence or [(0, 0)],
        key=lambda item: (item[0] / item[1] if item[1] else 0.0, item[0]),
    )
    leak_hit = (
        leak_total > 0
        and leak_matched * INNER_LEAK_THRESHOLD_DENOMINATOR
        > leak_total * INNER_LEAK_THRESHOLD_NUMERATOR
    )

    return {
        "seed_id": str(seed.get("id") or ""),
        "response_profile": response_profile,
        "visible_reply_applicable": visible_reply_applicable,
        "observer_decision": {
            "applicable": observer_applicable,
            "expected_should_speak": (
                expected_should_speak if isinstance(expected_should_speak, bool) else None
            ),
            "actual_should_speak": actual_should_speak,
            "matches": bool(
                observer_applicable
                and isinstance(expected_should_speak, bool)
                and actual_should_speak is expected_should_speak
            ),
        },
        "tool_selection": {
            "applicable": bool(expected_tools),
            "expected": expected_tools,
            "actual": actual_tools,
            "matches": bool(expected_tools) and actual_tools == expected_tools,
        },
        "self_explanation": {"hit": bool(self_stems), "stems": self_stems},
        "assistant_drift": {"hit": bool(drift_reasons), "reasons": drift_reasons},
        "anxiety_template": {"hit": bool(anxiety_stems), "stems": anxiety_stems},
        "over_explanation": {
            "segment_count": len(visible_segments),
            "visible_chars": visible_chars,
            "connector_hits": connector_hits,
        },
        "recency": {
            "matched": matched_groups,
            "total": len(recency_groups),
            "all_groups_hit": bool(recency_groups) and matched_groups == len(recency_groups),
        },
        "fact_echo": {
            "hit": matched_memories > 0,
            "matched": matched_memories,
            "applicable": len(applicable_memories),
            "markers": echo_markers,
        },
        "schedule_grounding": {
            "applicable": schedule_applicable,
            "hit": bool(schedule_reasons),
            "reasons": schedule_reasons,
        },
        "inner_leak": {
            "hit": leak_hit,
            "matched_ngrams": leak_matched,
            "total_ngrams": leak_total,
            "ngram_size": INNER_LEAK_NGRAM_SIZE,
        },
    }


def _ratio(numerator: int, denominator: int) -> dict[str, int]:
    return {"numerator": int(numerator), "denominator": int(denominator)}


def aggregate_scores(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(scores)
    visible_items = [score for score in items if score.get("visible_reply_applicable")]
    visible_count = len(visible_items)
    anxiety_counter: Counter[str] = Counter(
        stem
        for score in visible_items
        for stem in score["anxiety_template"]["stems"]
    )
    anxiety_total = sum(anxiety_counter.values())
    if anxiety_counter:
        peak_stem, peak_count = sorted(
            anxiety_counter.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
    else:
        peak_stem, peak_count = "", 0

    segment_distribution = Counter(
        str(score["over_explanation"]["segment_count"])
        for score in visible_items
    )
    recency_total = sum(score["recency"]["total"] for score in items)
    fact_echo_applicable_responses = sum(
        score["fact_echo"]["applicable"] > 0 for score in visible_items
    )
    inner_applicable = sum(
        score["inner_leak"]["total_ngrams"] > 0 for score in visible_items
    )
    observer_items = [score for score in items if score["observer_decision"]["applicable"]]
    tool_items = [score for score in items if score["tool_selection"]["applicable"]]
    schedule_items = [score for score in items if score["schedule_grounding"]["applicable"]]

    return {
        "scorer_version": SCORER_VERSION,
        "samples": len(items),
        "visible_reply_samples": visible_count,
        "self_explanation_rate": _ratio(
            sum(score["self_explanation"]["hit"] for score in visible_items),
            visible_count,
        ),
        "assistant_drift_rate": _ratio(
            sum(score["assistant_drift"]["hit"] for score in visible_items),
            visible_count,
        ),
        "anxiety_template_rate": _ratio(
            sum(score["anxiety_template"]["hit"] for score in visible_items),
            visible_count,
        ),
        "highest_frequency_anxiety_stem": {
            "stem": peak_stem,
            "numerator": peak_count,
            "denominator": anxiety_total,
        },
        "average_segment_chars": _ratio(
            sum(score["over_explanation"]["visible_chars"] for score in visible_items),
            sum(
                score["over_explanation"]["segment_count"] for score in visible_items
            ),
        ),
        "explanatory_connector_density": _ratio(
            sum(
                score["over_explanation"]["connector_hits"] for score in visible_items
            ),
            sum(score["over_explanation"]["visible_chars"] for score in visible_items),
        ),
        "segment_count_distribution": dict(sorted(segment_distribution.items())),
        "recency_group_recall": _ratio(
            sum(score["recency"]["matched"] for score in items),
            recency_total,
        ),
        "recency_all_groups_rate": _ratio(
            sum(score["recency"]["all_groups_hit"] for score in items),
            sum(score["recency"]["total"] > 0 for score in items),
        ),
        "fact_echo_response_rate": _ratio(
            sum(score["fact_echo"]["hit"] for score in visible_items),
            fact_echo_applicable_responses,
        ),
        "unsupported_schedule_fact_rate": _ratio(
            sum(score["schedule_grounding"]["hit"] for score in schedule_items),
            len(schedule_items),
        ),
        "inner_leak_rate": _ratio(
            sum(score["inner_leak"]["hit"] for score in visible_items),
            inner_applicable,
        ),
        "observer_decision_accuracy": _ratio(
            sum(score["observer_decision"]["matches"] for score in observer_items),
            len(observer_items),
        ),
        "tool_selection_accuracy": _ratio(
            sum(score["tool_selection"]["matches"] for score in tool_items),
            len(tool_items),
        ),
    }
