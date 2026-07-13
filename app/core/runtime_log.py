"""app/core/runtime_log.py — thin compatibility shim

Upstream v0.9.9 renamed debug_log → runtime_log.  We keep debug_log as the
canonical module and provide log_event() by delegating.
"""

from __future__ import annotations

from typing import Any

from app.core.debug_log import debug_log


def log_event(
    category: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log a structured runtime event (delegates to debug_log)."""
    debug_log(category, message, details or {})
