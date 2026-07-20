"""相对时间与时长文案：给注入上下文用的轻量工具。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


MIN_INTERACTION_GAP_SECONDS = 120  # 距上次互动 ≥ 2 分钟才写入文案


def parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        then = datetime.fromisoformat(text)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.astimezone()
    return then


def seconds_since(iso_timestamp: str | None, *, now: datetime | None = None) -> int | None:
    then = parse_iso_datetime(iso_timestamp)
    if then is None:
        return None
    current = now or datetime.now().astimezone()
    if then.tzinfo is None:
        then = then.astimezone()
    delta = (current - then).total_seconds()
    if delta < 0:
        return None
    return int(delta)


def format_duration_zh(seconds: float) -> str:
    """把秒数格式化成「N 分钟 / N 小时 M 分钟」等人话。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes:
        return f"{hours} 小时 {remaining_minutes} 分钟"
    return f"{hours} 小时"


def format_relative_age(iso_timestamp: str | None, *, now: datetime | None = None) -> str:
    """相对年龄标签（不含括号）：刚才 / N分钟前 / 约N小时前 / …

    解析失败返回空串。
    """
    then = parse_iso_datetime(iso_timestamp)
    if then is None:
        return ""
    current = now or datetime.now().astimezone()
    if then.tzinfo is None:
        then = then.astimezone()
    delta = current - then
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    if seconds < 90:
        return "刚才"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 6:
        return f"约{hours}小时前"
    if then.date() == current.date():
        return "今天稍早"
    yesterday = (current - timedelta(days=1)).date()
    if then.date() == yesterday:
        return "昨天"
    days = delta.days
    if days < 7:
        return f"约{max(days, 1)}天前"
    weeks = days // 7
    if weeks < 5:
        return f"约{weeks}周前"
    months = max(1, days // 30)
    return f"约{months}个月前"


def annotate_with_relative_age(
    content: str,
    iso_timestamp: str | None,
    *,
    now: datetime | None = None,
    expired: bool = False,
) -> str:
    """给正文加相对年龄 / 过期前缀。"""
    text = content.strip()
    if not text:
        return text
    parts: list[str] = []
    if expired:
        parts.append("已过期的约定")
    age = format_relative_age(iso_timestamp, now=now)
    if age:
        parts.append(age)
    if not parts:
        return text
    return f"（{' · '.join(parts)}）{text}"


def memory_event_timestamp(memory: dict[str, Any]) -> str:
    """优先 event_time，其次 created_at / updated_at。"""
    meta = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
    for key in ("event_time", "created_at", "updated_at"):
        value = memory.get(key) or meta.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def format_local_time_context(
    current_time: str,
    *,
    seconds_since_interaction: float | None = None,
    min_gap_seconds: int = MIN_INTERACTION_GAP_SECONDS,
) -> str:
    """构建 runtime.time 注入文案。"""
    lines = [f"当前本地时间：{current_time}"]
    period = _day_period_label(current_time)
    if period:
        lines.append(f"时段：{period}")
    if (
        isinstance(seconds_since_interaction, (int, float))
        and seconds_since_interaction >= min_gap_seconds
    ):
        lines.append(f"距上次和对方互动约 {format_duration_zh(seconds_since_interaction)}。")
    return "\n".join(lines)


def _day_period_label(current_time: str) -> str:
    then = parse_iso_datetime(current_time)
    if then is None:
        try:
            hour = int(str(current_time)[11:13])
        except (TypeError, ValueError, IndexError):
            return ""
    else:
        hour = then.hour
    if 5 <= hour < 11:
        return "早晨"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜/凌晨"
