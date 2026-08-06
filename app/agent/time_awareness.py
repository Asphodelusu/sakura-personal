"""相对时间与时长文案：给注入上下文用的轻量工具。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any


MIN_INTERACTION_GAP_SECONDS = 120  # 距上次互动 ≥ 2 分钟才写入文案

# parse_relative_time_window 对齐 format_relative_age 的词汇是近似逆映射，
# 不是一一对应（例如「约3小时前」只回一天内窗口，不会精确还原当时时刻）。
# 另支持「相对日期 + 时段 + 时间点/区间」组合（如「昨天晚上大约一点到两点」）。

# 时段默认窗：(start_hour, start_min, end_hour, end_min)；end 可跨日（如深夜）
_PERIOD_BOUNDS: dict[str, tuple[int, int, int, int]] = {
    "早上": (5, 0, 11, 0),
    "上午": (8, 0, 12, 0),
    "中午": (11, 0, 14, 0),
    "下午": (12, 0, 18, 0),
    "傍晚": (17, 0, 19, 30),
    "晚上": (18, 0, 24, 0),
    "夜里": (21, 0, 24, 0),
    "深夜": (22, 0, 5, 0),  # 跨日：当日 22:00 → 次日 05:00
    "凌晨": (0, 0, 5, 0),
}

_CN_WEEKDAY = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

_CN_HOUR = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


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


def parse_memory_event_date(event_time: str | None, *, now: datetime | None = None) -> date | None:
    """解析记忆 event_time（完整 ISO 或 YYYY-MM-DD）为本地日期。"""
    text = str(event_time or "").strip()
    if not text:
        return None
    current = now or datetime.now().astimezone()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=current.tzinfo)
            return parsed.astimezone(current.tzinfo).date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            continue
    return None


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


def parse_relative_time_window(
    text: str | None,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None] | None:
    """把中文相对时间 / 日期短语解析为本地 ISO 时间窗 (start, end)。

    - 空/空白 → (None, None)（表示不限时间）
    - 可解析 → (start_iso, end_iso)，本地时区、秒精度
    - 不可解析 → None

    这是 format_relative_age 的近似逆映射，不是精确还原。
    """
    raw = str(text or "").strip()
    if not raw:
        return (None, None)

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()

    def _day_bounds(day: date) -> tuple[str, str]:
        start = current.replace(
            year=day.year, month=day.month, day=day.day,
            hour=0, minute=0, second=0, microsecond=0,
        )
        end = current.replace(
            year=day.year, month=day.month, day=day.day,
            hour=23, minute=59, second=59, microsecond=0,
        )
        return (
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
        )

    def _to_now(start: datetime) -> tuple[str, str]:
        return (
            start.isoformat(timespec="seconds"),
            current.isoformat(timespec="seconds"),
        )

    if raw in {"刚才", "刚刚"}:
        return _to_now(current - timedelta(minutes=5))

    if raw in {"今天", "今天稍早"}:
        return _day_bounds(current.date())

    if raw == "昨天":
        return _day_bounds((current - timedelta(days=1)).date())

    if raw == "前天":
        return _day_bounds((current - timedelta(days=2)).date())

    minute_match = re.fullmatch(r"(\d+)\s*分钟前", raw)
    if minute_match:
        minutes = int(minute_match.group(1))
        center = current - timedelta(minutes=minutes)
        # 窄窗：中心前后各 2 分钟，并夹到不超过 now
        start = center - timedelta(minutes=2)
        end = min(center + timedelta(minutes=2), current)
        return (
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
        )

    hour_match = re.fullmatch(r"约?\s*(\d+)\s*小时前", raw)
    if hour_match:
        hours = int(hour_match.group(1))
        center = current - timedelta(hours=hours)
        # 一天内窗口：中心前后各 30 分钟
        start = center - timedelta(minutes=30)
        end = min(center + timedelta(minutes=30), current)
        return (
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
        )

    day_match = re.fullmatch(r"约?\s*(\d+)\s*天前", raw)
    if day_match:
        days = max(1, int(day_match.group(1)))
        return _day_bounds((current - timedelta(days=days)).date())

    week_match = re.fullmatch(r"约?\s*(\d+)\s*周前", raw)
    if week_match:
        weeks = max(1, int(week_match.group(1)))
        # 以「约 N 周前」那天为中心，取前后各 1 天（共约 3 天窗）
        center_day = (current - timedelta(weeks=weeks)).date()
        start_day = center_day - timedelta(days=1)
        end_day = center_day + timedelta(days=1)
        start_iso, _ = _day_bounds(start_day)
        _, end_iso = _day_bounds(end_day)
        if end_day >= current.date():
            end_iso = current.isoformat(timespec="seconds")
        return (start_iso, end_iso)

    month_match = re.fullmatch(r"约?\s*(\d+)\s*个月前", raw)
    if month_match:
        months = max(1, int(month_match.group(1)))
        center_day = (current - timedelta(days=30 * months)).date()
        start_day = center_day - timedelta(days=3)
        end_day = center_day + timedelta(days=3)
        start_iso, _ = _day_bounds(start_day)
        _, end_iso = _day_bounds(end_day)
        if end_day >= current.date():
            end_iso = current.isoformat(timespec="seconds")
        return (start_iso, end_iso)

    # YYYY-MM-DD → 整天
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return None
        return _day_bounds(day)

    # ISO 时间点 → 以该时刻为起点到现在；若在未来则单点窗
    point = parse_iso_datetime(raw)
    if point is not None:
        if point.tzinfo is None:
            point = point.replace(tzinfo=current.tzinfo)
        point = point.astimezone(current.tzinfo)
        if point > current:
            iso = point.isoformat(timespec="seconds")
            return (iso, iso)
        return (
            point.isoformat(timespec="seconds"),
            current.isoformat(timespec="seconds"),
        )

    # 相对日期 + 时段 + 时间点/区间（如「昨天晚上大约一点到两点」）
    composed = _parse_composed_time_window(raw, current)
    if composed is not None:
        return composed

    return None


def _parse_cn_hour_token(token: str) -> int | None:
    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 0 <= value <= 24 else None
    if text in _CN_HOUR:
        return _CN_HOUR[text]
    # 十X / 二十
    if text.startswith("十") and len(text) == 2:
        ones = _CN_HOUR.get(text[1])
        if ones is not None and 1 <= ones <= 9:
            return 10 + ones
    if text == "二十":
        return 20
    if text.startswith("二十") and len(text) == 3:
        ones = _CN_HOUR.get(text[2])
        if ones is not None and 1 <= ones <= 4:
            return 20 + ones
    return None


def _resolve_clock_on_day(
    base_day: date,
    hour: int,
    minute: int,
    *,
    period: str | None,
    current: datetime,
) -> datetime:
    """把「X点」落到具体日期时刻；晚上 1–4 点视为次日凌晨。"""
    h = hour
    day = base_day
    if period in {"下午"}:
        if 1 <= h <= 11:
            h += 12
    elif period in {"晚上", "夜里", "傍晚"}:
        if h == 12:
            # 晚上十二点 → 次日 00:00
            h = 0
            day = base_day + timedelta(days=1)
        elif 1 <= h <= 4:
            # 昨天晚上一点 = 今天 01:00
            day = base_day + timedelta(days=1)
        elif 5 <= h <= 11:
            h += 12
    elif period in {"凌晨", "深夜"}:
        if h == 12:
            h = 0
        elif 1 <= h <= 4:
            if period == "深夜":
                # 深夜 1-4 点 = 该日夜深已过午夜 = 次日凌晨（「前天深夜一点」= 昨天 01:00）
                day = base_day + timedelta(days=1)
        elif 5 <= h <= 11 and period == "深夜":
            # 深夜少见「十点」以外的说法；深夜十点=当天 22:00
            if h >= 6:
                h += 12
    elif period in {"早上", "上午", "中午"}:
        if h == 12 and period != "中午":
            h = 0
            day = base_day + timedelta(days=1)
    # 无时段：1–11 保持原样（按 24h 字面）；12 当作正午。
    # 无时段 + 凌晨小时（1-4）+ 非今天 → 昨日深夜 = 次日凌晨（「昨天一点到两点」= 次日 01:00）。
    if (
        period is None
        and 1 <= h <= 4
        and day == base_day
        and base_day != current.date()
    ):
        day = base_day + timedelta(days=1)
    return current.replace(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=min(h, 23),
        minute=minute,
        second=0,
        microsecond=0,
    )


def _period_window(
    base_day: date,
    period: str,
    *,
    current: datetime,
) -> tuple[datetime, datetime]:
    start_h, start_m, end_h, end_m = _PERIOD_BOUNDS[period]
    start = current.replace(
        year=base_day.year,
        month=base_day.month,
        day=base_day.day,
        hour=start_h,
        minute=start_m,
        second=0,
        microsecond=0,
    )
    if end_h >= 24:
        end_day = base_day + timedelta(days=1)
        end = current.replace(
            year=end_day.year,
            month=end_day.month,
            day=end_day.day,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(seconds=1)
    elif (end_h, end_m) <= (start_h, start_m):
        # 跨日（深夜）
        end_day = base_day + timedelta(days=1)
        end = current.replace(
            year=end_day.year,
            month=end_day.month,
            day=end_day.day,
            hour=end_h,
            minute=end_m,
            second=0,
            microsecond=0,
        )
    else:
        end = current.replace(
            year=base_day.year,
            month=base_day.month,
            day=base_day.day,
            hour=end_h,
            minute=end_m,
            second=0,
            microsecond=0,
        )
    return start, end


def _parse_composed_time_window(
    raw: str,
    current: datetime,
) -> tuple[str, str] | None:
    """解析「相对日期 + 时段词 + 时间点/区间」组合。"""
    # 去掉填充词，保留结构
    text = re.sub(r"(大约|大概|左右|差不多|将近|刚好)", "", raw)
    text = re.sub(r"\s+", "", text)
    if not text:
        return None

    base_day: date | None = None
    rest = text

    # 相对日期可出现在句中（「我昨天晚上…聊了什么」）
    week_m = re.search(r"上周([一二三四五六日天])", rest)
    if "昨天" in rest:
        base_day = (current - timedelta(days=1)).date()
        rest = rest.replace("昨天", "", 1)
    elif "前天" in rest:
        base_day = (current - timedelta(days=2)).date()
        rest = rest.replace("前天", "", 1)
    elif "今天" in rest:
        base_day = current.date()
        rest = rest.replace("今天", "", 1)
    elif week_m:
        target = _CN_WEEKDAY[week_m.group(1)]
        today = current.date()
        today_wd = today.weekday()
        days_since = (today_wd - target) % 7
        if days_since == 0:
            days_since = 7
        last_target = today - timedelta(days=days_since)
        week_start = today - timedelta(days=today_wd)
        # 「上周X」= 上一个日历周的该星期几
        if last_target >= week_start:
            last_target -= timedelta(days=7)
        base_day = last_target
        rest = rest[: week_m.start()] + rest[week_m.end() :]
    elif "上周" in rest:
        base_day = (current - timedelta(days=7)).date()
        rest = rest.replace("上周", "", 1)
    # N 天/周/月前（可带「约」）
    elif (m := re.search(r"约?\s*([零〇一二两三四五六七八九十\d]{1,3})\s*天前", rest)):
        days = max(1, _parse_cn_hour_token(m.group(1)) or 1)
        base_day = (current - timedelta(days=days)).date()
        rest = rest[: m.start()] + rest[m.end() :]
    elif (m := re.search(r"约?\s*([零〇一二两三四五六七八九十\d]{1,3})\s*个?(?:星期|周)前", rest)):
        weeks = max(1, _parse_cn_hour_token(m.group(1)) or 1)
        base_day = (current - timedelta(weeks=weeks)).date()
        rest = rest[: m.start()] + rest[m.end() :]
    elif (m := re.search(r"约?\s*([零〇一二两三四五六七八九十\d]{1,3})\s*个月前", rest)):
        months = max(1, _parse_cn_hour_token(m.group(1)) or 1)
        base_day = (current - timedelta(days=30 * months)).date()
        rest = rest[: m.start()] + rest[m.end() :]

    period: str | None = None
    for key in sorted(_PERIOD_BOUNDS, key=len, reverse=True):
        idx = rest.find(key)
        if idx >= 0:
            period = key
            rest = rest[:idx] + rest[idx + len(key) :]
            break

    hour_token = r"([零〇一二两三四五六七八九十\d]{1,3})"
    clock = rf"{hour_token}点(半)?"
    range_m = re.search(
        clock + r"[到至\-—~～]" + clock,
        rest,
    )
    point_m = None if range_m else re.search(clock, rest)

    # 句中夹杂无关字时：必须至少解析出相对日期或时段，避免误伤闲聊
    if base_day is None and period is None:
        return None
    if base_day is None:
        base_day = current.date()
    # 有日期但没有任何时段/钟点，且去掉日期后仍有大量残留 → 不当时间窗
    if period is None and range_m is None and point_m is None:
        leftover = re.sub(r"[的那会会儿段时间里]", "", rest)
        if leftover:
            return None

    def _iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    if range_m:
        h1 = _parse_cn_hour_token(range_m.group(1))
        h2 = _parse_cn_hour_token(range_m.group(3))
        if h1 is None or h2 is None:
            return None
        m1 = 30 if range_m.group(2) else 0
        m2 = 30 if range_m.group(4) else 0
        start_dt = _resolve_clock_on_day(
            base_day, h1, m1, period=period, current=current
        )
        end_dt = _resolve_clock_on_day(
            base_day, h2, m2, period=period, current=current
        )
        # 结束点若未写「半」，默认落到该小时末（两点 → 02:59:59）
        if not range_m.group(4):
            end_dt = end_dt.replace(minute=59, second=59)
        if end_dt < start_dt:
            end_dt += timedelta(days=1)
        return (_iso(start_dt), _iso(end_dt))

    if point_m:
        h = _parse_cn_hour_token(point_m.group(1))
        if h is None:
            return None
        minute = 30 if point_m.group(2) else 0
        center = _resolve_clock_on_day(
            base_day, h, minute, period=period, current=current
        )
        # 单点：前后各 30 分钟
        start_dt = center - timedelta(minutes=30)
        end_dt = center + timedelta(minutes=30)
        if not point_m.group(2):
            # 整点时用该小时窗更符合「十点」语感
            start_dt = center
            end_dt = center.replace(minute=59, second=59)
        return (_iso(start_dt), _iso(end_dt))

    if period is not None:
        start_dt, end_dt = _period_window(base_day, period, current=current)
        return (_iso(start_dt), _iso(end_dt))

    # 仅有相对日期、无时段/钟点 → 整天（与「昨天」等精确匹配一致，这里兜底组合残留）
    if rest == "" or rest in {"的", "那会", "那会儿", "那段"}:
        start = current.replace(
            year=base_day.year,
            month=base_day.month,
            day=base_day.day,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        end = current.replace(
            year=base_day.year,
            month=base_day.month,
            day=base_day.day,
            hour=23,
            minute=59,
            second=59,
            microsecond=0,
        )
        return (_iso(start), _iso(end))

    return None


def annotate_with_relative_age(
    content: str,
    iso_timestamp: str | None,
    *,
    now: datetime | None = None,
    expired: bool = False,
    expired_label: str = "已失效",
) -> str:
    """给正文加相对年龄 / 过期前缀。"""
    text = content.strip()
    if not text:
        return text
    parts: list[str] = []
    if expired:
        label = (expired_label or "已失效").strip() or "已失效"
        parts.append(label)
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
        duration = format_duration_zh(seconds_since_interaction)
        lines.append(f"距上次对话约 {duration}（这是客观间隔，不是主观感受）。")
        lines.append(
            f"若回复里提到过了多久，请用约 {duration}，不要说成明显更短的时间。"
        )
    lines.append("本机时刻是只读临时状态，只用于回答当前问题，不要写入长期记忆。")
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
