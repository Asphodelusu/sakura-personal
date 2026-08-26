"""Observer speech-decision fallback: adopt short natural Japanese dialogue."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.perception.observer import (
    ProactiveConfig,
    ProactiveObserver,
    _adopt_plain_dialogue_decision,
)


_ONE_SENTENCE = "ちょっと見てる。"
_TWO_SENTENCES = "ねえ、今ちょっと見ていい？邪魔しないよ。"


def _decision_observer(*, content: str) -> ProactiveObserver:
    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        chat_api_base_url="https://chat.example.com",
        chat_api_key="k",
        chat_api_model="decision",
        config=ProactiveConfig(enabled=True),
    )
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ]
    }
    observer._chat_http = AsyncMock()
    observer._chat_http.post = AsyncMock(return_value=response)
    return observer


def _run_decision(content: str) -> dict | None:
    observer = _decision_observer(content=content)
    return asyncio.run(observer._post_speech_decision([{"role": "user", "content": "x"}]))


def _log_text(mock_logger) -> str:
    parts: list[str] = []
    for method in (mock_logger.info, mock_logger.warning, mock_logger.debug):
        for call in method.call_args_list:
            parts.append(" ".join(str(arg) for arg in call.args))
    return "\n".join(parts)


def test_adopts_one_sentence_natural_japanese_dialogue() -> None:
    decision = _adopt_plain_dialogue_decision(_ONE_SENTENCE)

    assert decision == {
        "should_speak": True,
        "comment": _ONE_SENTENCE,
        "translation": "",
        "tone": "中性",
        "reason": "决策输出回退为短日语对白",
        "situational_summary": "",
    }


def test_adopts_two_sentence_natural_japanese_dialogue() -> None:
    decision = _adopt_plain_dialogue_decision(_TWO_SENTENCES)

    assert decision is not None
    assert decision["should_speak"] is True
    assert decision["comment"] == _TWO_SENTENCES
    assert decision["translation"] == ""
    assert decision["tone"] == "中性"


def test_rejects_chinese_explanation() -> None:
    assert (
        _adopt_plain_dialogue_decision("他正在看剧情，所以我先不说话。") is None
    )


def test_rejects_english_explanation() -> None:
    assert (
        _adopt_plain_dialogue_decision(
            "I think I should stay quiet while he watches the scene."
        )
        is None
    )


def test_rejects_markdown_wrapper() -> None:
    assert _adopt_plain_dialogue_decision(f"**{_ONE_SENTENCE}**") is None
    assert _adopt_plain_dialogue_decision(f"```\n{_ONE_SENTENCE}\n```") is None
    assert _adopt_plain_dialogue_decision(f"- {_ONE_SENTENCE}") is None
    assert _adopt_plain_dialogue_decision(f"1. {_ONE_SENTENCE}") is None
    assert _adopt_plain_dialogue_decision(f"> {_ONE_SENTENCE}") is None


def test_adopts_ellipsis_and_midline_ascii_hyphen() -> None:
    ellipsis = _adopt_plain_dialogue_decision("ちょっと待って…")
    assert ellipsis is not None
    assert ellipsis["comment"] == "ちょっと待って…"
    hyphen = _adopt_plain_dialogue_decision("ちょっと待って-いや、見てる。")
    assert hyphen is not None
    assert hyphen["comment"] == "ちょっと待って-いや、見てる。"


def test_rejects_kanji_only_and_latin_loanword_mix() -> None:
    assert _adopt_plain_dialogue_decision("了解。") is None
    assert _adopt_plain_dialogue_decision("YouTube見てる。") is None


def test_rejects_json_fragment() -> None:
    assert _adopt_plain_dialogue_decision('{"should_speak": true, "comment":') is None
    assert _adopt_plain_dialogue_decision('"comment": "ちょっと見てる。"') is None


def test_rejects_system_and_report_prose() -> None:
    assert _adopt_plain_dialogue_decision("观察者评估完成，建议保持沉默。") is None
    assert _adopt_plain_dialogue_decision("システム報告：いまは発言しない。") is None
    assert _adopt_plain_dialogue_decision("評価結果として発言すべきです。") is None


def test_rejects_japanese_meta_decision_narration() -> None:
    assert (
        _adopt_plain_dialogue_decision(
            "彼が動画を見ているので、話しかけることにします。"
        )
        is None
    )
    assert _adopt_plain_dialogue_decision("今は発言することにします。") is None
    assert _adopt_plain_dialogue_decision("発言すべきです。") is None
    assert _adopt_plain_dialogue_decision("発言します。") is None
    assert _adopt_plain_dialogue_decision("判断しました。") is None
    assert _adopt_plain_dialogue_decision("判断します。") is None


def test_preserves_direct_utterances_that_mention_talking() -> None:
    asked = _adopt_plain_dialogue_decision("今話しかけてもいい？")
    assert asked is not None
    assert asked["comment"] == "今話しかけてもいい？"
    assert _adopt_plain_dialogue_decision("今は黙って見るね。") is not None
    assert _adopt_plain_dialogue_decision("発言するな。") is not None
    assert _adopt_plain_dialogue_decision("見るべき？") is not None


def test_rejects_blank_content() -> None:
    assert _adopt_plain_dialogue_decision("") is None
    assert _adopt_plain_dialogue_decision("   \n") is None


def test_rejects_overly_long_and_three_sentence_text() -> None:
    assert _adopt_plain_dialogue_decision("あ" * 81 + "。") is None
    assert (
        _adopt_plain_dialogue_decision(
            "今日は長い一日だったね。画面も動いてる。お茶も冷めちゃった。"
        )
        is None
    )


def test_post_speech_decision_keeps_valid_json_unchanged() -> None:
    payload = (
        '{"should_speak":false,"reason":"他看着剧情呢，先不吵。",'
        '"comment":"","translation":"","tone":"","situational_summary":"彼が劇情を見ている。"}'
    )

    decision = _run_decision(payload)

    assert decision == {
        "should_speak": False,
        "reason": "他看着剧情呢，先不吵。",
        "comment": "",
        "translation": "",
        "tone": "",
        "situational_summary": "彼が劇情を見ている。",
    }


def test_post_speech_decision_adopts_plain_dialogue_after_json_failure() -> None:
    decision = _run_decision(_ONE_SENTENCE)

    assert decision is not None
    assert decision["should_speak"] is True
    assert decision["comment"] == _ONE_SENTENCE
    assert decision["translation"] == ""
    assert decision["tone"] == "中性"


def test_post_speech_decision_rejects_invalid_output_after_json_failure() -> None:
    assert _run_decision("I should speak now.") is None
    assert _run_decision("") is None


def test_post_speech_decision_logs_structured_outcomes_without_full_dialogue() -> None:
    with patch("app.perception.observer.logger") as mock_logger:
        _run_decision(
            '{"should_speak":false,"reason":"x","comment":"","translation":"","tone":""}'
        )
        json_logs = _log_text(mock_logger)
        assert "valid_json" in json_logs

    with patch("app.perception.observer.logger") as mock_logger:
        _run_decision(_TWO_SENTENCES)
        adopted_logs = _log_text(mock_logger)
        assert "adopted_plain_dialogue" in adopted_logs
        assert _TWO_SENTENCES not in adopted_logs

    with patch("app.perception.observer.logger") as mock_logger:
        _run_decision("系统报告：当前应保持沉默。")
        rejected_logs = _log_text(mock_logger)
        assert "rejected_invalid_output" in rejected_logs
        assert "系统报告：当前应保持沉默。" not in rejected_logs
