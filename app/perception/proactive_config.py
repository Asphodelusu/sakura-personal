"""ProactiveObserver 运行时配置（轻量模块，可供 settings 层安全导入）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProactiveConfig:
    enabled: bool = True
    timer_seconds: float = 480
    cooldown_seconds: float = 600
    min_silence_after_user: float = 10
    window_switch_enabled: bool = True
    # APP_FOCUS 类型冷却（对齐同类 ~20s；冷却内切换记 deferred，结束后补票）
    window_switch_cooldown: float = 25
    # 前台稳定多久才算一次切应用触发（快切会不断重置）
    focus_settle_delay: float = 15
    idle_threshold_seconds: float = 600
    poll_interval: float = 5.0
    content_check_interval: float = 30.0
    content_min_chars: int = 30
    # 暂时默认关闭：WinRT OCR 在游戏窗上常超时(~8s)，拖慢评估且收益不稳。
    game_ocr_enabled: bool = False
    max_edge: int = 1920
    request_timeout: float = 60.0
    eval_temperature: float = 0.7
    max_tokens: int = 1024
    adaptive_interval_min: float = 45.0
    adaptive_interval_max: float = 1800.0
    away_max_seconds: float = 12 * 3600

    @classmethod
    def from_dict(cls, d: dict | None) -> "ProactiveConfig":
        if not isinstance(d, dict):
            return cls()
        base = cls()
        return cls(
            enabled=bool(d.get("enabled", base.enabled)),
            timer_seconds=float(d.get("timer_seconds", base.timer_seconds)),
            cooldown_seconds=float(d.get("cooldown_seconds", base.cooldown_seconds)),
            min_silence_after_user=float(
                d.get("min_silence_after_user", base.min_silence_after_user)
            ),
            window_switch_enabled=bool(
                d.get("window_switch_enabled", base.window_switch_enabled)
            ),
            window_switch_cooldown=float(
                d.get("window_switch_cooldown", base.window_switch_cooldown)
            ),
            focus_settle_delay=float(d.get("focus_settle_delay", base.focus_settle_delay)),
            idle_threshold_seconds=float(
                d.get("idle_threshold_seconds", base.idle_threshold_seconds)
            ),
            poll_interval=float(d.get("poll_interval", base.poll_interval)),
            content_check_interval=float(
                d.get("content_check_interval", base.content_check_interval)
            ),
            content_min_chars=int(d.get("content_min_chars", base.content_min_chars)),
            game_ocr_enabled=bool(d.get("game_ocr_enabled", base.game_ocr_enabled)),
            max_edge=int(d.get("max_edge", base.max_edge)),
            request_timeout=float(d.get("request_timeout", base.request_timeout)),
            eval_temperature=float(d.get("eval_temperature", base.eval_temperature)),
            max_tokens=int(d.get("max_tokens", base.max_tokens)),
            adaptive_interval_min=float(
                d.get("adaptive_interval_min", base.adaptive_interval_min)
            ),
            adaptive_interval_max=float(
                d.get("adaptive_interval_max", base.adaptive_interval_max)
            ),
            away_max_seconds=float(d.get("away_max_seconds", base.away_max_seconds)),
        )
