"""亲密互动中的关系连续性提示回归。"""

from __future__ import annotations

from pathlib import Path

from app.agent.prompt_builder import _intimacy_entry_hint_text
from app.config.character_loader import load_system_prompt
from app.llm.prompts.runtime import PromptRuntime
from app.llm.prompts.types import (
    ContextFragment,
    ContextFragmentDecision,
    ContextRequest,
    ContextSnapshot,
    PromptRecipe,
    PromptSection,
)


def _decision(fragment_id: str, content: str) -> ContextFragmentDecision:
    fragment = ContextFragment(
        fragment_id=fragment_id,
        source="memory",
        content=content,
        trust="trusted",
        required=True,
    )
    return ContextFragmentDecision(fragment, estimated_tokens=20, included=True)


def test_established_relationship_evidence_survives_mood_and_one_declined_action() -> None:
    root = Path(__file__).resolve().parents[2]
    package = root / "characters" / "Sakura"
    character_prompt = load_system_prompt(
        package / "card.md",
        system_guards_path=package / "system_guards.md",
    )
    recipe = PromptRecipe(
        name="relationship-continuity-replay",
        blocks=(
            PromptSection("persona.base", character_prompt, source="character"),
            PromptSection(
                "persona.intimacy_entry",
                _intimacy_entry_hint_text(),
                source="character",
            ),
        ),
    )
    snapshot = ContextSnapshot(
        request=ContextRequest(current_input="可以抱你吗？"),
        selected=(
            _decision("memory.core_profile", "常驻档案：我们是对等的恋人。"),
            _decision("memory.mood", "心情：他在试探我的变化，但我仍想留在他身边。"),
            _decision("memory.recent", "近期对话：上一次具体触碰，我说先等一下。"),
        ),
    )

    built = PromptRuntime().build(recipe, snapshot)
    combined = f"{built.system_prompt}\n{built.runtime_context}"

    assert "当前关系以真实积累" in built.system_prompt
    assert "一次迟疑或冲突" in built.system_prompt
    assert "复读旧防御" in built.system_prompt
    assert "我们是对等的恋人" in built.runtime_context
    assert "他在试探我的变化" in built.runtime_context
    assert "上一次具体触碰，我说先等一下" in built.runtime_context
    assert "不是身体接触许可" in combined
    assert "未开启不限制拥抱、亲吻、前戏或性行为" in combined
    assert "自然升温" in combined
    assert "关系不足" in combined
    assert "不要动手描写" not in combined
    assert "开启后才能" not in combined
    assert "你们是恋人" not in built.system_prompt
