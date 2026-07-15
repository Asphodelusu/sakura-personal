"""UIA screen reader — extracts text from the active window without screenshots.

Uses Windows UI Automation to read text content directly from UI elements,
similar to how screen readers (NVDA, JAWS) work. Much faster than OCR and
free — but only works for apps that use standard Windows controls or expose
accessibility APIs (Chrome, VS Code, Office, most Windows apps).

Custom-rendered apps (WeChat, games, video players) fall back to empty output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class WindowText:
    """Text content extracted from the active window via UIA."""

    window_title: str = ""
    process_name: str = ""
    app_type: str = ""  # browser, editor, chat, unknown
    text_content: str = ""  # aggregated visible text, truncated
    element_count: int = 0
    walk_time_ms: float = 0.0
    is_accessible: bool = False  # True if UIA could read content

    @property
    def summary(self) -> str:
        if not self.is_accessible:
            return ""
        parts = [f"窗口：{self.window_title}"]
        if self.process_name:
            parts.append(f"进程：{self.process_name}")
        if self.app_type:
            parts.append(f"类型：{self.app_type}")
        if self.text_content.strip():
            parts.append(f"可见文字：\n{self.text_content.strip()}")
        return "\n".join(parts)


_MAX_TEXT_CHARS = 2000
_MAX_WALK_MS = 200  # hard timeout for tree walk
_MAX_DESCENDANTS = 500  # bail out after this many elements
_CONTENT_CONTROL_TYPES = frozenset({
    "TextControl", "EditControl", "DocumentControl",
    "HyperlinkControl", "ListItemControl", "TreeItemControl",
    "TabItemControl", "HeaderControl", "GroupControl",
    "DataItemControl", "HeaderItemControl",
})

_BROWSER_CLASSES = frozenset({
    "Chrome_WidgetWin_1", "MozillaWindowClass", "MozillaDialogClass",
})
_EDITOR_CLASSES = frozenset({
    "ATL:006D2C28",  # VS Code
    "Notepad", "Notepad++", "Scintilla",
})
_CHAT_CLASSES = frozenset({
    "Qt51514QWindowIcon",  # WeChat via Qt
    "WeChatMainWndForPC",
})


def _classify_app(window_class: str, control_types: set[str]) -> str:
    wc = window_class or ""
    if wc in _BROWSER_CLASSES:
        return "browser"
    if wc in _EDITOR_CLASSES or "Code" in wc or "Editor" in wc:
        return "editor"
    if wc in _CHAT_CLASSES or "Chat" in wc or "IM" in wc:
        return "chat"
    if "Document" in control_types or "Edit" in control_types:
        return "app_text"
    if not control_types:
        return "custom_ui"
    return "app"


def read_active_window() -> WindowText:
    """Extract text content from the foreground window via UIA.

    Returns WindowText with extracted content. On failure or unsupported
    windows, returns WindowText with is_accessible=False and empty text.
    """
    try:
        import uiautomation as auto
    except ImportError:
        return WindowText()

    try:
        hwnd = auto.GetForegroundWindow()
        if not hwnd:
            return WindowText()
        window = auto.ControlFromHandle(hwnd)
    except Exception:
        return WindowText()

    title = (window.Name or "").strip()
    klass = window.ClassName or ""

    result = WindowText(
        window_title=title,
        is_accessible=True,
    )

    # Process name via window class hint
    result.process_name = _guess_process_name(klass, title)

    # Fast walk
    start = time.perf_counter()
    elements: list[tuple[int, str, str]] = []  # (depth, control_type, name)
    control_types: set[str] = set()

    try:
        count_ref = [0]
        _walk_limited(window, 0, elements, control_types, count_ref,
                       start, _MAX_WALK_MS, _MAX_DESCENDANTS)
    except Exception as e:
        logger.debug("UIA walk interrupted: {}", e)

    elapsed = (time.perf_counter() - start) * 1000
    result.walk_time_ms = elapsed
    result.element_count = len(elements)
    result.app_type = _classify_app(klass, control_types)

    # Build text content — prefer deeper elements (more specific text)
    # Filter out UI chrome: minimize/close buttons, toolbar names, etc.
    text_parts: list[str] = []
    seen = set()
    for depth, ctype, name in elements:
        if not name or name in seen:
            continue
        if _is_ui_chrome(name):
            continue
        seen.add(name)
        text_parts.append(name)

    result.text_content = "\n".join(text_parts)[:_MAX_TEXT_CHARS]
    return result


# ---- internal helpers ----

def _walk_limited(
    control,
    depth: int,
    elements: list,
    control_types: set,
    count_ref: list[int],
    started_at: float,
    max_ms: float,
    max_elements: int,
) -> None:
    """Tree walk with time and element count limits."""
    if depth > 20 or count_ref[0] >= max_elements:
        return
    if (time.perf_counter() - started_at) * 1000 > max_ms:
        return

    try:
        child = control.GetFirstChildControl()
    except Exception:
        return

    while child and count_ref[0] < max_elements:
        if (time.perf_counter() - started_at) * 1000 > max_ms:
            return
        try:
            name = (child.Name or "").strip()
            ctype = child.ControlTypeName or ""
        except Exception:
            child = _safe_next_sibling(child)
            continue

        count_ref[0] += 1
        if ctype:
            control_types.add(ctype)
        if name and len(name) > 1 and ctype in _CONTENT_CONTROL_TYPES:
            elements.append((depth, ctype, name[:200]))

        _walk_limited(child, depth + 1, elements, control_types,
                       count_ref, started_at, max_ms, max_elements)
        child = _safe_next_sibling(child)


def _safe_next_sibling(control):
    try:
        return control.GetNextSiblingControl()
    except Exception:
        return None


def _is_ui_chrome(name: str) -> bool:
    """Filter out standard window chrome / toolbar noise."""
    low = name.lower()
    chrome_keywords = (
        "最小化", "最大化", "还原", "关闭", "minimize", "maximize", "restore", "close",
        "上一页", "下一页", "前进", "后退", "back", "forward",
        "刷新", "refresh", "主页", "home",
        "保存卡", "查看虚拟卡", "添加虚拟卡",
        "立即购买", "稍后支付", "保存地址",
        "详细了解", "了解更多", "learn more",
        "缩放", "zoom",
        "分屏", "split screen",
        "添加到收藏夹", "add to favorites",
        "在应用中打开", "open in app",
        "显示翻译选项", "translate",
        "进入阅读模式", "reading mode",
        "工作区按钮", "workspace",
    )
    for kw in chrome_keywords:
        if kw in low:
            return True
    return False


def _guess_process_name(window_class: str, title: str) -> str:
    """Guess process name from window class and title."""
    wc = window_class or ""
    if "Chrome_WidgetWin" in wc:
        # Edge or Chrome
        if "Edge" in (title or ""):
            return "msedge.exe"
        return "chrome.exe"
    if "MozillaWindowClass" in wc:
        return "firefox.exe"
    if "Qt5" in wc:
        return "qt_app.exe"
    if "Notepad" in wc:
        return "notepad.exe"
    if "ConsoleWindowClass" in wc:
        return "conhost.exe"
    if "CASCADIA" in wc.upper():
        return "windowsterminal.exe"
    return ""
