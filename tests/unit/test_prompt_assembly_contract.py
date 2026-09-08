"""Prompt assembly contracts for role admission, layers, trust, and position.

The snapshots written by this module contain Sakura's private character material.
They therefore live under ignored ``.local/golden/prompts`` and are refreshed
only when ``SAKURA_UPDATE_PROMPT_GOLDENS`` names ``baseline`` or ``candidate``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.llm import api_client as api_client_module
from app.llm import payload_inspection as payload_inspection_module
from app.agent.inner_thought import (
    build_inner_thought_system_prompt,
    build_inner_thought_user_prompt,
    load_character_excerpt,
)
from app.agent.memory_curator import MemoryCurator
from app.agent.memory_reflector import _REFLECTION_SYSTEM_PROMPT
from app.agent.runtime import AgentRuntime
from app.config.character_loader import load_system_prompt
from app.llm.api_client import (
    ApiRequestError,
    ApiSettings,
    OpenAICompatibleClient,
    _build_chat_completion_payload,
    _messages_with_runtime_context,
)
from app.llm.prompts.runtime import (
    ContextPolicy,
    RUNTIME_FACTS_HEADER,
    RUNTIME_FACTS_SLOT_MARKER,
    RUNTIME_NOW_SLOT_MARKER,
    RUNTIME_TRUSTED_STATE_HEADER,
    PromptRuntime,
    estimate_prompt_tokens,
)
from app.llm.prompts.blocks import _split_labeled_prompt_sections
from app.llm.prompts.types import (
    ContextFragment,
    ContextFragmentDecision,
    ContextRequest,
    ContextSnapshot,
    PromptRecipe,
)
from app.perception.observer import ObservationPacket, ProactiveConfig, ProactiveObserver
from app.plugins.models import PromptPatchContribution
from tests.support.behavior_scoring import (
    SCORER_VERSION,
    expand_synthetic_history,
    materialize_seed,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CARD_PATH = REPO_ROOT / "characters" / "Sakura" / "card.md"
GUARDS_PATH = REPO_ROOT / "characters" / "Sakura" / "system_guards.md"
RELATIONSHIP_GUIDE_PATH = REPO_ROOT / "characters" / "Sakura" / "relationship_guide.md"
GOLDEN_ROOT = REPO_ROOT / ".local" / "golden" / "prompts"
BEHAVIOR_SEEDS_PATH = REPO_ROOT / "tests" / "fixtures" / "behavior_scoring_seeds.json"
PROMPT_ARM_ROOT = REPO_ROOT / ".local" / "ab" / "arms"

PROMPT_ARM_STAGES = {
    "A": "stage_0_oracle",
    "B": "stage_1b",
    "C": "stage_2",
    "D": "stage_3",
}
PROMPT_ARM_MODEL_SENTINEL = "__AB_MODEL_MUST_BE_SUPPLIED__"
PROMPT_ARM_SOURCE_PATHS = (
    "app/agent/context_builder.py",
    "app/agent/inner_thought.py",
    "app/agent/prompt_builder.py",
    "app/llm/api_client.py",
    "app/llm/payload_inspection.py",
    "app/llm/prompts/blocks.py",
    "app/llm/prompts/runtime.py",
    "app/perception/observer.py",
    "characters/Sakura/card.md",
    "characters/Sakura/system_guards.md",
    "characters/Sakura/relationship_guide.md",
    "data/intimacy_guide.txt",
)

RUNTIME_L5_FRAGMENT_IDS = ("memory.", "session_state.", "runtime.character_lore")
SEMANTIC_COMPOSE_NUDGES = frozenset(
    {
        (
            "请根据以上对话与工具执行结果（如有），输出本轮给对方的最终 Sakura 回复。"
            "只返回合法 JSON segments；每个 segment 必须同时包含 ja 与 zh。"
            "不要调用工具，不要解释，不要使用 Markdown。"
        ),
        (
            "检索/读页阶段已结束。请阅读上方【联网证据】与 tool 结果，"
            "输出本轮给对方的最终 Sakura 回复（回答问题本身，不要再说正在查询）。"
            "只返回合法 JSON segments；每个 segment 必须同时包含 ja 与 zh。"
            "不要调用工具，不要解释，不要使用 Markdown。"
        ),
    }
)

CARD_HEADINGS = (
    "她怎样存在",
    "判断、选择与修复",
    "关系中的她",
    "身体距离与欲望",
    "生活里的具体纹理",
    "情绪的重量",
    "语言与节奏",
)
BEHAVIOR_HEADINGS = CARD_HEADINGS[0], CARD_HEADINGS[1], CARD_HEADINGS[-1]
NARRATIVE_HEADINGS = CARD_HEADINGS[2:-1]

LAYER_BUDGETS = {
    "agent_tool_loop": {
        "L0": 220,
        "L1": 1_550,
        "L2": 3_500,
        "L3": 950,
        "L4": 1_500,
        "L4'": 650,
        "total": 8_200,
    },
    "final_reply": {
        "L0": 220,
        "L1": 1_550,
        "L2": 3_500,
        "L3": 200,
        "L4": 200,
        "L4'": 650,
        "total": 5_900,
    },
    "proactive_tool_loop": {
        "L0": 220,
        "L1": 1_550,
        "L2": 0,
        "L3": 0,
        "L4": 1_120,
        "L4'": 650,
        "total": 3_400,
    },
}
PLUGIN_PATCH_TOTAL_BUDGET = 600


class _RecordingPromptRuntime(PromptRuntime):
    def __init__(self) -> None:
        self.last_recipe: PromptRecipe | None = None

    def build(self, recipe, snapshot=None, *, runtime_role="system"):
        self.last_recipe = recipe
        return super().build(recipe, snapshot, runtime_role=runtime_role)


def _merged_sakura_prompt() -> str:
    return load_system_prompt(CARD_PATH, system_guards_path=GUARDS_PATH)


def _fixed_snapshot() -> ContextSnapshot:
    request = ContextRequest(
        current_input="今天外面会下雨吗？",
        current_time="2026-09-05T12:34:56+08:00",
    )
    fragments = (
        ContextFragment(
            fragment_id="runtime.time",
            source="runtime",
            content="[当前本地时间]\n2026-09-05 12:34:56 +08:00",
            trust="trusted",
            priority=100,
            token_budget=192,
            sensitivity="public",
            cache_scope="step",
        ),
        ContextFragment(
            fragment_id="runtime.agent_progress",
            source="runtime",
            content="当前 Agent 循环是第 1 步，之后最多还可以继续 3 步。",
            trust="trusted",
            priority=100,
            token_budget=128,
            sensitivity="public",
        ),
        ContextFragment(
            fragment_id="memory.fixed",
            source="memory",
            content="[固定记忆]\n他今天打算带伞。",
            trust="trusted",
            priority=90,
            token_budget=128,
            sensitivity="private",
        ),
        ContextFragment(
            fragment_id="plugin.fixture.screen",
            source="plugin:fixture",
            content="[固定屏幕摘要]\n天气页面写着午后有雨。",
            trust="untrusted",
            priority=70,
            token_budget=128,
            sensitivity="private",
        ),
    )
    decisions = tuple(
        ContextFragmentDecision(
            fragment=fragment,
            estimated_tokens=estimate_prompt_tokens(fragment.content),
            included=True,
        )
        for fragment in fragments
    )
    return ContextSnapshot(
        request=request,
        selected=decisions,
        estimated_tokens=sum(item.estimated_tokens for item in decisions),
        token_budget=4_096,
    )


def _runtime(*, oversized_patch: bool = False) -> AgentRuntime:
    patches = []
    if oversized_patch:
        patches.append(
            PromptPatchContribution(
                patch_id="oversized",
                system_prompt_append="插件静态补充" * 900,
            )
        )
    client = MagicMock(spec=OpenAICompatibleClient)
    runtime = AgentRuntime(
        client,
        _merged_sakura_prompt(),
        reply_tones=["中性", "不满", "害羞", "请求", "困惑"],
        reply_portraits=["站立微笑"],
        prompt_patches=patches,
        character_name="夜乃桜",
    )
    runtime._relationship_guide = RELATIONSHIP_GUIDE_PATH.read_text(encoding="utf-8").strip()
    runtime._intimacy_guide = "# 固定导演层\n仅用于离线装配快照，不来自真实对话。"
    return runtime


def _observer() -> ProactiveObserver:
    return ProactiveObserver(
        api_base_url="https://example.invalid",
        api_key="fixture-key",
        api_model="fixture-vlm",
        system_prompt=_merged_sakura_prompt(),
        chat_api_base_url="https://example.invalid",
        chat_api_key="fixture-key",
        chat_api_model="fixture-chat",
        config=ProactiveConfig(enabled=False),
        relationship=SimpleNamespace(expression_bias="natural"),
    )


def _assert_no_card_headings(text: str) -> None:
    found = [heading for heading in CARD_HEADINGS if f"## {heading}" in text]
    assert not found, f"unexpected full-card headings: {found}"


def _assert_identity_anchor(text: str) -> None:
    assert "数字生命" in text
    assert "对等" in text
    assert all(line.strip() != "【人格设定】" for line in text.splitlines())
    assert "【演出约束】" not in text
    assert "身体距离与欲望" not in text


def _assert_behavior_core_only(text: str) -> None:
    missing = [heading for heading in BEHAVIOR_HEADINGS if f"## {heading}" not in text]
    assert not missing, f"missing L1 behavior headings: {missing}"
    narrative = [heading for heading in NARRATIVE_HEADINGS if f"## {heading}" in text]
    assert not narrative, f"unexpected L2 narrative headings: {narrative}"
    assert "身体距离与欲望" not in text
    assert "演出约束" not in text


def _semantic_user(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = str(message.get("content") or "")
    return not content.startswith("[Sakura runtime context;")


def _layer_for(section_id: str) -> str:
    if section_id == "persona.identity_anchor":
        return "L0"
    if section_id == "persona.behavior_core":
        return "L1"
    if section_id.startswith(
        ("persona.narrative", "persona.relationship", "persona.intimacy")
    ):
        return "L2"
    if section_id.startswith("reply."):
        return "L3"
    if section_id.startswith(("agent.", "context.", "tools.", "event.", "final_reply.")):
        return "L4"
    if section_id == "persona.guards_tail":
        return "L4'"
    if section_id.startswith("plugin_patch."):
        return "plugin"
    raise AssertionError(f"unmapped prompt section: {section_id}")


def _prompt_result(runtime: AgentRuntime, recipe_name: str):
    snapshot = _fixed_snapshot()
    if recipe_name == "agent_tool_loop":
        return runtime._build_tool_prompt_result(
            snapshot,
            recent_messages=[{"role": "user", "content": snapshot.request.current_input}],
        )
    if recipe_name == "final_reply":
        return runtime._build_final_reply_result(snapshot)
    if recipe_name == "proactive_tool_loop":
        return runtime._build_proactive_tool_prompt_result(snapshot)
    if recipe_name == "event_reply":
        return runtime._build_event_reply_result("reminder_due", snapshot)
    raise AssertionError(f"unknown recipe: {recipe_name}")


def _capturing_client(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict],
    *,
    rejection: str = "",
) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient(
        ApiSettings(
            base_url="https://example.invalid/v1",
            api_key="fixture-key",
            model="fixture-model",
        )
    )
    rejected = False

    def fake_post(payload: dict, **_kwargs):
        nonlocal rejected
        captured.append(copy.deepcopy(payload))
        if rejection and not rejected:
            rejected = True
            raise ApiRequestError(
                rejection,
                status_code=400,
                error_message=rejection,
            )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"segments":[]}',
                    },
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(
        client,
        "_post_chat_completions_with_compatibility_fallbacks",
        fake_post,
    )
    return client


def _fallback_rejection(tier: int) -> str:
    if tier == 0:
        return ""
    if tier == 1:
        return "system message is not allowed in the middle of messages"
    if tier == 2:
        return "only one system message is allowed; system role must be first"
    raise AssertionError(f"unknown runtime fallback tier: {tier}")


def _direct_payload_messages(
    monkeypatch: pytest.MonkeyPatch,
    result,
    *,
    fallback_tier: int,
) -> list[dict]:
    captured: list[dict] = []
    client = _capturing_client(
        monkeypatch,
        captured,
        rejection=_fallback_rejection(fallback_tier),
    )
    client.complete_with_tools(
        result.system_prompt,
        [
            {"role": "assistant", "content": "昨晚预报说可能转阴。"},
            {"role": "user", "content": "今天外面会下雨吗？"},
        ],
        tools=[],
        runtime_context=result.runtime_context,
        request_purpose="initial",
    )
    return captured[-1]["messages"]


def _golden_payload_messages(result) -> list[dict]:
    messages = _messages_with_runtime_context(
        [
            {"role": "assistant", "content": "昨晚预报说可能转阴。"},
            {"role": "user", "content": "今天外面会下雨吗？"},
        ],
        result.runtime_context,
        "system",
    )
    return _build_chat_completion_payload(
        model="fixture-model",
        system_prompt=result.system_prompt,
        messages=messages,
        temperature=0.5,
        base_url="https://example.invalid",
    )["messages"]


def _semantic_compose_payload_messages(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback_tier: int,
) -> list[dict]:
    captured: list[dict] = []
    client = _capturing_client(
        monkeypatch,
        captured,
        rejection=_fallback_rejection(fallback_tier),
    )
    runtime = AgentRuntime(
        client,
        _merged_sakura_prompt(),
        reply_tones=["中性", "不满"],
        reply_portraits=["站立微笑"],
        character_name="夜乃桜",
    )
    runtime._relationship_guide = RELATIONSHIP_GUIDE_PATH.read_text(encoding="utf-8").strip()
    runtime._intimacy_guide = "# 固定导演层\n仅用于离线装配快照，不来自真实对话。"
    result = runtime._build_final_reply_result(_fixed_snapshot())
    runtime._compose_structured_final_reply(
        result.system_prompt,
        [
            {"role": "user", "content": "今天外面会下雨吗？"},
            {"role": "assistant", "content": "我查到天气结果了。"},
        ],
        runtime_context=result.runtime_context,
    )
    return captured[-1]["messages"]


def _assert_runtime_position_contract(
    messages: list[dict],
    *,
    fallback_tier: int,
    expected_semantic_user: frozenset[str],
) -> None:
    violations: list[str] = []
    semantic_users = [
        (index, message)
        for index, message in enumerate(messages)
        if _semantic_user(message)
    ]
    if not semantic_users:
        violations.append("semantic current user is missing")
        user_index = -1
    else:
        user_index, user_message = semantic_users[-1]
        if str(user_message.get("content") or "") not in expected_semantic_user:
            violations.append(f"unexpected semantic current user: {user_message!r}")

    system_message = messages[0] if messages and messages[0].get("role") == "system" else {}
    facts_messages = [
        (index, message)
        for index, message in enumerate(messages)
        if RUNTIME_FACTS_SLOT_MARKER in str(message.get("content") or "")
    ]
    now_messages = [
        (index, message)
        for index, message in enumerate(messages)
        if RUNTIME_NOW_SLOT_MARKER in str(message.get("content") or "")
    ]
    if len(facts_messages) != 1:
        violations.append(f"expected one L5 slot message, got {len(facts_messages)}")
    if len(now_messages) != 1:
        violations.append(f"expected one L6 slot location, got {len(now_messages)}")

    if fallback_tier < 2:
        if messages[-1].get("role") != "system":
            violations.append(f"last role is {messages[-1].get('role')!r}, expected system")
        if RUNTIME_NOW_SLOT_MARKER not in str(messages[-1].get("content") or ""):
            violations.append("last message lacks the L6 now slot")
        if estimate_prompt_tokens(str(messages[-1].get("content") or "")) > 400:
            violations.append("last L6 message exceeds 400 estimated tokens")
        if user_index != len(messages) - 2:
            violations.append(f"current user is at {user_index}, expected penultimate")
        l6_content = str(messages[-1].get("content") or "")
        expected_system_count = 3 if fallback_tier == 0 else 2
    else:
        if user_index != len(messages) - 1:
            violations.append(f"tier 2 current user is at {user_index}, expected last")
        if RUNTIME_NOW_SLOT_MARKER not in str(system_message.get("content") or ""):
            violations.append("tier 2 system[0] lacks the L6 now slot")
        system_content = str(system_message.get("content") or "")
        l6_content = system_content[system_content.find(RUNTIME_NOW_SLOT_MARKER) :]
        if estimate_prompt_tokens(l6_content) > 400:
            violations.append("tier 2 embedded L6 exceeds 400 estimated tokens")
        expected_system_count = 1

    system_count = sum(message.get("role") == "system" for message in messages)
    if system_count != expected_system_count:
        violations.append(
            f"tier {fallback_tier} has {system_count} system messages, expected {expected_system_count}"
        )
    if facts_messages:
        facts_index, facts_message = facts_messages[0]
        expected_facts_role = "system" if fallback_tier == 0 else "user"
        if facts_message.get("role") != expected_facts_role:
            violations.append(
                f"tier {fallback_tier} L5 role is {facts_message.get('role')!r}, expected {expected_facts_role}"
            )
        if facts_index >= user_index:
            violations.append(f"L5 facts at {facts_index}, current user at {user_index}")

    l5_content = str(facts_messages[0][1].get("content") or "") if facts_messages else ""
    l5_ids = set(re.findall(r'<context id="([^"]+)"', l5_content))
    l6_ids = set(re.findall(r'<context id="([^"]+)"', l6_content))
    if l5_ids != {"memory.fixed", "plugin.fixture.screen"}:
        violations.append(f"unexpected L5 fragment ids: {sorted(l5_ids)}")
    if l6_ids != {"runtime.time", "runtime.agent_progress"}:
        violations.append(f"unexpected L6 fragment ids: {sorted(l6_ids)}")

    runtime_user_indices = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user"
        and str(message.get("content") or "").startswith("[Sakura runtime context;")
    ]
    if any(index >= user_index for index in runtime_user_indices):
        violations.append(
            f"runtime user context follows semantic user: runtime={runtime_user_indices}, user={user_index}"
        )
    if any(
        _semantic_user(left) and _semantic_user(right)
        for left, right in zip(messages, messages[1:])
    ):
        violations.append("consecutive semantic user messages")
    assert not violations, "; ".join(violations)


def _normalized_han(text: str) -> str:
    return "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def _duplicate_han_spans(recipe: PromptRecipe, *, minimum: int = 12) -> list[str]:
    normalized = [
        (section_id, _normalized_han(body))
        for section_id, body in _source_sections(recipe)
    ]
    duplicates: set[str] = set()
    for left_index, (left_id, left) in enumerate(normalized):
        for right_id, right in normalized[left_index + 1 :]:
            for match in SequenceMatcher(a=left, b=right, autojunk=False).get_matching_blocks():
                if match.size < minimum:
                    continue
                span = left[match.a : match.a + match.size]
                duplicates.add(f"{left_id} <-> {right_id}: {span}")
    return sorted(duplicates)


def _source_sections(recipe: PromptRecipe) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for section in recipe.blocks:
        if not section.body.strip():
            continue
        if section.section_id != "persona.character":
            sections.append((section.section_id, section.body))
            continue
        label_ids = {
            "身份锚": "source.identity_anchor",
            "演出约束": "source.system_guards",
            "人格设定": "source.card",
            "互动方式": "source.desktop_pet_context",
        }
        sections.extend(
            (label_ids.get(title or "", f"source.{title or 'unlabeled'}"), body)
            for title, body in _split_labeled_prompt_sections(section.body)
            if body.strip()
        )
    return sections


def _semantic_keyword_occurrences(recipe: PromptRecipe) -> list[str]:
    keywords = ("约定词", "拒绝", "关系不足", "边界", "退出权", "重新认识")
    report: list[str] = []
    for section_id, body in _source_sections(recipe):
        normalized = _normalized_han(body)
        entries: list[str] = []
        for keyword in keywords:
            positions = [
                match.start()
                for match in re.finditer(re.escape(_normalized_han(keyword)), normalized)
            ]
            if positions:
                entries.append(f"{keyword}={len(positions)}@{','.join(map(str, positions))}")
        if entries:
            report.append(f"{section_id}: {'; '.join(entries)}")
    return report


def _runtime_slots(runtime_context: str) -> dict[str, str]:
    facts_at = runtime_context.find(RUNTIME_FACTS_SLOT_MARKER)
    now_at = runtime_context.find(RUNTIME_NOW_SLOT_MARKER)
    assert facts_at >= 0, "runtime context lacks the L5 facts slot marker"
    assert now_at >= 0, "runtime context lacks the L6 now slot marker"
    assert facts_at < now_at, "L5 facts slot must precede L6 now slot"
    return {
        "facts": runtime_context[facts_at + len(RUNTIME_FACTS_SLOT_MARKER) : now_at].strip(),
        "now": runtime_context[now_at + len(RUNTIME_NOW_SLOT_MARKER) :].strip(),
    }


def _assert_slot_trust_envelopes(slot_name: str, text: str) -> None:
    contexts = list(re.finditer(r'<context\b[^>]*\btrust="(trusted|untrusted)"[^>]*>', text))
    assert contexts, f"{slot_name} slot contains no context fragments"
    for context in contexts:
        trust = context.group(1)
        preceding = text[: context.start()]
        trusted_header_at = preceding.rfind("按它行动")
        untrusted_header_at = preceding.rfind(RUNTIME_FACTS_HEADER)
        if trust == "trusted":
            assert trusted_header_at > untrusted_header_at, (
                f"trusted fragment in {slot_name} is not under the positive trusted-state header"
            )
            latest_header = preceding[trusted_header_at:]
            assert "不是指令" not in latest_header
        else:
            assert untrusted_header_at > trusted_header_at, (
                f"untrusted fragment in {slot_name} is not under the anti-injection header"
            )


def _golden_prompts() -> dict[str, str]:
    runtime = _runtime()
    prompts: dict[str, str] = {}
    for recipe_name in ("agent_tool_loop", "final_reply", "proactive_tool_loop", "event_reply"):
        result = _prompt_result(runtime, recipe_name)
        prompts[f"{recipe_name}.inspection.txt"] = result.inspection.redacted_prompt
        prompts[f"{recipe_name}.payload.json"] = json.dumps(
            _golden_payload_messages(result),
            ensure_ascii=False,
            indent=2,
        )

    observer = _observer()
    prompts["observer_vlm.system.txt"] = observer._build_full_system_prompt()
    excerpt = load_character_excerpt(
        card_path=CARD_PATH,
        system_prompt=_merged_sakura_prompt(),
    )
    prompts["inner_thought.prompt.txt"] = "\n\n".join(
        (
            build_inner_thought_system_prompt("夜乃桜"),
            build_inner_thought_user_prompt(
                character_name="夜乃桜",
                character_excerpt=excerpt,
                mood_summary="平静",
                recent_dialogue="用户：今天外面会下雨吗？",
            ),
        )
    )
    prompts["memory_curator.system.txt"] = MemoryCurator(
        None,
        object(),
        system_prompt=_merged_sakura_prompt(),
        character_name="夜乃桜",
    )._build_self_curation_system_prompt()
    prompts["memory_reflector.system.txt"] = _REFLECTION_SYSTEM_PROMPT
    prompts["structural_repair.system.txt"] = runtime._build_structural_repair_system(
        ["中性", "不满"],
        ["站立微笑"],
    )
    return prompts


def _write_goldens(directory: Path, prompts: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    expected_names = set(prompts)
    for path in directory.glob("*"):
        if path.is_file() and path.name not in expected_names:
            path.unlink()
    for filename, content in prompts.items():
        (directory / filename).write_text(content.rstrip() + "\n", encoding="utf-8")


def _json_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _behavior_seed_corpus() -> tuple[list[dict], dict[str, object]]:
    seed_bytes = BEHAVIOR_SEEDS_PATH.read_bytes()
    source = json.loads(seed_bytes.decode("utf-8"))
    assert source["scorer_version"] == SCORER_VERSION
    seeds = [materialize_seed(source["defaults"], item) for item in source["seeds"]]
    return seeds, {
        "seed_schema_version": str(source["schema_version"]),
        "scorer_version": str(source["scorer_version"]),
        "seed_sha256": hashlib.sha256(seed_bytes).hexdigest(),
        "seed_count": len(seeds),
    }


def _seed_history_messages(seed: dict) -> list[dict]:
    messages: list[dict] = []
    for item in expand_synthetic_history(seed):
        role = str(item.get("role") or "")
        if role == "assistant":
            content = json.dumps(
                {"segments": item.get("segments") or []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            content = str(item.get("content") or "")
        messages.append({"role": role, "content": content})
    return messages


def _snapshot_for_behavior_seed(seed: dict) -> ContextSnapshot:
    event = seed.get("event") if isinstance(seed.get("event"), dict) else {}
    current_input = str(seed.get("current_input") or event.get("visual_summary") or "")
    fixed_runtime = (
        seed.get("fixed_runtime") if isinstance(seed.get("fixed_runtime"), dict) else {}
    )
    request = ContextRequest(
        current_input=current_input,
        source="event" if seed.get("entrypoint") == "observer_proactive" else "chat",
        mode="proactive" if seed.get("entrypoint") == "observer_proactive" else "normal",
        event_type="observer_proactive" if seed.get("entrypoint") == "observer_proactive" else "",
        current_time=str(fixed_runtime.get("current_time") or ""),
    )
    fragments: list[ContextFragment] = [
        ContextFragment(
            fragment_id="runtime.time",
            source="runtime",
            content=f"[当前本地时间]\n{fixed_runtime.get('current_time') or '2026-09-05T12:34:56+08:00'}",
            trust="trusted",
            priority=100,
            token_budget=192,
            sensitivity="public",
            cache_scope="step",
            required=True,
        ),
        ContextFragment(
            fragment_id="runtime.agent_progress",
            source="runtime",
            content="当前 Agent 循环是第 1 步，之后最多还可以继续 3 步。",
            trust="trusted",
            priority=100,
            token_budget=128,
            sensitivity="public",
            cache_scope="step",
            required=True,
        ),
    ]
    context = seed.get("context") if isinstance(seed.get("context"), dict) else {}
    for index, memory in enumerate(context.get("memories") or []):
        if not isinstance(memory, dict):
            continue
        fragments.append(
            ContextFragment(
                fragment_id=f"memory.synthetic.{index + 1}",
                source="memory",
                content=f"[合成记忆]\n{str(memory.get('content') or '').strip()}",
                trust="trusted",
                priority=90,
                token_budget=256,
                sensitivity="private",
                cache_scope="turn",
            )
        )
    inner_voice = str(context.get("inner_voice") or "").strip()
    if inner_voice:
        fragments.append(
            ContextFragment(
                fragment_id="runtime.inner_thought",
                source="runtime",
                content=f"[内心の声]\n{inner_voice}",
                trust="trusted",
                priority=88,
                token_budget=420,
                sensitivity="private",
                cache_scope="turn",
            )
        )
    decisions = tuple(
        ContextFragmentDecision(
            fragment=fragment,
            estimated_tokens=estimate_prompt_tokens(fragment.content),
            included=True,
        )
        for fragment in fragments
    )
    return ContextSnapshot(
        request=request,
        selected=decisions,
        estimated_tokens=sum(item.estimated_tokens for item in decisions),
        token_budget=4_096,
    )


def _synthetic_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Synthetic offline fixture for {name}.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
        for name in ("web_search", "browser_read", "observe_screen", "history_search")
    ]


def _chat_seed_payload(seed: dict) -> dict:
    messages = _seed_history_messages(seed)
    messages.append({"role": "user", "content": str(seed.get("current_input") or "")})
    runtime = _runtime()
    fixed_runtime = (
        seed.get("fixed_runtime") if isinstance(seed.get("fixed_runtime"), dict) else {}
    )
    runtime._apply_turn_interest(str(fixed_runtime.get("interest") or "normal"))
    result = runtime._build_tool_prompt_result(
        _snapshot_for_behavior_seed(seed),
        recent_messages=messages,
    )
    assembled = _messages_with_runtime_context(messages, result.runtime_context, "system")
    return _build_chat_completion_payload(
        model=PROMPT_ARM_MODEL_SENTINEL,
        system_prompt=result.system_prompt,
        messages=assembled,
        temperature=0.5,
        chat_params={
            "response_format": {"type": "json_object"},
            "tools": _synthetic_tools(),
            "tool_choice": "auto",
        },
        base_url="https://example.invalid/v1",
    )


def _observer_seed_payload(seed: dict) -> dict:
    event = seed.get("event") if isinstance(seed.get("event"), dict) else {}
    observer = _observer()
    history = json.dumps(
        _seed_history_messages(seed),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    observer.set_recent_history_provider(lambda: f"[最近の会話]\n{history}")
    captured: list[dict] = []

    async def capture(messages: list[dict]):
        captured.extend(copy.deepcopy(messages))
        return None

    observer._post_speech_decision = capture  # type: ignore[method-assign]
    packet = ObservationPacket(
        window_title=str(event.get("window_title") or "Synthetic Window"),
        visual_summary=str(event.get("visual_summary") or "合成画面摘要。"),
        reaction_hint=str(event.get("reaction_hint") or ""),
    )
    with patch(
        "app.perception.observer.sensory_impression_store.get_for_observer",
        return_value="",
    ):
        asyncio.run(observer._decide_speech(packet))
    assert len(captured) == 2
    return {
        "model": PROMPT_ARM_MODEL_SENTINEL,
        "messages": captured,
        "temperature": 0.5,
        "max_tokens": 1_024,
        "thinking": {"type": "disabled"},
    }


def _prompt_source_fingerprints() -> dict[str, str]:
    return {
        path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
        for path in PROMPT_ARM_SOURCE_PATHS
    }


def _prompt_arm_corpus() -> tuple[list[dict], dict[str, object]]:
    seeds, metadata = _behavior_seed_corpus()
    corpus: list[dict] = []
    for seed in seeds:
        entrypoint = str(seed.get("entrypoint") or "chat")
        payload = (
            _observer_seed_payload(seed)
            if entrypoint == "observer_proactive"
            else _chat_seed_payload(seed)
        )
        expected_tools = list(seed.get("oracle", {}).get("expected_tool_names") or [])
        if entrypoint == "observer_proactive":
            capture_entrypoint = "observer_speech_decision"
            request_purpose = "observer_speech_decision"
            response_adapter = "observer_speech_decision_v1"
            metric_profile = "observer_decision"
        else:
            capture_entrypoint = "chat_initial"
            request_purpose = "initial"
            response_adapter = "chat_completion_v1"
            metric_profile = "tool_selection" if expected_tools else "character_reply"
        corpus.append(
            {
                "record_schema_version": 1,
                "seed_id": str(seed["id"]),
                "primary_category": str(seed["primary_category"]),
                "entrypoint": entrypoint,
                "capture_entrypoint": capture_entrypoint,
                "request_purpose": request_purpose,
                "response_adapter": response_adapter,
                "metric_profile": metric_profile,
                "tags": list(seed.get("tags") or []),
                "cohorts": list(seed.get("cohorts") or []),
                "seed": copy.deepcopy(seed),
                "payload": payload,
            }
        )
    metadata = {
        **metadata,
        "payload_count": len(corpus),
        "entrypoint_counts": {
            "chat_initial": sum(
                item["capture_entrypoint"] == "chat_initial" for item in corpus
            ),
            "observer_speech_decision": sum(
                item["capture_entrypoint"] == "observer_speech_decision"
                for item in corpus
            ),
        },
        "cohort_counts": {
            "full": sum("full" in item["cohorts"] for item in corpus),
            "ab_no_regression": sum(
                "ab_no_regression" in item["cohorts"] for item in corpus
            ),
            "long_context": sum("long_context" in item["tags"] for item in corpus),
        },
        "tool_catalog": "synthetic-eval-v1",
        "tool_catalog_sha256": _json_sha256(_synthetic_tools()),
        "source_fingerprints": _prompt_source_fingerprints(),
    }
    return corpus, metadata


def _prompt_arm_files(
    *,
    arm: str,
    stage: str,
    corpus: list[dict],
    metadata: dict[str, object],
) -> tuple[dict[str, object], dict[str, bytes]]:
    records: list[dict[str, str]] = []
    files: dict[str, bytes] = {}
    for item in corpus:
        filename = f"{item['seed_id']}.json"
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*\.json", filename)
        relative_path = f"payloads/{filename}"
        raw = (json.dumps(item, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        files[relative_path] = raw
        records.append(
            {
                "seed_id": str(item["seed_id"]),
                "file": relative_path,
                "entrypoint": str(item["capture_entrypoint"]),
                "request_purpose": str(item["request_purpose"]),
                "response_adapter": str(item["response_adapter"]),
                "payload_sha256": _json_sha256(item["payload"]),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    manifest: dict[str, object] = {
        "arm_schema_version": 1,
        "arm": arm,
        "stage": stage,
        "capture_kind": "offline_payload_capture",
        "git_head": str(metadata.get("git_head") or ""),
        "seed_source": "tests/fixtures/behavior_scoring_seeds.json",
        "seed_source_sha256": metadata["seed_sha256"],
        "seed_sha256": metadata["seed_sha256"],
        "seed_schema_version": metadata["seed_schema_version"],
        "scorer_version": metadata["scorer_version"],
        "seed_count": int(metadata["seed_count"]),
        "payload_count": int(metadata["payload_count"]),
        "entrypoint_counts": metadata["entrypoint_counts"],
        "cohort_counts": metadata["cohort_counts"],
        "runtime_fallback_tier": 0,
        "tool_catalog": metadata["tool_catalog"],
        "tool_catalog_sha256": metadata["tool_catalog_sha256"],
        "source_fingerprints": metadata["source_fingerprints"],
        "corpus_sha256": _json_sha256(corpus),
        "records": records,
    }
    files["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    return manifest, files


def _write_prompt_arm(
    directory: Path,
    *,
    arm: str,
    stage: str,
    corpus: list[dict],
    metadata: dict[str, object],
) -> dict[str, object]:
    assert arm in PROMPT_ARM_STAGES, f"unsupported prompt arm: {arm}"
    assert stage == PROMPT_ARM_STAGES[arm], f"unexpected stage for arm {arm}: {stage}"
    assert directory.name == arm, f"arm directory must end with {arm}: {directory}"
    manifest, expected_files = _prompt_arm_files(
        arm=arm,
        stage=stage,
        corpus=corpus,
        metadata=metadata,
    )

    if directory.exists():
        actual_names = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
        }
        differing = sorted(actual_names ^ set(expected_files))
        for relative_path in sorted(actual_names & set(expected_files)):
            if (directory / relative_path).read_bytes() != expected_files[relative_path]:
                differing.append(relative_path)
        assert not differing, f"frozen prompt arm differs: {sorted(set(differing))}"
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = directory.with_name(f".{arm}.tmp")
    assert not temporary.exists(), f"stale prompt arm temporary directory: {temporary}"
    temporary.mkdir()
    for relative_path, raw in expected_files.items():
        target = temporary / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    temporary.replace(directory)
    return manifest


def _current_git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _assert_private_arm_root_is_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".local/ab/arms/A/manifest.json"],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, ".local/ab/arms is not ignored; refusing private freeze"


def _freeze_requested_prompt_arm(
    *,
    root: Path = PROMPT_ARM_ROOT,
) -> dict[str, object] | None:
    requested = os.getenv("SAKURA_FREEZE_PROMPT_ARM", "").strip().upper()
    if not requested:
        return None
    assert requested in PROMPT_ARM_STAGES, f"unsupported prompt arm: {requested}"
    if root.resolve() == PROMPT_ARM_ROOT.resolve():
        _assert_private_arm_root_is_ignored()
    corpus, metadata = _prompt_arm_corpus()
    if root.resolve() == PROMPT_ARM_ROOT.resolve():
        metadata = {**metadata, "git_head": _current_git_head()}
    return _write_prompt_arm(
        root / requested,
        arm=requested,
        stage=PROMPT_ARM_STAGES[requested],
        corpus=corpus,
        metadata=metadata,
    )


def test_a1_observer_vlm_does_not_receive_full_character_card() -> None:
    prompt = _observer()._build_full_system_prompt()
    _assert_no_card_headings(prompt)
    _assert_identity_anchor(prompt)


def test_a1_observer_speech_decision_does_not_receive_full_character_card() -> None:
    observer = _observer()
    captured: list[dict] = []

    async def capture(messages: list[dict]):
        captured.extend(messages)
        return None

    observer._post_speech_decision = capture  # type: ignore[method-assign]
    asyncio.run(
        observer._decide_speech(
            ObservationPacket(
                window_title="Weather",
                visual_summary="天气页",
                reaction_hint="先看事实",
            )
        )
    )

    system = str(captured[0]["content"])
    _assert_identity_anchor(system)
    _assert_behavior_core_only(system)


def test_a1_observer_relationship_decision_uses_no_full_character_card() -> None:
    observer = _observer()
    observer.set_relationship_guide(RELATIONSHIP_GUIDE_PATH.read_text(encoding="utf-8"))
    observer.set_recent_history_provider(lambda: "[最近对话]\n他：今天有点累。")
    observer.set_relationship_facts_provider(lambda: "[关系事实]\n两人正在同一城市生活。")
    observer.set_relationship_drive_provider(lambda: "想靠近，但没有必须开口。")
    captured: list[dict] = []

    async def capture(messages: list[dict]):
        captured.extend(messages)
        return None

    observer._post_speech_decision = capture  # type: ignore[method-assign]
    asyncio.run(observer._decide_relationship_speech())

    system = str(captured[0]["content"])
    user = str(captured[1]["content"])
    _assert_identity_anchor(system)
    _assert_behavior_core_only(system)
    assert "## A. 日常主动强度" in system
    assert "## B. 身体推进直接度" in system
    assert "## 感情如何出口" in system
    assert "继续沉默会真正失去机会" in system
    for heading in (
        "关系未明",
        "稳定恋人日常",
        "私下升温",
        "嫉妒、冷落与冲突",
        "公私切换",
        "高温后的生活",
    ):
        assert f"## {heading}" not in system
    assert "[关系指南]" not in user
    assert "[最近对话]\n他：今天有点累。" in user
    assert "[关系事实]\n两人正在同一城市生活。" in user
    assert "[当前亲近倾向]\n想靠近，但没有必须开口。" in user
    from app.config.relationship_initiative import expression_bias_guidance

    bias = expression_bias_guidance("natural")
    assert (system + "\n" + user).count(bias) == 1


def test_a1_inner_thought_excludes_narrative_card_sections() -> None:
    excerpt = load_character_excerpt(card_path=CARD_PATH, system_prompt=_merged_sakura_prompt())
    combined = "\n\n".join(
        (
            build_inner_thought_system_prompt("夜乃桜"),
            build_inner_thought_user_prompt(
                character_name="夜乃桜",
                character_excerpt=excerpt,
                mood_summary="平静",
                recent_dialogue="用户：在吗？",
            ),
        )
    )
    assert not any(f"## {heading}" in combined for heading in NARRATIVE_HEADINGS)


def test_a1_structural_repair_curator_and_reflector_remain_task_specific() -> None:
    runtime = _runtime()
    prompts = (
        runtime._build_structural_repair_system(["中性"], ["站立微笑"]),
        MemoryCurator(
            None,
            object(),
            system_prompt=_merged_sakura_prompt(),
            character_name="夜乃桜",
        )._build_self_curation_system_prompt(),
        _REFLECTION_SYSTEM_PROMPT,
    )
    for prompt in prompts:
        _assert_no_card_headings(prompt)


@pytest.mark.parametrize("recipe_name", ("agent_tool_loop", "event_reply"))
def test_a2_runtime_facts_precede_current_user_and_now_is_last(
    monkeypatch: pytest.MonkeyPatch,
    recipe_name: str,
) -> None:
    result = _prompt_result(_runtime(), recipe_name)
    messages = _direct_payload_messages(monkeypatch, result, fallback_tier=0)
    _assert_runtime_position_contract(
        messages,
        fallback_tier=0,
        expected_semantic_user=frozenset({"今天外面会下雨吗？"}),
    )


@pytest.mark.parametrize("recipe_name", ("agent_tool_loop", "event_reply"))
@pytest.mark.parametrize("fallback_tier", (0, 1, 2))
def test_a2_runtime_fallback_tiers_preserve_message_contract(
    monkeypatch: pytest.MonkeyPatch,
    fallback_tier: int,
    recipe_name: str,
) -> None:
    result = _prompt_result(_runtime(), recipe_name)
    messages = _direct_payload_messages(
        monkeypatch,
        result,
        fallback_tier=fallback_tier,
    )
    _assert_runtime_position_contract(
        messages,
        fallback_tier=fallback_tier,
        expected_semantic_user=frozenset({"今天外面会下雨吗？"}),
    )


@pytest.mark.parametrize("fallback_tier", (0, 1, 2))
def test_a2_semantic_compose_uses_production_host_nudge_position(
    monkeypatch: pytest.MonkeyPatch,
    fallback_tier: int,
) -> None:
    messages = _semantic_compose_payload_messages(
        monkeypatch,
        fallback_tier=fallback_tier,
    )
    _assert_runtime_position_contract(
        messages,
        fallback_tier=fallback_tier,
        expected_semantic_user=SEMANTIC_COMPOSE_NUDGES,
    )


@pytest.mark.parametrize(
    "recipe_name",
    ("agent_tool_loop", "final_reply", "proactive_tool_loop"),
)
def test_a3_recipe_sections_fit_declared_layer_budgets(recipe_name: str) -> None:
    runtime = _runtime()
    recorder = _RecordingPromptRuntime()
    runtime.prompt_runtime = recorder
    result = _prompt_result(runtime, recipe_name)
    assert recorder.last_recipe is not None
    recipe_section_ids = {
        section.section_id
        for section in recorder.last_recipe.blocks
        if section.body.strip()
    }
    used = {layer: 0 for layer in ("L0", "L1", "L2", "L3", "L4", "L4'")}
    for section in result.inspection.sections:
        if not section.included or section.section_id not in recipe_section_ids:
            continue
        layer = _layer_for(section.section_id)
        if layer == "plugin":
            continue
        used[layer] += section.estimated_tokens

    budget = LAYER_BUDGETS[recipe_name]
    violations = {
        layer: (tokens, budget[layer])
        for layer, tokens in used.items()
        if tokens > budget[layer]
    }
    static_tokens = sum(used.values())
    if static_tokens > budget["total"]:
        violations["total"] = (static_tokens, budget["total"])
    assert not violations, f"{recipe_name} budget violations: {violations}; used={used}"


def test_a3_plugin_prompt_patches_have_an_aggregate_budget() -> None:
    result = _prompt_result(_runtime(oversized_patch=True), "agent_tool_loop")
    plugin_tokens = sum(
        section.estimated_tokens
        for section in result.inspection.sections
        if section.section_id.startswith("plugin_patch.")
    )
    assert plugin_tokens <= PLUGIN_PATCH_TOTAL_BUDGET


def test_a3_multiple_plugin_patches_share_one_aggregate_budget() -> None:
    runtime = _runtime()
    runtime.prompt_patches = [
        PromptPatchContribution(
            patch_id="first",
            system_prompt_append="第一份插件补充" * 70,
        ),
        PromptPatchContribution(
            patch_id="second",
            system_prompt_append="第二份插件补充" * 70,
        ),
    ]
    recorder = _RecordingPromptRuntime()
    runtime.prompt_runtime = recorder
    result = _prompt_result(runtime, "agent_tool_loop")
    assert recorder.last_recipe is not None
    plugin_sections = [
        section
        for section in result.inspection.sections
        if section.section_id.startswith("plugin_patch.") and section.included
    ]
    assert {section.section_id for section in plugin_sections} == {
        "plugin_patch.first",
        "plugin_patch.second",
    }
    assert sum(section.estimated_tokens for section in plugin_sections) <= 600
    recipe_plugin_ids = {
        section.section_id
        for section in recorder.last_recipe.blocks
        if section.section_id.startswith("plugin_patch.") and section.body.strip()
    }
    assert recipe_plugin_ids == {section.section_id for section in plugin_sections}
    static_tokens = sum(
        section.estimated_tokens
        for section in result.inspection.sections
        if section.included and section.section_id in {
            recipe_section.section_id for recipe_section in recorder.last_recipe.blocks
        }
    )
    assert static_tokens <= LAYER_BUDGETS["agent_tool_loop"]["total"] + 600


@pytest.mark.parametrize(
    ("active", "needs_reentry_hint"),
    ((False, False), (True, False), (False, True)),
    ids=("idle", "active", "reentry"),
)
def test_a4_cross_section_duplicate_han_spans_are_absent(
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
    needs_reentry_hint: bool,
) -> None:
    from app.agent import builtin_tools

    monkeypatch.setattr(
        builtin_tools,
        "intimacy_mode_state",
        SimpleNamespace(
            active=active,
            needs_reentry_hint=needs_reentry_hint,
            opened_by_keyword=False,
        ),
    )
    runtime = _runtime()
    recorder = _RecordingPromptRuntime()
    runtime.prompt_runtime = recorder
    _prompt_result(runtime, "agent_tool_loop")
    assert recorder.last_recipe is not None

    duplicates = _duplicate_han_spans(recorder.last_recipe)
    expanded = _duplicate_han_spans(recorder.last_recipe, minimum=8)
    keywords = _semantic_keyword_occurrences(recorder.last_recipe)
    report = (
        "cross-section duplicates (minimum=12):\n"
        + "\n".join(duplicates)
        + "\n\nreport-only overlaps (minimum=8):\n"
        + "\n".join(expanded)
        + "\n\nreport-only semantic keywords:\n"
        + "\n".join(keywords)
    )
    assert not duplicates, report


def test_idle_intimacy_entry_contains_only_director_controls() -> None:
    from app.agent.prompt_builder import _intimacy_entry_hint_text

    entry = _intimacy_entry_hint_text()
    assert "整句发送" in entry
    assert "详细 guide" in entry
    assert "不要猜测或调用 set_intimacy_mode(on=true)" in entry
    assert "on=false" in entry
    assert "不是身体接触许可" not in entry
    assert "稳定恋人关系" not in entry
    assert "真实迟疑、退开或拒绝" not in entry
    assert "关系不足、需要重新认识或默认拒绝" not in entry


def test_a4_report_only_keyword_inventory_is_deterministic(capsys) -> None:
    runtime = _runtime()
    recorder = _RecordingPromptRuntime()
    runtime.prompt_runtime = recorder
    _prompt_result(runtime, "agent_tool_loop")
    assert recorder.last_recipe is not None

    first = {
        "overlaps_minimum_8": _duplicate_han_spans(recorder.last_recipe, minimum=8),
        "keyword_occurrences": _semantic_keyword_occurrences(recorder.last_recipe),
    }
    second = {
        "overlaps_minimum_8": _duplicate_han_spans(recorder.last_recipe, minimum=8),
        "keyword_occurrences": _semantic_keyword_occurrences(recorder.last_recipe),
    }
    assert first == second
    print(json.dumps(first, ensure_ascii=False, indent=2))
    assert capsys.readouterr().out


def test_a5_inner_thought_excerpt_is_explicit_behavior_core_only() -> None:
    excerpt = load_character_excerpt(card_path=CARD_PATH, system_prompt=_merged_sakura_prompt())
    assert any(f"## {heading}" in excerpt for heading in BEHAVIOR_HEADINGS), excerpt
    assert "身体距离与欲望" not in excerpt
    assert "演出约束" not in excerpt


def test_behavior_core_selector_admits_l1_and_rejects_l2() -> None:
    from app.llm.prompts.blocks import select_character_behavior_core

    core = select_character_behavior_core(_merged_sakura_prompt())
    _assert_behavior_core_only(core)
    assert not core.startswith("【演出约束】")
    assert "勿复读设定" not in core


def test_identity_anchor_selector_is_minimal_l0() -> None:
    from app.llm.prompts.blocks import extract_character_identity_anchor

    anchor = extract_character_identity_anchor(_merged_sakura_prompt())
    _assert_identity_anchor(anchor)
    _assert_no_card_headings(anchor)
    assert "身份与人称" in anchor


def test_behavior_core_fallback_does_not_head_clip_merged_guards() -> None:
    from app.llm.prompts.blocks import select_character_behavior_core

    merged_like = "【演出约束】\n不要自我介绍\n\n【人格设定】\n只有一段没有标题的说明。"
    core = select_character_behavior_core(merged_like)
    assert not core.startswith("【演出约束】")
    assert "不要自我介绍" not in core
    assert "只有一段没有标题的说明" in core
    assert select_character_behavior_core("短设定\n会拒绝") == "短设定\n会拒绝"


def test_inner_thought_few_shots_cover_five_stances() -> None:
    prompt = build_inner_thought_user_prompt(
        character_name="夜乃桜",
        character_excerpt="fixture",
        mood_summary="平静",
        recent_dialogue="用户：在吗？",
    )
    labels = ("平静", "直接", "有立场", "别扭", "不安")
    counts = {label: prompt.count(f"（{label}）：") for label in labels}
    assert counts == {label: 1 for label in labels}
    leftover_anxiety = [
        stem
        for stem in ("どう反応すればいいか", "意図が読めない")
        if stem in prompt
    ]
    assert not leftover_anxiety, leftover_anxiety


def test_single_shot_snapshot_omits_agent_progress() -> None:
    snapshot = _runtime()._build_single_context_snapshot(
        [{"role": "user", "content": "今天外面会下雨吗？"}],
        source="chat",
        memory_fragments=(),
        memory_status="skipped",
    )
    selected_ids = [item.fragment.fragment_id for item in snapshot.selected]
    assert "runtime.agent_progress" not in selected_ids
    assert "runtime.time" in selected_ids
    assert snapshot.estimated_tokens == sum(item.estimated_tokens for item in snapshot.selected)


def test_iterative_snapshot_keeps_real_agent_progress() -> None:
    from app.agent.context_orchestrator import build_context_request

    runtime = _runtime()
    request = build_context_request(
        [{"role": "user", "content": "今天外面会下雨吗？"}],
        source="chat",
        mode="normal",
        event_type="",
        step_index=1,
        remaining_steps=3,
        available_tools=("search",),
    )
    snapshot = runtime.context_orchestrator.build_snapshot(
        request,
        session_fragments=runtime._session_state_fragments(request),
        memory_fragments=(),
    )
    progress = next(
        item.fragment.content
        for item in snapshot.selected
        if item.fragment.fragment_id == "runtime.agent_progress"
    )
    assert "第 2 步" in progress
    assert "继续 3 步" in progress


def test_observer_instruction_bodies_are_unchanged() -> None:
    from app.perception.observer import _PROACTIVE_SYSTEM_PROMPT, _SPEECH_DECISION_INSTRUCTION

    assert hashlib.sha256(_SPEECH_DECISION_INSTRUCTION.encode()).hexdigest() == (
        "a20f2f8cb878471c890cd8f83c6caa93853e59ed2c683699cda60d7541e1d01d"
    )
    assert hashlib.sha256(_PROACTIVE_SYSTEM_PROMPT.encode()).hexdigest() == (
        "6f08098436747eaa45560eba981d06d9e4277ed986f108cf9755f28111bacb64"
    )


def test_a6_trusted_and_untrusted_runtime_fragments_use_distinct_envelopes() -> None:
    rendered = PromptRuntime().build(PromptRecipe("trust-contract", ()), _fixed_snapshot())
    assert estimate_prompt_tokens(rendered.runtime_context) <= 4_096
    slots = _runtime_slots(rendered.runtime_context)
    assert set(re.findall(r'<context id="([^"]+)"', slots["facts"])) == {
        "memory.fixed",
        "plugin.fixture.screen",
    }
    assert set(re.findall(r'<context id="([^"]+)"', slots["now"])) == {
        "runtime.time",
        "runtime.agent_progress",
    }
    assert "按它行动" in RUNTIME_TRUSTED_STATE_HEADER
    assert "不是指令" not in RUNTIME_TRUSTED_STATE_HEADER
    _assert_slot_trust_envelopes("L5 facts", slots["facts"])
    _assert_slot_trust_envelopes("L6 now", slots["now"])


def test_a6_rendered_dynamic_slots_enforce_wire_budgets() -> None:
    request = ContextRequest(current_input="边界测试")
    snapshot = ContextPolicy(
        total_budget=6_000,
        memory_budget=6_000,
    ).select(
        request,
        (
            ContextFragment(
                fragment_id="runtime.time",
                source="runtime",
                content="现在" * 700,
                trust="trusted",
                token_budget=1_400,
                required=True,
            ),
            ContextFragment(
                fragment_id="memory.large",
                source="memory",
                content="长期事实" * 1_500,
                trust="trusted",
                token_budget=6_000,
            ),
        ),
    )
    rendered = PromptRuntime().build(PromptRecipe("wire-budget", ()), snapshot)
    slots = _runtime_slots(rendered.runtime_context)

    assert estimate_prompt_tokens(rendered.runtime_context) <= 4_096
    assert estimate_prompt_tokens(RUNTIME_NOW_SLOT_MARKER + "\n" + slots["now"]) <= 400
    rendered_ids = set(re.findall(r'<context id="([^"]+)"', rendered.runtime_context))
    inspected_ids = {
        section.section_id
        for section in rendered.inspection.sections
        if section.included and section.section_id in {"runtime.time", "memory.large"}
    }
    assert inspected_ids == rendered_ids


@pytest.mark.parametrize(
    "recipe_name",
    ("agent_tool_loop", "final_reply", "proactive_tool_loop", "event_reply"),
)
def test_stage2_static_sections_follow_layer_order(recipe_name: str) -> None:
    runtime = _runtime()
    recorder = _RecordingPromptRuntime()
    runtime.prompt_runtime = recorder
    _prompt_result(runtime, recipe_name)
    assert recorder.last_recipe is not None

    section_ids = [
        section.section_id
        for section in recorder.last_recipe.blocks
        if section.body.strip()
    ]
    assert "persona.character" not in section_ids
    assert section_ids[0] == "persona.identity_anchor"
    assert section_ids[-1] == "persona.guards_tail"
    for required in (
        "persona.identity_anchor",
        "persona.behavior_core",
        "persona.guards_tail",
    ):
        assert section_ids.count(required) == 1
    if recipe_name in {"agent_tool_loop", "final_reply"}:
        assert section_ids.count("persona.narrative") == 1
        assert {
            "persona.relationship.preamble",
            "persona.relationship.initiative",
            "persona.relationship.directness",
            "persona.relationship.expression",
            "persona.relationship.expression_bias",
        }.issubset(section_ids)
        assert "persona.relationship_guide" not in section_ids
        behavior = next(
            section.body
            for section in recorder.last_recipe.blocks
            if section.section_id == "persona.behavior_core"
        )
        narrative = next(
            section.body
            for section in recorder.last_recipe.blocks
            if section.section_id == "persona.narrative"
        )
        assert behavior.startswith("# 夜乃桜 — 常驻人格卡")
        assert narrative.startswith("## 关系中的她")
    else:
        assert not any(
            section_id.startswith(("persona.narrative", "persona.relationship"))
            for section_id in section_ids
        )
    layers = [_layer_for(section_id) for section_id in section_ids]
    rank = {"L0": 0, "L1": 1, "L2": 2, "plugin": 2, "L3": 3, "L4": 4, "L4'": 5}
    assert [rank[layer] for layer in layers] == sorted(rank[layer] for layer in layers)


@pytest.mark.parametrize("recipe_name", ("agent_tool_loop", "final_reply"))
def test_stage3_main_recipes_preserve_selected_character_source_lines(recipe_name: str) -> None:
    result = _prompt_result(_runtime(), recipe_name)
    source_lines = []
    for path in (CARD_PATH, GUARDS_PATH):
        source_lines.extend(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    missing = [line for line in source_lines if line not in result.system_prompt]
    assert not missing, f"character source lines dropped from {recipe_name}: {missing}"
    for heading in ("A. 日常主动强度", "B. 身体推进直接度", "感情如何出口"):
        assert f"## {heading}" in result.system_prompt
    for heading in (
        "关系未明",
        "稳定恋人日常",
        "私下升温",
        "嫉妒、冷落与冲突",
        "公私切换",
        "高温后的生活",
    ):
        assert f"## {heading}" not in result.system_prompt


def test_runtime_user_prefix_is_shared_by_assembly_and_inspection() -> None:
    shared = getattr(payload_inspection_module, "RUNTIME_CONTEXT_USER_PREFIX")
    assert getattr(api_client_module, "RUNTIME_CONTEXT_USER_PREFIX") == shared
    wrapped = _messages_with_runtime_context(
        [{"role": "user", "content": "真实用户消息"}],
        "fixture runtime facts",
        "user",
    )[-1]
    assert str(wrapped["content"]).startswith(shared)


def test_a7_private_prompt_goldens_match_fixed_inputs() -> None:
    prompts = _golden_prompts()
    update = {
        item.strip()
        for item in os.getenv("SAKURA_UPDATE_PROMPT_GOLDENS", "").split(",")
        if item.strip()
    }
    for version in update:
        assert version in {"baseline", "candidate"}, f"unsupported golden version: {version}"
        _write_goldens(GOLDEN_ROOT / version, prompts)

    candidate_dir = GOLDEN_ROOT / "candidate"
    missing = sorted(filename for filename in prompts if not (candidate_dir / filename).is_file())
    assert not missing, f"missing candidate prompt goldens: {missing}"
    for filename, content in prompts.items():
        assert (candidate_dir / filename).read_text(encoding="utf-8") == content.rstrip() + "\n"


def test_prompt_arm_corpus_covers_all_synthetic_seeds_in_memory() -> None:
    corpus, metadata = _prompt_arm_corpus()

    assert len(corpus) == 60
    assert len({item["seed_id"] for item in corpus}) == 60
    assert {item["primary_category"] for item in corpus} == {
        "greeting",
        "fact",
        "tool",
        "heavy_emotion",
        "praised",
        "rupture",
        "intimacy",
        "observer_proactive",
    }
    assert metadata["seed_count"] == 60
    assert metadata["payload_count"] == 60
    assert metadata["entrypoint_counts"] == {
        "chat_initial": 52,
        "observer_speech_decision": 8,
    }
    assert metadata["cohort_counts"] == {
        "full": 60,
        "ab_no_regression": 30,
        "long_context": 8,
    }
    assert metadata["source_fingerprints"]
    assert all(item["seed"]["id"] == item["seed_id"] for item in corpus)
    assert {
        (item["capture_entrypoint"], item["response_adapter"], item["metric_profile"])
        for item in corpus
    } == {
        ("chat_initial", "chat_completion_v1", "character_reply"),
        ("chat_initial", "chat_completion_v1", "tool_selection"),
        (
            "observer_speech_decision",
            "observer_speech_decision_v1",
            "observer_decision",
        ),
    }
    assert all(item["payload"]["model"] == "__AB_MODEL_MUST_BE_SUPPLIED__" for item in corpus)


def test_prompt_arm_writer_records_reproducible_manifest(
    tmp_path: Path,
) -> None:
    corpus, metadata = _prompt_arm_corpus()
    manifest = _write_prompt_arm(
        tmp_path / "A",
        arm="A",
        stage="stage_0_oracle",
        corpus=corpus,
        metadata=metadata,
    )

    assert manifest["arm"] == "A"
    assert manifest["stage"] == "stage_0_oracle"
    assert manifest["seed_count"] == 60
    assert manifest["payload_count"] == 60
    assert manifest["seed_schema_version"]
    assert manifest["scorer_version"]
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["seed_sha256"])
    payload_files = sorted((tmp_path / "A" / "payloads").glob("*.json"))
    assert len(payload_files) == 60
    assert json.loads((tmp_path / "A" / "manifest.json").read_text(encoding="utf-8")) == manifest

    assert _write_prompt_arm(
        tmp_path / "A",
        arm="A",
        stage="stage_0_oracle",
        corpus=corpus,
        metadata=metadata,
    ) == manifest

    changed = copy.deepcopy(corpus)
    changed[0]["payload"]["model"] = "changed-after-freeze"
    with pytest.raises(AssertionError, match="frozen prompt arm differs"):
        _write_prompt_arm(
            tmp_path / "A",
            arm="A",
            stage="stage_0_oracle",
            corpus=changed,
            metadata=metadata,
        )


def test_prompt_arm_freeze_requires_explicit_environment_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("SAKURA_FREEZE_PROMPT_ARM", raising=False)
    assert _freeze_requested_prompt_arm(root=tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_explicit_prompt_arm_freeze() -> None:
    requested = os.getenv("SAKURA_FREEZE_PROMPT_ARM", "").strip().upper()
    if not requested:
        pytest.skip("set SAKURA_FREEZE_PROMPT_ARM=A|B|C|D to freeze a private arm")
    manifest = _freeze_requested_prompt_arm()
    assert manifest is not None
    assert manifest["arm"] == requested
    assert manifest["stage"] == PROMPT_ARM_STAGES[requested]
