"""system_guards 与角色系统提示组装。"""

from __future__ import annotations

from pathlib import Path

from app.config.character_loader import load_system_prompt
from app.llm.prompts.blocks import with_desktop_pet_context


def test_with_desktop_pet_context_puts_guards_before_persona() -> None:
    text = with_desktop_pet_context("人格正文", system_guards="- 勿复述战力")
    assert text.index("【演出约束】") < text.index("【人格设定】")
    assert text.index("【人格设定】") < text.index("【互动方式】")
    assert "勿复述战力" in text
    assert "人格正文" in text
    assert "数字生命" in text
    assert "对等" in text
    assert "桌宠" not in text


def test_with_desktop_pet_context_without_guards() -> None:
    text = with_desktop_pet_context("人格正文")
    assert "【演出约束】" not in text
    assert text.startswith("【人格设定】")


def test_desktop_pet_context_includes_chinese_pragmatics() -> None:
    from app.llm.prompts.blocks import DESKTOP_PET_CONTEXT

    text = with_desktop_pet_context("人格正文")
    assert "网络缩略" in DESKTOP_PET_CONTEXT
    assert "当下语境" in DESKTOP_PET_CONTEXT
    assert "网络缩略" in text
    assert "自然确认" in text
    assert "人格设定" in DESKTOP_PET_CONTEXT
    assert "不必把自己演成只会帮忙的助手" in DESKTOP_PET_CONTEXT


def test_segment_protocol_follows_real_mood_not_default_neutral() -> None:
    from app.llm.prompts.recipes import build_segmented_reply_instruction

    text = build_segmented_reply_instruction(["中性", "不满"])
    assert "优先选择中性" not in text
    assert "真实心情" in text
    assert "不必为了稳妥默认成中性" in text


def test_memory_honesty_rule_is_positive_framed() -> None:
    import inspect

    from app.agent.runtime import AgentRuntime

    src = inspect.getsource(AgentRuntime._build_tool_prompt_result)
    assert "流行梗" not in src
    assert "记忆诚实" in src
    assert "只依据已注入片段与 memory_search/detail" in src
    assert "禁止编造共同经历或熟人关系" in src
    assert "按当下语境理解" in src


def test_load_system_prompt_includes_guards(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    guards = tmp_path / "system_guards.md"
    card.write_text("她是夜乃桜。", encoding="utf-8")
    guards.write_text("- 不要每轮自称生徒会長", encoding="utf-8")
    prompt = load_system_prompt(card, system_guards_path=guards)
    assert "【演出约束】" in prompt
    assert "不要每轮自称生徒会長" in prompt
    assert "【人格设定】" in prompt
    assert "她是夜乃桜。" in prompt
    assert prompt.index("【演出约束】") < prompt.index("【人格设定】")


def test_sakura_package_loads_guards_in_chain() -> None:
    """生产角色包：card + system_guards 按清单进入同款加载函数。"""
    import json

    root = Path(__file__).resolve().parents[2]
    package = root / "characters" / "Sakura"
    manifest_path = package / "character.json"
    if not manifest_path.exists():
        return
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw.get("system_guards") == "system_guards.md"
    card_path = package / str(raw["card"])
    guards_path = package / str(raw["system_guards"])
    assert card_path.is_file()
    assert guards_path.is_file()
    prompt = load_system_prompt(card_path, system_guards_path=guards_path)
    assert "【演出约束】" in prompt
    assert "【人格设定】" in prompt
    assert "## 核心" in prompt
    assert "勿复读设定" in prompt or "不要每轮自我介绍" in prompt
    assert "避免把刚说过的反应、拒绝或结论仅换一种说法再讲一遍" in prompt
    assert "不必为了显得不同而刻意转折、添新信息或改变真实态度" in prompt
    assert "重复本身符合当下情绪、强调或对方确实在追问时，可以自然重复" in prompt
    assert "数字生命" in prompt
    assert "对等" in prompt
    assert "桌宠" not in prompt
    assert prompt.index("【演出约束】") < prompt.index("【人格设定】")


def test_current_relationship_follows_accumulated_runtime_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    guards = (root / "characters" / "Sakura" / "system_guards.md").read_text(
        encoding="utf-8"
    )

    assert "当前关系" in guards
    assert "近期对话、常驻档案与长期记忆" in guards
    assert "不能因原作、心情或一次迟疑而重置" in guards
    assert "重新认识" in guards
    assert "你们是恋人" not in guards
    assert "跟随当前关系和当下意愿" in guards
    assert "亲密未打开" not in guards
