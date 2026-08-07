"""内心独白与记忆召回 fork-join：跳过、成功 push、失败 fail-open。"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from app.agent.inner_thought import InnerThoughtResult, InnerThoughtSettings
from app.agent.runtime import AgentRuntime
from app.agent.turn_routing import TurnPlan, TurnState
from app.llm.api_client import OpenAICompatibleClient


def _runtime_with_thought_client() -> AgentRuntime:
    client = MagicMock(spec=OpenAICompatibleClient)
    runtime = AgentRuntime(
        client,
        "system",
        inner_thought_api_client=client,
        inner_thought_settings=InnerThoughtSettings(enabled=True, skip_fast_tier=True),
    )
    return runtime


def _plan(tier: str = "standard") -> TurnPlan:
    return TurnPlan(
        tier=tier,  # type: ignore[arg-type]
        modality="text",
        client_key="chat",
        decided_by="test",
    )


def _standard_turn() -> TurnState:
    return TurnState(turn_plan=_plan("standard"), recall_decision="light")


def test_launch_skipped_for_fast_tier_without_worker() -> None:
    runtime = _runtime_with_thought_client()
    launch = runtime._launch_inner_thought_worker(
        [{"role": "user", "content": "hi"}],
        TurnState(turn_plan=_plan("fast"), recall_decision="skip"),
        proactive_mode=False,
    )
    assert launch is None
    assert runtime._inner_thought_done_for_turn is True
    runtime._finalize_inner_thought_worker(None)
    assert runtime._inner_thought_window.items() == ()


def test_finalize_pushes_after_parallel_join() -> None:
    runtime = _runtime_with_thought_client()
    started = time.perf_counter()

    def _slow_thought(*_args: object, **_kwargs: object) -> InnerThoughtResult:
        time.sleep(0.05)
        return InnerThoughtResult(text="並行できた", interest="high")

    with patch("app.agent.context_builder.generate_inner_thought", side_effect=_slow_thought):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "在吗"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        # 模拟主线程做召回（与 Flash 重叠）
        time.sleep(0.02)
        runtime._finalize_inner_thought_worker(launch)

    elapsed = time.perf_counter() - started
    assert runtime._inner_thought_window.items() == ("並行できた",)
    assert runtime._turn_interest == "high"
    assert "【本轮篇幅】" in runtime._turn_verbosity_guidance
    # 重叠执行：总耗时应明显小于 sleep 之和（0.05+0.02）
    assert elapsed < 0.08


def test_finalize_without_interest_skips_verbosity_block() -> None:
    runtime = _runtime_with_thought_client()
    with patch(
        "app.agent.context_builder.generate_inner_thought",
        return_value=InnerThoughtResult(text="特に何も", interest=None),
    ):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "嗯"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        runtime._finalize_inner_thought_worker(launch)
    assert runtime._inner_thought_window.items() == ("特に何も",)
    assert runtime._turn_interest is None
    assert runtime._turn_verbosity_guidance == ""


def test_finalize_fail_open_keeps_window_empty() -> None:
    runtime = _runtime_with_thought_client()

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("flash down")

    with patch("app.agent.context_builder.generate_inner_thought", side_effect=_boom):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "hi"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        runtime._finalize_inner_thought_worker(launch)

    assert runtime._inner_thought_window.items() == ()
    assert runtime._inner_thought_done_for_turn is True


def test_finalize_join_timeout_fail_open() -> None:
    """主路径 join 超时后跳过独白，不因后台慢请求死等。"""
    runtime = AgentRuntime(
        MagicMock(spec=OpenAICompatibleClient),
        "system",
        inner_thought_api_client=MagicMock(spec=OpenAICompatibleClient),
        inner_thought_settings=InnerThoughtSettings(
            enabled=True,
            skip_fast_tier=True,
            join_timeout_seconds=1,
        ),
    )

    def _slow_thought(*_args: object, **_kwargs: object) -> InnerThoughtResult:
        time.sleep(2.5)
        return InnerThoughtResult(text="遅すぎ", interest="mid")

    started = time.perf_counter()
    with patch("app.agent.context_builder.generate_inner_thought", side_effect=_slow_thought):
        launch = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "在吗"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert launch is not None
        runtime._finalize_inner_thought_worker(launch)
        elapsed = time.perf_counter() - started
        assert runtime._inner_thought_window.items() == ()
        assert elapsed < 2.0
        # 回收后台慢任务，避免非守护线程拖住用例结束
        try:
            launch.future.result(timeout=3)
        except Exception:
            pass


def test_second_launch_in_same_turn_is_noop() -> None:
    runtime = _runtime_with_thought_client()
    with patch("app.agent.context_builder.generate_inner_thought", return_value="once"):
        first = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "a"}],
            _standard_turn(),
            proactive_mode=False,
        )
        second = runtime._launch_inner_thought_worker(
            [{"role": "user", "content": "b"}],
            _standard_turn(),
            proactive_mode=False,
        )
        assert first is not None
        assert second is None
        runtime._finalize_inner_thought_worker(first)
    assert runtime._inner_thought_window.items() == ("once",)
