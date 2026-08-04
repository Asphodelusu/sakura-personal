"""篇幅仅由独白 interest 驱动，无规则启发。"""

from __future__ import annotations

from app.agent.reply_verbosity import (
    decision_from_interest,
    format_verbosity_guidance,
)


def test_low_maps_to_brief() -> None:
    decision = decision_from_interest("low")
    assert decision is not None
    assert decision.tier == "brief"
    assert decision.min_segments == 1
    assert decision.max_segments == 2


def test_mid_maps_to_normal() -> None:
    decision = decision_from_interest("MID")
    assert decision is not None
    assert decision.tier == "normal"


def test_high_maps_to_engaged() -> None:
    decision = decision_from_interest("high")
    assert decision is not None
    assert decision.tier == "engaged"
    assert decision.min_segments == 3
    assert decision.max_segments == 5


def test_unknown_interest_has_no_decision() -> None:
    assert decision_from_interest(None) is None
    assert decision_from_interest("") is None
    assert decision_from_interest("maybe") is None


def test_guidance_mentions_interest_and_range() -> None:
    decision = decision_from_interest("high")
    assert decision is not None
    guidance = format_verbosity_guidance(decision)
    assert "【本轮篇幅】" in guidance
    assert "high" in guidance
    assert "3-5" in guidance
