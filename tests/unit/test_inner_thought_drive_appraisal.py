"""Optional inner-thought drive appraisal headers: parse, fail-open, token budget."""

from __future__ import annotations

from app.agent.inner_thought import (
    DEFAULT_INNER_THOUGHT_MAX_TOKENS,
    InnerThoughtResult,
    build_inner_thought_system_prompt,
    build_inner_thought_user_prompt,
    parse_inner_thought_output,
)
from app.core.relational_drive import DriveKind


def test_inner_thought_parses_optional_drive_headers() -> None:
    result = parse_inner_thought_output(
        "interest: high\n"
        "drive_kind: erotic_salience\n"
        "drive_shift: rise\n"
        "drive_strength: subtle\n"
        "昨夜のことを、少し思い出した。"
    )
    assert result.text == "昨夜のことを、少し思い出した。"
    assert result.interest == "high"
    assert result.drive_appraisal is not None
    assert result.drive_appraisal.kind == "erotic_salience"
    assert result.drive_appraisal.direction == "rise"
    assert result.drive_appraisal.strength == "subtle"


def test_illegal_drive_headers_do_not_drop_inner_thought_text() -> None:
    result = parse_inner_thought_output(
        "interest: mid\ndrive_strength: strong\nまだ少し気になる。"
    )
    assert result.text == "まだ少し気になる。"
    assert result.interest == "mid"
    assert result.drive_appraisal is None


def test_no_header_output_preserves_old_text_and_interest() -> None:
    raw = "黙ってしまった。少し不安。"
    result = parse_inner_thought_output(raw)
    assert result.interest is None
    assert result.drive_appraisal is None
    assert result.text == parse_inner_thought_output(raw).text
    assert "不安" in result.text


def test_interest_only_output_stays_compatible() -> None:
    result = parse_inner_thought_output(
        "interest: high\nあ、この話題好きだ。もっと話したいな。"
    )
    assert result.interest == "high"
    assert result.drive_appraisal is None
    assert "話題" in result.text
    assert "interest" not in result.text.lower()
    assert "drive_" not in result.text


def test_unknown_kind_is_fail_open() -> None:
    result = parse_inner_thought_output(
        "interest: low\n"
        "drive_kind: mood\n"
        "drive_shift: rise\n"
        "drive_strength: mild\n"
        "特に何も。"
    )
    assert result.text == "特に何も。"
    assert result.interest == "low"
    assert result.drive_appraisal is None


def test_partial_triplet_is_ignored() -> None:
    result = parse_inner_thought_output(
        "interest: mid\n"
        "drive_kind: attachment_longing\n"
        "drive_shift: hold\n"
        "まだ少し気になる。"
    )
    assert result.text == "まだ少し気になる。"
    assert result.drive_appraisal is None


def test_duplicate_headers_are_ignored() -> None:
    result = parse_inner_thought_output(
        "interest: high\n"
        "drive_kind: erotic_salience\n"
        "drive_kind: afterglow\n"
        "drive_shift: rise\n"
        "drive_strength: subtle\n"
        "昨夜のことを、少し思い出した。"
    )
    assert result.text == "昨夜のことを、少し思い出した。"
    assert result.drive_appraisal is None


def test_conflicting_headers_are_ignored() -> None:
    result = parse_inner_thought_output(
        "interest: high\n"
        "drive_kind: physical_arousal\n"
        "drive_shift: rise\n"
        "drive_shift: fall\n"
        "drive_strength: mild\n"
        "昨夜のことを、少し思い出した。"
    )
    assert result.text == "昨夜のことを、少し思い出した。"
    assert result.drive_appraisal is None


def test_malformed_header_line_is_ignored() -> None:
    result = parse_inner_thought_output(
        "interest: mid\n"
        "drive_kind:\n"
        "drive_shift: rise\n"
        "drive_strength: subtle\n"
        "まだ少し気になる。"
    )
    assert result.text == "まだ少し気になる。"
    assert result.drive_appraisal is None


def test_drive_headers_without_interest_stay_in_text() -> None:
    raw = (
        "drive_kind: erotic_salience\n"
        "drive_shift: rise\n"
        "drive_strength: subtle\n"
        "昨夜のことを、少し思い出した。"
    )
    result = parse_inner_thought_output(raw)
    assert result.interest is None
    assert result.drive_appraisal is None
    assert "drive_kind" in result.text or "昨夜" in result.text


def test_old_result_constructor_defaults_appraisal_to_none() -> None:
    result = InnerThoughtResult(text="特に何も", interest="low")
    assert result.drive_appraisal is None


def test_allowed_kinds_come_from_drive_kind() -> None:
    kinds = {item.value for item in DriveKind}
    assert kinds == {
        "physical_arousal",
        "erotic_salience",
        "attachment_longing",
        "afterglow",
        "inhibition",
    }
    result = parse_inner_thought_output(
        "interest: high\n"
        "drive_kind: afterglow\n"
        "drive_shift: hold\n"
        "drive_strength: mild\n"
        "少しだけ余韻が残ってる。"
    )
    assert result.drive_appraisal is not None
    assert result.drive_appraisal.kind == "afterglow"


def test_optional_instruction_defines_the_complete_fail_open_contract() -> None:
    from app.agent.inner_thought import DRIVE_APPRAISAL_OUTPUT_INSTRUCTION

    system = build_inner_thought_system_prompt("桜")
    user = build_inner_thought_user_prompt(
        character_name="桜",
        character_excerpt="",
        mood_summary="",
        recent_dialogue="用户：在吗",
    )
    assert DRIVE_APPRAISAL_OUTPUT_INSTRUCTION in system or DRIVE_APPRAISAL_OUTPUT_INSTRUCTION in user
    joined = f"{system}\n{user}"
    for field in ("drive_kind", "drive_shift", "drive_strength"):
        assert field in joined
    for value in (
        "physical_arousal",
        "erotic_salience",
        "attachment_longing",
        "afterglow",
        "inhibition",
        "rise",
        "fall",
        "hold",
        "subtle",
        "mild",
    ):
        assert value in joined
    assert "省略" in joined
    assert "新对话" in joined
    assert "当前状态" in joined
    assert "除上述可选 drive 头外" in joined
    # Task 4 allows at most 24 extra output tokens over the former 180-token cap.
    assert DEFAULT_INNER_THOUGHT_MAX_TOKENS <= 204
