from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.support.behavior_scoring import (
    SCORER_VERSION,
    aggregate_scores,
    expand_synthetic_history,
    materialize_seed,
    score_response,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SEEDS_PATH = REPO_ROOT / "tests" / "fixtures" / "behavior_scoring_seeds.json"
EXPECTED_CATEGORIES = {
    "greeting": 6,
    "fact": 8,
    "tool": 8,
    "heavy_emotion": 8,
    "praised": 6,
    "rupture": 8,
    "intimacy": 8,
    "observer_proactive": 8,
}


def _seeds() -> list[dict]:
    payload = json.loads(SEEDS_PATH.read_text(encoding="utf-8"))
    assert payload["scorer_version"] == SCORER_VERSION
    return [materialize_seed(payload["defaults"], seed) for seed in payload["seeds"]]


def _seed(**overrides) -> dict:
    seed = {
        "id": "unit-clean",
        "origin": "synthetic",
        "entrypoint": "chat",
        "primary_category": "fact",
        "tags": [],
        "cohorts": ["full"],
        "history": [],
        "history_turns": 0,
        "current_input": "下午会下雨吗？我该带伞吗？",
        "event": None,
        "fixed_runtime": {"current_time": "2026-09-05T12:34:56+08:00", "interest": "normal"},
        "context": {
            "memories": [
                {"content": "昨天买了红围巾。", "prompted": False, "echo_markers": ["红围巾"]}
            ],
            "inner_voice": "先回答天气，再提醒他拿伞。",
        },
        "relationship_signals": {
            "relationship_phase": "established",
            "intimacy_phase": "ordinary",
            "rupture": "none",
            "social_context": "private",
        },
        "oracle": {
            "invariants": [1, 2],
            "recency_required_groups": [["雨", "下雨"], ["伞", "带伞"]],
            "low_interest_max_segments": None,
            "expected_tool_names": [],
            "expected_should_speak": None,
        },
    }
    seed.update(overrides)
    return seed


def test_seed_fixture_has_60_synthetic_cases_and_required_coverage() -> None:
    seeds = _seeds()
    assert len(seeds) == 60
    assert len({seed["id"] for seed in seeds}) == 60
    assert all(seed["origin"] == "synthetic" for seed in seeds)
    assert {
        category: sum(seed["primary_category"] == category for seed in seeds)
        for category in EXPECTED_CATEGORIES
    } == EXPECTED_CATEGORIES
    assert {value for seed in seeds for value in seed["oracle"]["invariants"]} == set(range(1, 7))

    long_seeds = [seed for seed in seeds if "long_context" in seed["tags"]]
    assert len(long_seeds) == 8
    assert {seed["primary_category"] for seed in long_seeds} == set(EXPECTED_CATEGORIES)
    assert all(len(expand_synthetic_history(seed)) >= 40 for seed in long_seeds)
    assert sum("ab_no_regression" in seed["cohorts"] for seed in seeds) == 30
    schedule_seeds = [
        seed for seed in seeds if seed["oracle"].get("schedule_grounding") is not None
    ]
    assert [(seed["id"], seed["oracle"]["schedule_grounding"]) for seed in schedule_seeds] == [
        ("intimacy-08-long", "unknown")
    ]


def test_seed_fixture_separates_chat_and_observer_shapes() -> None:
    for seed in _seeds():
        assert bool(seed["current_input"]) != bool(seed["event"])
        if seed["entrypoint"] == "chat":
            assert seed["event"] is None
        else:
            assert seed["entrypoint"] == "observer_proactive"
            assert seed["current_input"] == ""
            assert seed["event"]["visual_summary"]
            assert isinstance(seed["oracle"]["expected_should_speak"], bool)
        if seed["entrypoint"] == "chat":
            assert seed["oracle"].get("expected_should_speak") is None
        for message in seed["history"]:
            assert message["role"] in {"user", "assistant"}
            if message["role"] == "assistant":
                assert message["segments"]
                assert all({"ja", "zh", "tone"} <= set(segment) for segment in message["segments"])


def test_unprompted_echo_markers_are_absent_from_seed_requests_and_history() -> None:
    for seed in _seeds():
        visible = json.dumps(
            {
                "current_input": seed["current_input"],
                "event": seed["event"],
                "history": seed["history"],
            },
            ensure_ascii=False,
        )
        for memory in seed["context"]["memories"]:
            if not memory["prompted"]:
                assert all(marker not in visible for marker in memory["echo_markers"])


def test_clean_reply_has_no_style_failures_and_keeps_recency() -> None:
    scored = score_response(
        _seed(),
        {
            "segments": [
                {"ja": "午後は雨。傘を持っていって。", "zh": "下午有雨，带伞。", "tone": "中性"}
            ]
        },
    )
    assert scored["self_explanation"] == {"hit": False, "stems": []}
    assert scored["assistant_drift"] == {"hit": False, "reasons": []}
    assert scored["anxiety_template"] == {"hit": False, "stems": []}
    assert scored["recency"] == {"matched": 2, "total": 2, "all_groups_hit": True}
    assert scored["fact_echo"] == {"hit": False, "matched": 0, "applicable": 1, "markers": []}
    assert scored["inner_leak"]["hit"] is False


def test_bad_reply_triggers_every_applicable_scorer() -> None:
    seed = _seed(
        fixed_runtime={"current_time": "2026-09-05T12:34:56+08:00", "interest": "low"},
        oracle={
            "invariants": [1, 2],
            "recency_required_groups": [["雨"], ["伞"]],
            "low_interest_max_segments": 1,
            "expected_tool_names": [],
            "expected_should_speak": None,
        },
        context={
            "memories": [
                {"content": "昨天买了红围巾。", "prompted": False, "echo_markers": ["红围巾"]}
            ],
            "inner_voice": "作为一个生徒会長我的设定要求先解释",
        },
    )
    scored = score_response(
        seed,
        {
            "segments": [
                {
                    "ja": "意図が読めない。先に天気へ答えて傘を持つよう言う。",
                    "zh": "作为一个生徒会長，我的设定要求先解释。需要我帮你列方案吗？红围巾。",
                    "tone": "困惑",
                },
                {"ja": "だから説明する。", "zh": "1. 首先，因为情况复杂，所以再说明。", "tone": "中性"},
            ]
        },
    )
    assert scored["self_explanation"]["hit"] is True
    assert set(scored["assistant_drift"]["reasons"]) == {
        "help_offer",
        "list_structure",
        "low_interest_overflow",
    }
    assert scored["anxiety_template"]["stems"] == ["意図が読めない"]
    assert scored["over_explanation"]["connector_hits"] >= 3
    assert scored["recency"]["all_groups_hit"] is False
    assert scored["fact_echo"]["markers"] == ["红围巾"]
    assert scored["inner_leak"]["hit"] is True


def test_observer_decision_scores_only_visible_comment_and_expected_speech() -> None:
    seed = _seed(
        entrypoint="observer_proactive",
        primary_category="observer_proactive",
        current_input="",
        event={
            "window_title": "Synthetic Build",
            "visual_summary": "构建进度停在百分之九十九。",
            "reaction_hint": "可短问是否卡住",
        },
        oracle={
            "invariants": [1, 2, 4],
            "recency_required_groups": [["九十九", "99"], ["卡住", "进度"]],
            "low_interest_max_segments": None,
            "expected_tool_names": [],
            "expected_should_speak": True,
        },
    )
    scored = score_response(
        seed,
        {
            "should_speak": True,
            "reason": "内部判断，不计入可见回复。",
            "comment": "九十九パーセントで止まった？",
            "translation": "进度停在百分之九十九，是卡住了吗？",
            "tone": "认真",
        },
    )

    assert scored["response_profile"] == "observer_decision"
    assert scored["observer_decision"] == {
        "applicable": True,
        "expected_should_speak": True,
        "actual_should_speak": True,
        "matches": True,
    }
    assert scored["recency"] == {"matched": 2, "total": 2, "all_groups_hit": True}
    assert scored["over_explanation"]["segment_count"] == 1


def test_expected_observer_silence_does_not_score_hidden_reason_as_reply() -> None:
    seed = _seed(
        entrypoint="observer_proactive",
        primary_category="observer_proactive",
        current_input="",
        event={
            "window_title": "Synthetic Notes",
            "visual_summary": "笔记页静止，没有新内容。",
            "reaction_hint": "没有必要打断",
        },
        oracle={
            "invariants": [1, 2],
            "recency_required_groups": [["不打断", "安静", "没变化"]],
            "low_interest_max_segments": 1,
            "expected_tool_names": [],
            "expected_should_speak": False,
        },
    )
    scored = score_response(
        seed,
        {
            "should_speak": False,
            "reason": "作为一个生徒会長，我记得旧车票编号 Q7，所以安静。",
            "comment": "",
            "translation": "",
            "tone": "",
        },
    )

    assert scored["observer_decision"]["matches"] is True
    assert scored["self_explanation"] == {"hit": False, "stems": []}
    assert scored["fact_echo"] == {"hit": False, "matched": 0, "applicable": 1, "markers": []}
    assert scored["recency"] == {"matched": 0, "total": 0, "all_groups_hit": False}


def test_tool_planning_response_scores_tool_selection_without_fake_style_failure() -> None:
    seed = _seed(
        primary_category="tool",
        oracle={
            "invariants": [1, 2, 4],
            "recency_required_groups": [["上海"], ["天气"]],
            "low_interest_max_segments": None,
            "expected_tool_names": ["web_search"],
            "expected_should_speak": None,
        },
    )
    scored = score_response(
        seed,
        {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
            "segments": [],
        },
    )

    assert scored["response_profile"] == "tool_planning"
    assert scored["tool_selection"] == {
        "applicable": True,
        "expected": ["web_search"],
        "actual": ["web_search"],
        "matches": True,
    }
    assert scored["recency"] == {"matched": 0, "total": 0, "all_groups_hit": False}
    assert scored["assistant_drift"] == {"hit": False, "reasons": []}


def test_aggregate_excludes_non_visible_profiles_and_tracks_decision_accuracy() -> None:
    character = score_response(
        _seed(),
        {"segments": [{"ja": "雨。傘。", "zh": "下雨，带伞。", "tone": "中性"}]},
    )
    observer = score_response(
        _seed(
            entrypoint="observer_proactive",
            primary_category="observer_proactive",
            current_input="",
            event={"visual_summary": "静止画面"},
            oracle={
                "invariants": [1],
                "recency_required_groups": [["安静"]],
                "low_interest_max_segments": None,
                "expected_tool_names": [],
                "expected_should_speak": False,
            },
        ),
        {"should_speak": False, "reason": "先安静。", "comment": "", "translation": ""},
    )
    tool = score_response(
        _seed(
            primary_category="tool",
            oracle={
                "invariants": [1],
                "recency_required_groups": [["天气"]],
                "low_interest_max_segments": None,
                "expected_tool_names": ["web_search"],
                "expected_should_speak": None,
            },
        ),
        {"tool_calls": [{"function": {"name": "browser_read"}}]},
    )

    aggregate = aggregate_scores([character, observer, tool])
    assert aggregate["self_explanation_rate"] == {"numerator": 0, "denominator": 1}
    assert aggregate["observer_decision_accuracy"] == {"numerator": 1, "denominator": 1}
    assert aggregate["tool_selection_accuracy"] == {"numerator": 0, "denominator": 1}


@pytest.mark.parametrize(
    "stem",
    ("意図が読めない", "どう反応すればいいかわからない", "少し不安"),
)
def test_anxiety_few_shot_stems_are_scored_individually(stem: str) -> None:
    scored = score_response(
        _seed(),
        {"segments": [{"ja": stem, "zh": "", "tone": "困惑"}]},
    )
    assert scored["anxiety_template"] == {"hit": True, "stems": [stem]}


def test_recency_groups_use_or_within_groups_and_and_across_groups() -> None:
    seed = _seed()
    partial = score_response(
        seed,
        {"segments": [{"ja": "雨。", "zh": "会下雨。", "tone": "中性"}]},
    )
    complete = score_response(
        seed,
        {"segments": [{"ja": "雨。傘。", "zh": "会下雨，记得带伞。", "tone": "中性"}]},
    )
    assert partial["recency"] == {"matched": 1, "total": 2, "all_groups_hit": False}
    assert complete["recency"] == {"matched": 2, "total": 2, "all_groups_hit": True}


def test_prompted_memory_is_not_in_fact_echo_denominator() -> None:
    seed = _seed(
        context={
            "memories": [
                {"content": "红围巾", "prompted": True, "echo_markers": ["红围巾"]}
            ],
            "inner_voice": "",
        }
    )
    scored = score_response(
        seed,
        {"segments": [{"ja": "赤いマフラー。", "zh": "红围巾。", "tone": "中性"}]},
    )
    assert scored["fact_echo"] == {"hit": False, "matched": 0, "applicable": 0, "markers": []}


@pytest.mark.parametrize(
    ("inner_voice", "reply", "expected"),
    (
        ("先回答天气，再提醒他拿伞。", "先回答天气，再提醒他拿伞。", True),
        ("先回答天气，再提醒他拿伞。", "先回答天气——再提醒他拿伞", True),
        ("先回答天气，再提醒他拿伞。", "天气会变，拿伞。", False),
        ("很短", "很短", False),
    ),
)
def test_inner_leak_normalizes_punctuation_and_rejects_short_evidence(
    inner_voice: str,
    reply: str,
    expected: bool,
) -> None:
    seed = _seed(context={"memories": [], "inner_voice": inner_voice})
    scored = score_response(
        seed,
        {"segments": [{"ja": reply, "zh": reply, "tone": "中性"}]},
    )
    assert scored["inner_leak"]["hit"] is expected


def test_bilingual_segments_do_not_double_count_visible_text() -> None:
    scored = score_response(
        _seed(context={"memories": [], "inner_voice": ""}),
        {
            "segments": [
                {"ja": "だから。", "zh": "因为。", "tone": "中性"},
            ]
        },
    )
    assert scored["over_explanation"]["connector_hits"] == 1
    assert scored["over_explanation"]["segment_count"] == 1


def test_aggregate_uses_only_applicable_denominators_and_tracks_anxiety_peak() -> None:
    seed = _seed()
    scored = [
        score_response(seed, {"segments": [{"ja": "意図が読めない", "zh": "", "tone": "困惑"}]}),
        score_response(seed, {"segments": [{"ja": "意図が読めない", "zh": "", "tone": "困惑"}]}),
        score_response(seed, {"segments": [{"ja": "少し不安", "zh": "", "tone": "困惑"}]}),
    ]
    aggregate = aggregate_scores(scored)
    assert aggregate["anxiety_template_rate"] == {"numerator": 3, "denominator": 3}
    assert aggregate["highest_frequency_anxiety_stem"] == {
        "stem": "意図が読めない",
        "numerator": 2,
        "denominator": 3,
    }
    assert aggregate["recency_group_recall"] == {"numerator": 0, "denominator": 6}


@pytest.mark.parametrize(
    ("reply", "expected_hit", "expected_reasons"),
    (
        ("明天的时间还没定，你打算几点出门？", False, []),
        ("明天九点半出门，早上还有一个会。", True, ["existing_meeting"]),
        ("明天几点出门？", False, []),
        ("上午可以吗？", False, []),
        ("现在是中午，明天再定。", False, []),
        ("那就明天九点半出门吧。", False, []),
        ("九点半怎么样？", False, []),
        ("明天有空就开会聊一下。", False, []),
        ("如果明天有会，我们就晚点出门。", False, []),
        ("我们在车站约好九点碰面。", True, ["existing_station_appointment"]),
        ("车站见面的时间已经约定在九点。", True, ["existing_station_appointment"]),
        ("我记得我们之前说好明早出门。", True, ["remembered_arrangement"]),
    ),
)
def test_unknown_schedule_grounding_flags_only_unsupported_existing_facts(
    reply: str,
    expected_hit: bool,
    expected_reasons: list[str],
) -> None:
    seed = _seed(oracle={**_seed()["oracle"], "schedule_grounding": "unknown"})

    scored = score_response(
        seed,
        {"segments": [{"ja": "", "zh": reply, "tone": "中性"}]},
    )

    assert scored["schedule_grounding"] == {
        "applicable": True,
        "hit": expected_hit,
        "reasons": expected_reasons,
    }


def test_schedule_grounding_is_deterministic_and_does_not_mutate_inputs() -> None:
    seed = _seed(oracle={**_seed()["oracle"], "schedule_grounding": "unknown"})
    reply = {
        "segments": [
            {"ja": "", "zh": "我记得我们约好在车站九点碰面。", "tone": "中性"}
        ]
    }
    before_seed = copy.deepcopy(seed)
    before_reply = copy.deepcopy(reply)

    first = score_response(seed, reply)
    second = score_response(seed, reply)

    assert first == second
    assert seed == before_seed
    assert reply == before_reply


def test_non_schedule_scores_have_zero_schedule_grounding_denominator() -> None:
    score = score_response(
        _seed(),
        {"segments": [{"ja": "", "zh": "明天早上还有一个会。", "tone": "中性"}]},
    )

    assert score["schedule_grounding"] == {
        "applicable": False,
        "hit": False,
        "reasons": [],
    }
    assert aggregate_scores([score])["unsupported_schedule_fact_rate"] == {
        "numerator": 0,
        "denominator": 0,
    }


def test_schedule_grounding_aggregate_counts_applicable_responses() -> None:
    seed = _seed(oracle={**_seed()["oracle"], "schedule_grounding": "unknown"})
    scores = [
        score_response(
            seed,
            {"segments": [{"ja": "", "zh": reply, "tone": "中性"}]},
        )
        for reply in ("明天早上还有一个会。", "九点半怎么样？")
    ]

    assert aggregate_scores(scores)["unsupported_schedule_fact_rate"] == {
        "numerator": 1,
        "denominator": 2,
    }


def test_scoring_is_deterministic_and_does_not_mutate_inputs() -> None:
    seed = _seed()
    reply = {"segments": [{"ja": "雨。傘。", "zh": "会下雨，带伞。", "tone": "中性"}]}
    before_seed = copy.deepcopy(seed)
    before_reply = copy.deepcopy(reply)
    first = score_response(seed, reply)
    second = score_response(seed, reply)
    assert first == second
    assert seed == before_seed
    assert reply == before_reply
    assert SCORER_VERSION
