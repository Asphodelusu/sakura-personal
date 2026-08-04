from app.agent.screen_policy import ScreenPolicy
from app.agent.screen_observation import SCREEN_OBSERVATION_HISTORY_MARKER


def test_screen_policy_hides_tool_for_casual_chat() -> None:
    assert not ScreenPolicy.should_offer_screen_observation_text("喂？")
    assert not ScreenPolicy.should_offer_screen_observation_text(
        "我在做测试嘛，别那么不高兴嘛"
    )
    assert not ScreenPolicy.should_offer_screen_observation_text("今天聊点什么")


def test_screen_policy_offers_tool_for_screen_dependent_asks() -> None:
    assert ScreenPolicy.should_offer_screen_observation_text("你觉得我现在是不是卡住了？")
    assert ScreenPolicy.should_offer_screen_observation_text("帮我看下这个报错")
    assert ScreenPolicy.should_offer_screen_observation_text("看看当前画面")


def test_screen_policy_hides_tool_after_observation_marker() -> None:
    assert not ScreenPolicy.should_offer_screen_observation_text(
        f"这报错什么意思\n{SCREEN_OBSERVATION_HISTORY_MARKER}"
    )
