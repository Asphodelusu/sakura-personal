from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QImage
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QWidget


SCREEN_OBSERVATION_TRIGGER_KEYWORDS = (
    "看屏幕",
    "观察屏幕",
    "看看屏幕",
    "看看当前画面",
    "帮我看这个",
)
SCREEN_OBSERVATION_HISTORY_MARKER = "[Sakura 已自主观察屏幕]"
MANUAL_SCREEN_OBSERVATION_HISTORY_MARKER = "[Sakura 已附加手动框选截图]"
SCREEN_OBSERVATION_MAX_EDGE = 1280
SCREEN_OBSERVATION_JPEG_QUALITY = 70


@dataclass(frozen=True)
class ScreenObservation:
    """一次按需屏幕观察结果，不负责持久化截图内容。"""

    data_url: str
    width: int
    height: int
    captured_at: str
    screen_name: str


@dataclass(frozen=True)
class CapturedScreenImage:
    """UI 线程捕获的屏幕图像；后续压缩编码可放到后台线程。"""

    image: QImage
    captured_at: str
    screen_name: str


def should_observe_screen(text: str) -> bool:
    """判断用户是否明确要求观察屏幕。"""
    normalized = "".join(text.split()).lower()
    return any(keyword in normalized for keyword in SCREEN_OBSERVATION_TRIGGER_KEYWORDS)


def append_observation_marker(
    text: str,
    observation: ScreenObservation,
    visual_id: str | None = None,
) -> str:
    """给历史记录追加观察标记，避免保存 base64 图片。"""
    _ = observation
    return f"{text.rstrip()}\n{_marker_with_visual_id(SCREEN_OBSERVATION_HISTORY_MARKER, visual_id)}"


def append_manual_observation_marker(
    text: str,
    observation: ScreenObservation,
    visual_id: str | None = None,
) -> str:
    """给手动框选截图追加历史标记，避免保存 base64 图片。"""
    _ = observation
    return f"{text.rstrip()}\n{_marker_with_visual_id(MANUAL_SCREEN_OBSERVATION_HISTORY_MARKER, visual_id)}"


def build_screen_observation_user_message(
    text: str,
    observation: ScreenObservation,
) -> dict[str, object]:
    """构造 OpenAI 兼容的多模态用户消息，附赠 UIA 直接读取的窗口文字。"""
    prompt_text = (
        f"{text.strip()}\n\n"
        f"当前屏幕截图信息：{observation.width}x{observation.height}，"
        f"捕获时间 {observation.captured_at}，屏幕 {observation.screen_name}。\n"
        "【忽略自己的界面】桌宠（夜乃樱／立绘）通常在屏幕右下角或边缘；"
        "那里的对话气泡、日文/中文台词、输入栏都是你自己说的话或自己的 UI，"
        "不是对方正在看的应用内容。请完全忽略，不要摘抄、不要回答、不要据此自问自答；"
        "只描述并理解对方正在操作的窗口/页面/游戏等内容。"
    ).strip()

    # 附加 UIA 直接读取的窗口文字（免费，不经过 OCR）
    # 前台若是本进程，读到的就是自家气泡/输入栏，会诱发自问自答，故跳过。
    try:
        import os

        from app.perception.win32 import get_active_window_pid
        from app.perception.screen_reader import read_active_window

        if int(get_active_window_pid() or 0) != int(os.getpid()):
            uia = read_active_window()
            if uia.is_accessible and uia.text_content.strip():
                uia_block = (
                    f"\n\n[UIA 直接读取] 以下文字来自系统无障碍接口，已从屏幕控件直接提取，无需 OCR：\n"
                    f"应用类型：{uia.app_type}\n"
                    f"进程：{uia.process_name}\n"
                    f"{uia.text_content}"
                )
                prompt_text += uia_block
    except Exception:
        pass  # UIA 不可用时静默跳过，截图仍正常发送

    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": prompt_text,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": observation.data_url,
                },
            },
        ],
    }


