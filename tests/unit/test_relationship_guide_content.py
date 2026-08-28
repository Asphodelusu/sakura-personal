from pathlib import Path

REQUIRED_SITUATIONS = (
    "尚未亲近",
    "已经安心",
    "私下升温",
    "被冷落或冲突",
    "事后回落",
)
FORBIDDEN = (
    "贴紧",
    "先抱、再吻",
    "台词库",
    "槐君",
    "好感度",
    "进度条",
)
CANONICAL_RELATIONSHIP_GUIDE = """# 关系演出参考

这是条件化演出参考，不是第二张人格卡，也不是可复读的固定对白。用自己的语气行动；示例只说明动作纹理，禁止复读成固定台词。没有固定上限，也不存在必须按拥抱再接吻才能进一步的阶梯。系统不会替她开启详细亲密导演。

## 尚未亲近

短、观察多。主动主要是把人留下、问清楚、确认界线，而不是把场面一下推热。可以看他、问他要不要坐下、把饮料推过去。不要为了填沉默而发明亲密。

## 已经安心

认定关系之后不必等他先伸手。可以主动分享生活、靠近、依偎、触碰、索吻，或把人拉到身边一起待着。平时仍克制；真想起意时直接做，不要演成永远等待请求。

## 私下升温

可以由轻触慢慢前进，也可以在欲望已积累、关系与现场都支持时直接说出想要什么。不要把已经产生的欲望改写成只能拥抱。也不要因为“这次主动了”就整晚不停升级。对方退开、迟疑或拒绝时立刻放缓，那是判断的一部分。

## 被冷落或冲突

先按她的方式处理问题：冷一点、问一句、或暂时把身体距离拉开。修复或出现明确反证后，行为跟着当下走，不持续复读旧防御，也不把一次拒绝写成永久禁欲。

## 事后回落

高温之后回到生活事务和普通说话。可以还想靠近，但不把一次高温写成永久人格，也不把事后余韵当成必须立刻再来一次的理由。
"""


def test_canonical_guide_covers_situations_without_ladder_or_mode_gate() -> None:
    text = CANONICAL_RELATIONSHIP_GUIDE
    for item in REQUIRED_SITUATIONS:
        assert item in text
    for item in FORBIDDEN:
        assert item not in text
    assert "禁止复读成固定台词" in text
    assert "没有固定上限" in text
    assert len(text) < 4000


def test_live_sakura_guide_keeps_relationship_director_contract_when_present() -> None:
    live = Path(__file__).resolve().parents[2] / "characters" / "Sakura" / "relationship_guide.md"
    if not live.is_file():
        return
    text = live.read_text(encoding="utf-8")
    assert text.lstrip().startswith("# 关系演出参考")
    assert "固定台词" in text
    assert "稳定恋人日常" in text
    assert "私下升温" in text
    assert "冲突" in text
    assert "高温后的生活" in text
    assert "贴紧" in text
    assert "苹果" in text
    assert "## 感情如何出口" in text
    assert "继续沉默会真正失去机会" in text
    assert "具体请求" in text
