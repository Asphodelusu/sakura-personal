"""plugins/mmd_renderer/bridge.py — Python ↔ JS 消息协议。

统一消息信封：``{"type": <str>, "payload": <dict>}``。本模块负责：
- 集中消息类型常量，避免散落字符串；
- 把消息安全序列化为可注入 ``runJavaScript`` 的 JS 表达式。

安全约束：
- 参数一律走 ``json.dumps`` 序列化，**禁止**字符串拼接用户数据进 JS；
- 处理 U+2028/U+2029：它们是合法 JSON 字符但在 JS 源码中是行终止符，
  不转义会破坏注入语句，这里显式转义。

JS→Python 回传（如点击模型、加载完成回调）预留给后续 QWebChannel 接入（TODO）。
"""

from __future__ import annotations

import json
from typing import Any

# ---- 消息类型常量 ----
MSG_LOAD_CHARACTER = "loadCharacter"
MSG_PLAY_MOTION = "playMotion"
MSG_STOP_MOTION = "stopMotion"
MSG_SET_EXPRESSION = "setExpression"
MSG_SET_LIP_SYNC = "setLipSync"
MSG_LOOK_AT = "lookAt"
MSG_SET_SCALE = "setScale"
MSG_EVENT = "event"
MSG_PING = "ping"

# 浏览器全局上挂载的控制器对象名（见 web/main.js）。
JS_CONTROLLER = "window.sakuraMMD"

# JS 源码中的行终止符（U+2028 / U+2029）。用 chr 构造避免源码内出现裸不可见字符。
_JS_LINE_SEP = chr(0x2028)
_JS_PARA_SEP = chr(0x2029)


def build_message(message_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造统一消息信封。"""
    return {"type": message_type, "payload": payload or {}}


def serialize_message(message: dict[str, Any]) -> str:
    """把消息序列化为安全的 JSON 文本（保留非 ASCII，转义 JS 行终止符）。"""
    text = json.dumps(message, ensure_ascii=False)
    return text.replace(_JS_LINE_SEP, "\\u2028").replace(_JS_PARA_SEP, "\\u2029")


def build_dispatch_js(message: dict[str, Any]) -> str:
    """构造注入 runJavaScript 的分发语句。

    形如：``window.sakuraMMD && window.sakuraMMD.dispatch({...})``，
    控制器不存在时短路，不报错。
    """
    return f"{JS_CONTROLLER} && {JS_CONTROLLER}.dispatch({serialize_message(message)});"