def capture_screen_image(excluded_widget: QWidget | None = None) -> CapturedScreenImage:
    """截取光标所在屏幕并复制为 QImage，避免后台线程触碰 QPixmap。

    不在截图时 hide/show 桌宠（会闪一下，观感差）。自家 UI 靠调用方跳过本进程 UIA、
    以及观察提示里的忽略说明；excluded_widget 保留形参以兼容现有调用点。
    """

    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication

    _ = excluded_widget
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("屏幕观察需要先创建 QApplication。")

    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen is None:
        raise RuntimeError("无法找到可截图的屏幕。")

    pixmap = screen.grabWindow(0)

    if pixmap.isNull():
        raise RuntimeError("屏幕截图为空，可能被系统权限或显示环境阻止。")

    return CapturedScreenImage(
        image=pixmap.toImage().copy(),
        captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        screen_name=screen.name() or "primary",
    )


def capture_screen_observation(excluded_widget: QWidget | None = None) -> ScreenObservation:
    """同步截屏并编码；UI 调用优先使用 capture_screen_image + 后台编码。"""
    return build_screen_observation_from_image(capture_screen_image(excluded_widget))


def build_screen_observation_from_image(
    captured: CapturedScreenImage,
    *,
    max_edge: int = SCREEN_OBSERVATION_MAX_EDGE,
) -> ScreenObservation:
    """从已复制的 QImage 构造观察结果，可在后台线程执行。"""
    if captured.image.isNull():
        raise RuntimeError("屏幕截图为空。")

    encoded_image = _scaled_image(captured.image, max_edge=max_edge)
    return ScreenObservation(
        data_url=_encode_image_to_data_url(encoded_image),
        width=encoded_image.width(),
        height=encoded_image.height(),
        captured_at=captured.captured_at,
        screen_name=captured.screen_name,
    )


def build_screen_observation_from_pixmap(
    pixmap: QPixmap,
    screen_name: str = "manual-selection",
) -> ScreenObservation:
    """从用户框选区域构造一次屏幕观察结果。"""
    if pixmap.isNull():
        raise RuntimeError("框选截图为空。")

    return build_screen_observation_from_image(
        CapturedScreenImage(
            image=pixmap.toImage().copy(),
            captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            screen_name=screen_name,
        )
    )


