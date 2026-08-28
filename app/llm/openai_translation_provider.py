"""OpenAI 兼容的中文字幕翻译 provider。

复用已装配的 chat_fast 客户端 complete_raw，不重复加载 HTTP/密钥。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from app.core.debug_log import debug_log


TRANSLATION_SYSTEM_PROMPT = (
    "把日语翻译成自然的简体中文。只输出译文，不要解释，不要拼音，不要日语原文。"
)
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class TranslationError(RuntimeError):
    """翻译失败；调用方应保留日语原文，不得把原文当作 zh。"""


class OpenAITranslationProvider:
    """批量翻译；单条失败抛异常，由调用方保留日语。"""

    def __init__(
        self,
        client: Any,
        *,
        max_attempts: int = 2,
        provider_name: str = "chat_fast",
    ) -> None:
        self.client = client
        self.max_attempts = max(1, min(2, int(max_attempts)))
        self.provider_name = provider_name

    def translate(
        self,
        texts: list[str],
        *,
        source_lang: str = "ja",
        target_lang: str = "zh",
    ) -> list[str]:
        _ = source_lang, target_lang
        started = time.perf_counter()
        attempts = 0
        try:
            translations: list[str] = []
            for text in texts:
                zh, used = self._translate_one(str(text or ""))
                attempts += used
                if not zh:
                    raise TranslationError("invalid_or_empty_translation")
                translations.append(zh)
        except Exception:
            self._log_outcome(
                "failed",
                attempts=attempts or 1,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        self._log_outcome(
            "success",
            attempts=attempts,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        return translations

    def _translate_one(self, text: str) -> tuple[str, int]:
        used = 0
        for _attempt in range(self.max_attempts):
            used += 1
            try:
                raw = self.client.complete_raw(
                    TRANSLATION_SYSTEM_PROMPT,
                    [{"role": "user", "content": text}],
                    temperature=0.3,
                    max_attempts=1,
                    max_tokens=512,
                )
            except Exception:
                continue
            zh = _normalize_chinese_output(raw)
            if zh:
                return zh, used
        return "", used

    def _log_outcome(self, outcome: str, *, attempts: int, elapsed_ms: float) -> None:
        settings = getattr(self.client, "settings", None)
        model = str(getattr(settings, "model", "") or "")
        debug_log(
            "Translation",
            "sidecar completed",
            {
                "provider": self.provider_name,
                "model": model,
                "outcome": outcome,
                "attempts": int(attempts),
                "elapsed_ms": round(float(elapsed_ms), 1),
            },
        )


def build_translation_provider(
    settings: Any,
    chat_fast_client: Any,
) -> OpenAITranslationProvider | None:
    if settings is None or not bool(getattr(settings, "enabled", False)):
        return None
    if chat_fast_client is None:
        return None
    return OpenAITranslationProvider(
        chat_fast_client,
        max_attempts=int(getattr(settings, "max_attempts", 2) or 2),
    )


def _normalize_chinese_output(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    extracted = _extract_json_zh(text)
    if extracted:
        text = extracted
    if not _is_valid_simplified_chinese(text):
        return ""
    return text


def _extract_json_zh(text: str) -> str:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return ""
    if isinstance(obj, dict) and isinstance(obj.get("zh"), str):
        return str(obj["zh"]).strip()
    return ""


def _is_valid_simplified_chinese(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if _KANA_RE.search(stripped):
        return False
    return bool(_CJK_RE.search(stripped))
