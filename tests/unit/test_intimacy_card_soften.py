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


def test_soften_keeps_required_markdown_persona_sections() -> None:
    card = """handoff: local-only metadata

## 核心
她会认真判断，不机械服从。

## 能动性与判断
任何一方迟疑、沉默或退开时，她会停下来确认，并保留退出权。

## 情绪怎样发生
这是一大段可省略的场景细节。

## 关系中的她
关系推进后仍尊重双方意愿和边界。

## 日常质感
兴趣清单与生活细节。

## 不要写成
不要写成没有选择的所有物，也不要只换说法重复。
"""
    soft = soften_character_card_for_intimacy(
        with_desktop_pet_context(card),
        max_persona_chars=1400,
    )

    for heading in ("核心", "能动性与判断", "关系中的她", "不要写成"):
        assert f"## {heading}" in soft
    assert sum(line.strip() == "【人格设定】" for line in soft.splitlines()) == 1
    assert "退出权" in soft
    assert "迟疑" in soft
    assert "handoff" not in soft.lower()
    assert "## 日常质感" not in soft
    assert not soft.rstrip().endswith("## 情绪怎样发生")


def test_soften_small_budget_keeps_each_required_section_body() -> None:
    card = """## 核心
核心判断。

## 能动性与判断
迟疑时暂停，保留退出权。

## 关系中的她
尊重双方意愿。

## 不要写成
不要写成没有选择，也不要机械重复。
"""
    soft = soften_character_card_for_intimacy(
        with_desktop_pet_context(card),
        max_persona_chars=120,
    )

    for body in ("核心判断", "退出权", "双方意愿", "不要写成"):
        assert body in soft


def test_soften_prioritizes_boundary_paragraph_in_long_relationship_section() -> None:
    card = """## 核心
她会自己判断。

## 能动性与判断
她不是顺从型，会设边界。

## 关系中的她
正式成为恋人后，她会发展出明确的占有欲，也会主动认领彼此的位置。""" + (
        "这是一段很长但优先级较低的关系背景。" * 30
    ) + """

亲近不会抹掉她原本的克制。任何一方迟疑、沉默或退开，节奏就停下来确认；她保留独立判断和退出权。

## 不要写成
不要写成没有选择的所有物，也不要机械重复。
"""
    soft = soften_character_card_for_intimacy(
        with_desktop_pet_context(card),
        max_persona_chars=500,
    )

    assert "迟疑" in soft
    assert "退出权" in soft
    assert "没有选择" in soft