def _scaled_image(image: QImage, *, max_edge: int = SCREEN_OBSERVATION_MAX_EDGE) -> QImage:
    from PySide6.QtCore import Qt

    max_edge = max(1, int(max_edge or SCREEN_OBSERVATION_MAX_EDGE))
    longest_edge = max(image.width(), image.height())
    if longest_edge <= max_edge:
        return image
    return image.scaled(
        max_edge,
        max_edge,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _encode_image_to_data_url(image: QImage) -> str:
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPEG", SCREEN_OBSERVATION_JPEG_QUALITY):
        raise RuntimeError("屏幕截图编码失败。")
    image_bytes = bytes(buffer.data())
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _scaled_pixmap(pixmap: QPixmap) -> QPixmap:
    from PySide6.QtCore import Qt

    longest_edge = max(pixmap.width(), pixmap.height())
    if longest_edge <= SCREEN_OBSERVATION_MAX_EDGE:
        return pixmap
    return pixmap.scaled(
        SCREEN_OBSERVATION_MAX_EDGE,
        SCREEN_OBSERVATION_MAX_EDGE,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _encode_pixmap_to_data_url(pixmap: QPixmap) -> str:
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pixmap.toImage().save(buffer, "JPEG", SCREEN_OBSERVATION_JPEG_QUALITY):
        raise RuntimeError("屏幕截图编码失败。")
    image_bytes = bytes(buffer.data())
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _marker_with_visual_id(marker: str, visual_id: str | None) -> str:
    if not visual_id:
        return marker
    if marker.endswith("]"):
        return f"{marker[:-1]}，视觉记录 visual_id={visual_id}]"
    return f"{marker}，视觉记录 visual_id={visual_id}"


# ---- VLM 摘要：截图 → 文本描述 → 聊天模型 ----

_VLM_SUMMARY_SYSTEM_PROMPT = """\
你是一个桌面截图描述助手。请仔细观察截图，用中文输出以下信息：

1. 当前打开的应用/窗口是什么
2. 画面上有哪些主要内容（文字、图片、UI 元素等）
3. 用户可能正在做什么或看什么

要求：
- 详细但简洁，重点描述对理解用户当前活动有帮助的信息
- 如果画面上有文字，尽量摘录关键内容
- 不要评价画面质量或截图本身
- 输出纯文本，不要用 JSON 或 Markdown 格式"""


def summarize_screen_observation(
    observation: ScreenObservation,
    api_client: object,
    *,
    cancel_checker: object | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    """调用 VLM 把截图转成文本描述（同步，在后台线程调用）。

    失败时返回空字符串；调用方应把空结果当作「摘要不可用」处理。
    """
    from app.llm.api_client import ApiRequestError, OpenAICompatibleClient

    client: OpenAICompatibleClient | None = None
    # 优先用 vision_api_client（如果 Runtime 暴露了），否则 fallback 到主 client。
    vision_attr = getattr(api_client, "vision_api_client", None)
    if vision_attr is not None:
        client = vision_attr
    elif isinstance(api_client, OpenAICompatibleClient):
        client = api_client
    else:
        cloud = getattr(api_client, "cloud_client", None)
        if isinstance(cloud, OpenAICompatibleClient):
            client = cloud

    if client is None:
        return ""

    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张截图。"},
                {
                    "type": "image_url",
                    "image_url": {"url": observation.data_url},
                },
            ],
        }
    ]

    # 用较低温度 + 限制 token，避免 VLM 长篇大论（摘要应在 500-1000 tokens 内）。
    try:
        raw = client.complete_raw(
            _VLM_SUMMARY_SYSTEM_PROMPT,
            messages,
            temperature=0.3,
            max_tokens=_VLM_SUMMARY_MAX_TOKENS,
            cancel_checker=cancel_checker,
            task="vision",
            request_timeout=timeout_seconds,
        )
    except (ApiRequestError, TimeoutError, OSError, ValueError) as exc:
        from app.core.debug_log import debug_log
        debug_log(
            "ScreenObservation",
            "VLM 摘要失败，将回退到纯文本上下文",
            {"error": str(exc)},
        )
        return ""
    except Exception:
        return ""

    return (raw or "").strip()


_VLM_SUMMARY_MAX_TOKENS = 1024


def build_screen_observation_summary_message(
    text: str,
    summary: str,
    observation: ScreenObservation,
) -> dict[str, object]:
    """用 VLM 文本摘要代替原始截图，构建纯文本用户消息。

    summary 为空时回退到只含元数据的文本消息（不含截图）。
    """
    parts: list[str] = [text.strip()]

    meta = (
        f"\n\n【屏幕截图】{observation.width}x{observation.height}，"
        f"捕获时间 {observation.captured_at}，屏幕 {observation.screen_name}。"
    )

    if summary:
        parts.append(
            f"{meta}\n"
            f"以下为视觉模型对截图的分析描述：\n\n"
            f"{summary}"
        )
    else:
        parts.append(f"{meta}\n（视觉摘要暂时不可用，请根据对话上下文和已有信息回应用户。）")

    # 附加 UIA 直接读取的窗口文字（免费，不经过 OCR/VLM）
    try:
        import os
        from app.perception.win32 import get_active_window_pid
        from app.perception.screen_reader import read_active_window

        if int(get_active_window_pid() or 0) != int(os.getpid()):
            uia = read_active_window()
            if uia.is_accessible and uia.text_content.strip():
                parts.append(
                    f"\n\n[UIA 直接读取] 以下文字来自系统无障碍接口，已从屏幕控件直接提取：\n"
                    f"应用类型：{uia.app_type}\n"
                    f"进程：{uia.process_name}\n"
                    f"{uia.text_content}"
                )
    except Exception:
        pass

    return {"role": "user", "content": "\n\n".join(parts)}
