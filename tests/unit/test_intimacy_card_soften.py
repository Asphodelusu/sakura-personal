"""亲密模式弱化人格卡。"""

from __future__ import annotations

from app.llm.prompts.blocks import (
    soften_character_card_for_intimacy,
    with_desktop_pet_context,
)


def test_soften_keeps_guards_and_focus_overlay() -> None:
    full = with_desktop_pet_context(
        "我是夜乃桜。\n" + ("日常设定细节。\n" * 200),
        system_guards="- 勿复述战力",
    )
    soft = soften_character_card_for_intimacy(full)
    assert "【演出约束】" in soft
    assert "勿复述战力" in soft
    assert "【当下专注】" in soft
    assert "眼前的触感" in soft
    assert "夜乃桜" in soft
    assert soft.count("日常设定细节") < full.count("日常设定细节")
    assert len(soft) < len(full)


def test_soften_truncates_long_persona_section() -> None:
    persona = "身份锚：樱。\n" + ("很长的兴趣爱好清单。" * 100)
    full = with_desktop_pet_context(persona)
    soft = soften_character_card_for_intimacy(full, max_persona_chars=200)
    assert "身份锚：樱" in soft
    assert "亲密中从简" in soft or len(soft) < len(full)
    assert "很长的兴趣爱好清单。" * 20 not in soft


def test_soften_plain_prompt_without_headers() -> None:
    soft = soften_character_card_for_intimacy("短人设", max_persona_chars=720)
    assert "短人设" in soft
    assert "【当下专注】" in soft
