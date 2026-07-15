"""Lightweight screen capture via mss — no Qt dependency.

Captures the primary monitor, downsamples to max_edge, and returns
a base64 PNG suitable for vision LLM consumption.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from datetime import UTC, datetime

import mss
from loguru import logger
from PIL import Image


@dataclass
class ScreenObservation:
    """A single screen capture, normalized for VLM consumption."""

    ts: str
    width: int
    height: int
    image_b64: str  # base64-encoded PNG, no data: prefix
    monitor_index: int
    mime: str = "image/png"
    dhash: int = 0  # 64-bit difference hash for frame dedup (0 = not computed)


class ScreenCapture:
    def __init__(self, max_edge: int = 1024, monitor_index: int = 1, window_only: bool = True) -> None:
        """monitor_index: 0=all monitors stitched, 1=primary (mss convention).
        window_only: if True, capture only the foreground window rect."""
        self.max_edge = max_edge
        self.monitor_index = monitor_index
        self.window_only = window_only

    def grab(self, monitor_index: int | None = None) -> ScreenObservation:
        idx = monitor_index if monitor_index is not None else self.monitor_index

        # Window-only mode: capture just the foreground window rect
        if self.window_only:
            try:
                from ctypes import windll, byref
                from ctypes.wintypes import RECT
                hwnd = windll.user32.GetForegroundWindow()
                if hwnd:
                    rect = RECT()
                    windll.user32.GetWindowRect(hwnd, byref(rect))
                    # Skip tiny/minimized windows
                    w, h = rect.right - rect.left, rect.bottom - rect.top
                    if w > 100 and h > 100:
                        mon = {"left": rect.left, "top": rect.top, "width": w, "height": h}
                        with mss.MSS() as sct:
                            raw = sct.grab(mon)
                        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                        orig_w, orig_h = img.size
                        long_edge = max(orig_w, orig_h)
                        if long_edge > self.max_edge:
                            scale = self.max_edge / long_edge
                            new_size = (int(orig_w * scale), int(orig_h * scale))
                            img = img.resize(new_size, Image.LANCZOS)
                        frame_hash = _compute_dhash(img)
                        buf = io.BytesIO()
                        img.save(buf, format="PNG", optimize=True)
                        png_bytes = buf.getvalue()
                        b64 = base64.b64encode(png_bytes).decode("ascii")
                        logger.debug(
                            "screen captured (window): rect=({},{}) {}x{} sent={}x{} png={}KB dhash={:016x}",
                            rect.left, rect.top, orig_w, orig_h, img.size[0], img.size[1],
                            len(png_bytes) // 1024, frame_hash,
                        )
                        return ScreenObservation(
                            ts=datetime.now(UTC).isoformat(),
                            width=img.size[0],
                            height=img.size[1],
                            image_b64=b64,
                            monitor_index=-1,  # -1 = window capture
                            dhash=frame_hash,
                        )
            except Exception as e:
                logger.debug("window-only capture failed, falling back to monitor: {}", e)

        # Fallback: full monitor capture
        with mss.MSS() as sct:
            if idx >= len(sct.monitors):
                logger.warning("monitor {} not found, falling back to all", idx)
                idx = 0
            mon = sct.monitors[idx]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        orig_w, orig_h = img.size
        long_edge = max(orig_w, orig_h)
        if long_edge > self.max_edge:
            scale = self.max_edge / long_edge
            new_size = (int(orig_w * scale), int(orig_h * scale))
            img = img.resize(new_size, Image.LANCZOS)

        frame_hash = _compute_dhash(img)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()

        b64 = base64.b64encode(png_bytes).decode("ascii")
        logger.debug(
            "screen captured: monitor={} orig={}x{} sent={}x{} png={}KB dhash={:016x}",
            idx, orig_w, orig_h, img.size[0], img.size[1], len(png_bytes) // 1024, frame_hash,
        )
        return ScreenObservation(
            ts=datetime.now(UTC).isoformat(),
            width=img.size[0],
            height=img.size[1],
            image_b64=b64,
            monitor_index=idx,
            dhash=frame_hash,
        )


def _compute_dhash(img: Image.Image) -> int:
    """Compute 64-bit difference hash for frame dedup.

    Downsamples to 9×8 grayscale, then sets bit when left pixel > right pixel.
    Hamming distance ≤ 4 between two dhashes = near-identical frames.
    """
    small = img.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(small.get_flattened_data())
    dhash = 0
    for row in range(8):
        for col in range(8):
            if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                dhash |= 1 << (row * 8 + col)
    return dhash
