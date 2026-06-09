"""底部控制组（对话气泡 + 输入栏）的可调布局参数与归一化。

气泡与输入栏在新架构里是各自独立的顶层卡片窗口（AcrylicCardWindow），
其尺寸/位置由 PetWindow._layout_stage 计算的本地矩形决定。这里集中存放
三个用户可调参数的取值范围与归一化逻辑：

- control_panel_width：控制组（气泡与输入栏共用）的宽度
- bubble_height：气泡卡片的高度
- vertical_offset：控制组整体的上下偏移（正值=向上抬升，远离屏幕底部）

独立成模块是为了让 PetWindow 与 SettingsDialog 都能引用，又不引入二者之间的
循环导入（PetWindow 已经 import SettingsDialog）。本模块保持零外部依赖。
"""

from __future__ import annotations

# 控制组宽度（气泡与输入栏共用同一宽度）
DEFAULT_CONTROL_PANEL_WIDTH = 640
MIN_CONTROL_PANEL_WIDTH = 420
MAX_CONTROL_PANEL_WIDTH = 860

# 气泡卡片高度
DEFAULT_BUBBLE_HEIGHT = 128
MIN_BUBBLE_HEIGHT = 96
MAX_BUBBLE_HEIGHT = 260

# 控制组整体上下偏移：正值向上抬升，负值向下沉。0 为原始默认位置。
DEFAULT_CONTROL_PANEL_VERTICAL_OFFSET = 0
MIN_CONTROL_PANEL_VERTICAL_OFFSET = -200
MAX_CONTROL_PANEL_VERTICAL_OFFSET = 200

# 布局固定量：输入栏高度、气泡与输入栏间距、控制组距舞台底部的基础留白。
# 取自重构前 _layout_stage 中的硬编码值，提取为常量便于布局统一引用。
INPUT_BAR_HEIGHT = 52
CONTROL_PANEL_GAP = 10
CONTROL_PANEL_BOTTOM_MARGIN = 84


def _clamp_int(value: object, *, minimum: int, maximum: int, default: int) -> int:
    """把任意输入归一化为 [minimum, maximum] 内的整数；无法解析时回退默认值。

    兼容配置文件里写成字符串/浮点的情况（如 "640"、640.0）。
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def normalize_control_panel_width(value: object) -> int:
    return _clamp_int(
        value,
        minimum=MIN_CONTROL_PANEL_WIDTH,
        maximum=MAX_CONTROL_PANEL_WIDTH,
        default=DEFAULT_CONTROL_PANEL_WIDTH,
    )


def normalize_bubble_height(value: object) -> int:
    return _clamp_int(
        value,
        minimum=MIN_BUBBLE_HEIGHT,
        maximum=MAX_BUBBLE_HEIGHT,
        default=DEFAULT_BUBBLE_HEIGHT,
    )


def normalize_control_panel_vertical_offset(value: object) -> int:
    return _clamp_int(
        value,
        minimum=MIN_CONTROL_PANEL_VERTICAL_OFFSET,
        maximum=MAX_CONTROL_PANEL_VERTICAL_OFFSET,
        default=DEFAULT_CONTROL_PANEL_VERTICAL_OFFSET,
    )
