"""翻译 provider 抽象与实现（OpenAI 兼容 API）。

key 从 data/config/api.yaml 读取，不硬编码、不打印。
DeepL / 本地 fallback 为接口占位（无 key 时不参与）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

API_YAML = Path("data/config/api.yaml")

_TERM_GLOSSARY = """术语表（保持固定译法，不要改译名）：
- B.E.G. → B.E.G.（愿力武装）
- ウィドウ・メーカー → Widow Maker（寡妇制造者）
- 生徒会長 → 学生会长
- 黒列車 → 黑列车
- 夜乃桜 → 夜乃樱
- 横浜支部 → 横滨支部
"""

_TRANSLATE_SYSTEM = (
    "你是夜乃樱的中文翻译，只把日语翻译成自然、贴合角色语气的中文。\n"
    "要求：\n"
    "- 保留语气：撒娇、傲娇、吃醋、害羞、请求、亲昵都要在中文里体现出来\n"
    "- 日语常省略主语：翻译时补出合适的主语，或保持自然，不要逐字生硬\n"
    "- 称谓（君/さん/ちゃん/あんた/お前 等）选择合适的中文译法\n"
    "- 只输出 JSON，格式：{\"zh\": \"中文翻译\"}\n"
    + _TERM_GLOSSARY
)


@dataclass
class TranslationResult:
    ok: bool
    zh: str = ""
    latency_ms: float = 0.0
    error: str = ""
    raw: str = ""


class BaseProvider:
    name: str = "base"
    available: bool = False

    def translate(self, ja: str) -> TranslationResult:  # pragma: no cover - 接口
        raise NotImplementedError


class OpenAILikeProvider(BaseProvider):
    """OpenAI 兼容 chat/completions，要求返回 {"zh": "..."} JSON。"""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 30.0, json_format: bool = False):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.json_format = json_format
        self.available = bool(api_key and base_url)

    def _parse_json_zh(self, raw: str) -> str | None:
        text = raw.strip()
        # 去掉可能的 markdown 代码块围栏
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # 尝试截取第一个 { 到最后一个 }
            try:
                obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
            except Exception:
                return None
        if isinstance(obj, dict) and isinstance(obj.get("zh"), str):
            return obj["zh"]
        return None

    def translate(self, ja: str) -> TranslationResult:
        if not self.available:
            return TranslationResult(ok=False, error="provider 不可用")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _TRANSLATE_SYSTEM},
                {"role": "user", "content": ja},
            ],
            "temperature": 0.3,
            "max_tokens": 512,
        }
        if self.json_format:
            payload["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            if resp.status_code != 200:
                return TranslationResult(ok=False, latency_ms=latency_ms, error=f"HTTP {resp.status_code}")
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            zh = self._parse_json_zh(raw)
            if zh is None:
                return TranslationResult(ok=False, latency_ms=latency_ms, error="JSON 解析失败", raw=raw[:200])
            return TranslationResult(ok=True, zh=zh, latency_ms=latency_ms, raw=raw)
        except httpx.TimeoutException:
            return TranslationResult(ok=False, error="timeout")
        except Exception as exc:  # noqa: BLE001
            return TranslationResult(ok=False, error=str(exc)[:200])


class DeepLPlaceholder(BaseProvider):
    """无 key，接口占位。"""
    name = "deepl"
    available = False

    def translate(self, ja: str) -> TranslationResult:
        return TranslationResult(ok=False, error="DeepL 未配置 key")


class LocalFallbackPlaceholder(BaseProvider):
    """本地 fallback 接口占位。"""
    name = "local_fallback"
    available = False

    def translate(self, ja: str) -> TranslationResult:
        return TranslationResult(ok=False, error="本地 fallback 未实现")


def _load_profiles() -> list[dict]:
    if not API_YAML.exists():
        return []
    raw = yaml.safe_load(API_YAML.read_text(encoding="utf-8")) or {}
    return list(raw.get("api_profiles") or [])


def make_providers() -> dict[str, BaseProvider]:
    profiles = _load_profiles()
    by_id = {p.get("id"): p for p in profiles if isinstance(p, dict)}

    providers: dict[str, BaseProvider] = {}
    text = by_id.get("text-profile")
    if text:
        providers["deepseek_flash"] = OpenAILikeProvider(
            "deepseek_flash",
            str(text.get("base_url") or ""),
            str(text.get("api_key") or ""),
            "deepseek-v4-flash",
            json_format=True,
        )
    dash = by_id.get("dashscope")
    if dash:
        providers["qwen"] = OpenAILikeProvider(
            "qwen",
            str(dash.get("base_url") or ""),
            str(dash.get("api_key") or ""),
            "qwen3.7-plus",
        )
    providers["deepl"] = DeepLPlaceholder()
    providers["local_fallback"] = LocalFallbackPlaceholder()
    return providers
