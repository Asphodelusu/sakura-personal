"""异步中文字幕翻译 — 最小接口（Phase 1）。

日语主回复与中文字幕解耦：主模型可只产出 ja/tone/portrait，
缺 zh 的 segment 交给 TranslationProvider 后补；失败时保留日语原文。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TranslationProvider(Protocol):
    """最小翻译接口；Phase 1 可用 Fake，不绑定具体供应商。"""

    def translate(
        self,
        texts: list[str],
        *,
        source_lang: str = "ja",
        target_lang: str = "zh",
    ) -> list[str]:
        """同步翻译一批文本；返回与 texts 等长的译文列表。

        实现方可抛出异常；调用方应捕获并保留原文，不得展示系统降级文案。
        """
        ...


class FakeTranslationProvider:
    """测试用假实现：在原文前加前缀，便于断言。"""

    def __init__(self, *, prefix: str = "译:") -> None:
        self.prefix = prefix
        self.calls: list[list[str]] = []

    def translate(
        self,
        texts: list[str],
        *,
        source_lang: str = "ja",
        target_lang: str = "zh",
    ) -> list[str]:
        self.calls.append(list(texts))
        return [f"{self.prefix}{text}" for text in texts]


class NoopTranslationProvider:
    """空实现：原样返回，相当于不翻译。"""

    def translate(
        self,
        texts: list[str],
        *,
        source_lang: str = "ja",
        target_lang: str = "zh",
    ) -> list[str]:
        return list(texts)
