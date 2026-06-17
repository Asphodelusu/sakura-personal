"""Web 消息通道（队列/flush）与消息协议序列化单元测试（纯 Python，无 Qt）。"""

from __future__ import annotations

from plugins.mmd_renderer.bridge import (
    build_dispatch_js,
    build_message,
    serialize_message,
)
from plugins.mmd_renderer.channel import WebMessageChannel


def test_queue_before_ready_then_flush_in_order():
    sent: list[str] = []
    ch = WebMessageChannel(send_js=sent.append)

    ch.dispatch("ping")
    ch.dispatch("playMotion", {"name": "idle", "loop": True})

    # 未就绪：全部入队，未发送。
    assert sent == []
    assert ch.pending_count == 2
    assert not ch.is_ready

    ch.set_ready()

    assert ch.is_ready
    assert ch.pending_count == 0
    assert len(sent) == 2
    assert "ping" in sent[0]
    assert "playMotion" in sent[1]


def test_dispatch_after_ready_sends_immediately():
    sent: list[str] = []
    ch = WebMessageChannel(send_js=sent.append)
    ch.set_ready()
    ch.dispatch("ping")
    assert len(sent) == 1


def test_reset_returns_to_not_ready():
    sent: list[str] = []
    ch = WebMessageChannel(send_js=sent.append)
    ch.set_ready()
    ch.reset()
    assert not ch.is_ready
    ch.dispatch("ping")
    assert sent == []
    assert ch.pending_count == 1


def test_serialize_escapes_js_line_separators():
    # U+2028 / U+2029 是合法 JSON 字符但会破坏注入语句，必须转义。用 chr 明确构造。
    sep_line = chr(0x2028)
    sep_para = chr(0x2029)
    msg = build_message("event", {"text": "a" + sep_line + "b" + sep_para + "c"})
    text = serialize_message(msg)
    assert sep_line not in text
    assert sep_para not in text
    assert "\\u2028" in text
    assert "\\u2029" in text


def test_serialize_preserves_non_ascii():
    msg = build_message("setExpression", {"name": "笑い"})
    text = serialize_message(msg)
    assert "笑い" in text


def test_build_dispatch_js_shape():
    js = build_dispatch_js(build_message("ping"))
    assert js.startswith("window.sakuraMMD && window.sakuraMMD.dispatch(")
    assert js.endswith(");")


def test_build_message_envelope():
    msg = build_message("setLipSync", {"value": 0.5})
    assert msg == {"type": "setLipSync", "payload": {"value": 0.5}}
    # payload 省略时应为空字典
    assert build_message("ping") == {"type": "ping", "payload": {}}
