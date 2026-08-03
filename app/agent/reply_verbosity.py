"""本轮回复篇幅：仅由内心独白给出的 interest 驱动。

interest 是主观判断（对方一句「嗯」也可能很高），不做规则启发或兜底猜测。
独白缺失/未解析出 interest 时，不注入本轮篇幅块，只保留通用协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

InterestLevel = Literal["low", "mid", "high"]
VerbosityTier = Literal["brief", "normal", "engaged"]

# 真实闲聊经验：一句 ≈ 一个 segment
TIER_SEGMENT_RANGE: dict[VerbosityTier, tuple[int, int]] = {
    "brief": (1, 2),
    "normal": (1, 3),
    "engaged": (3, 5),
}

INTEREST_TO_TIER: dict[InterestLevel, VerbosityTier] = {
    "low": "brief",
    "mid": "normal",
    "high": "engaged",
}


@dataclass(frozen=True)
class VerbosityDecision:
    interest: InterestLevel
    tier: VerbosityTier
    min_segments: int
    max_segments: int

    @property
    def label_zh(self) -> str:
        return {
            "brief": "偏短",
            "normal": "日常",
            "engaged": "兴致较高",
        }[self.tier]


def decision_from_interest(interest: str | None) -> VerbosityDecision | None:
    level = str(interest or "").strip().lower()
    if level not in INTEREST_TO_TIER:
        return None
    typed: InterestLevel = level  # type: ignore[assignment]
    tier = INTEREST_TO_TIER[typed]
    low, high = TIER_SEGMENT_RANGE[tier]
    return VerbosityDecision(typed, tier, low, high)


def format_verbosity_guidance(decision: VerbosityDecision) -> str:
    low, high = decision.min_segments, decision.max_segments
    if decision.tier == "brief":
        body = (
            f"本轮你对继续展开兴致不高：优先 {low}-{high} 个短句 segment 收住，"
            "别为了礼貌硬凑。"
        )
    elif decision.tier == "normal":
        body = (
            f"本轮兴致平常：大约 {low}-{high} 个短句 segment，像日常闲聊即可。"
        )
    else:
        body = (
            f"本轮你对这个话题/这一拍交流兴致较高：可以说满大约 {low}-{high} 个短句 segment，"
            "多一点反应或补充也自然；仍用口语短句，不要写成说明文。"
            "若话头很热，偶尔再多一句也可以，但不要变成演讲。"
        )
    return (
        "【本轮篇幅】\n"
        f"- 兴致：{decision.interest}（{decision.label_zh}，约 {low}-{high} 句）\n"
        f"- {body}\n"
        "- 上限不是任务：没话可说时少说也完全正常。"
    )
