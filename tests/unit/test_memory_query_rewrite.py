from __future__ import annotations

from app.agent.memory_query_rewrite import (
    build_baseline_memory_query,
    context_request_from_parts,
    rewrite_memory_query_heuristic,
    _parse_rewrite_payload,
)
from app.agent.memory_recall_eval import (
    baseline_query_builder,
    default_fixture_dir,
    heuristic_query_builder,
    load_cases,
    load_corpus,
    run_lexical_eval,
)


def test_heuristic_drops_visual_and_food_distractors() -> None:
    request = context_request_from_parts(
        current_input="沙耶之歌结局到底在讲什么来着",
        recent_user_messages=["今晚好想吃火锅", "牛肚要多一点"],
        visual_summaries=["屏幕上在看美食推荐视频"],
    )
    baseline = build_baseline_memory_query(request)
    planned = rewrite_memory_query_heuristic(request)

    assert "沙耶之歌" in planned.query
    assert "火锅" not in planned.query
    assert "美食推荐" not in planned.query
    assert "火锅" in baseline
    assert "美食推荐" in baseline


def test_heuristic_keeps_one_recent_for_anaphora() -> None:
    request = context_request_from_parts(
        current_input="那个游戏后来查清楚了吗",
        recent_user_messages=["帮我搜一下 BRAIN 恐怖脑症候群是什么游戏"],
        visual_summaries=["正在看 bilibili 首页"],
    )
    planned = rewrite_memory_query_heuristic(request)
    assert "BRAIN" in planned.query or "恐怖" in planned.query
    assert "bilibili" not in planned.query.lower()


def test_parse_rewrite_payload_appends_entities() -> None:
    plan = _parse_rewrite_payload('{"query":"沙耶之歌结局","entities":["沙耶之歌","虚渊玄"]}')
    assert plan is not None
    assert plan.source == "llm"
    assert "沙耶之歌结局" in plan.query
    assert "虚渊玄" in plan.query
    assert plan.entities == ("沙耶之歌", "虚渊玄")


def test_frozen_eval_heuristic_beats_or_ties_baseline() -> None:
    root = default_fixture_dir()
    corpus = load_corpus(root / "corpus.json")
    cases = load_cases(root / "cases.json")
    assert len(cases) >= 8

    baseline = run_lexical_eval(
        corpus=corpus,
        cases=cases,
        query_builder=baseline_query_builder,
        mode="baseline",
        k=5,
    )
    heuristic = run_lexical_eval(
        corpus=corpus,
        cases=cases,
        query_builder=heuristic_query_builder,
        mode="heuristic",
        k=5,
    )
    # 改写目标：召回不弱，且明显少带近期无关话题的 forbidden 记忆。
    assert heuristic.mean_recall + 1e-9 >= baseline.mean_recall - 0.05
    assert heuristic.contamination_rate <= baseline.contamination_rate + 1e-9
    assert heuristic.contamination_rate < baseline.contamination_rate or heuristic.mean_recall > baseline.mean_recall
