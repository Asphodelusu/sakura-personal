from __future__ import annotations

import re


_JAPANESE_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 有实质英文词（≥3 字母）才切 auto；Lv100 / HP 等短标签不算。
_MEANINGFUL_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_LATIN_NOISE_TOKEN_RE = re.compile(
    r"(?i)\b(?:lv|hp|mp|exp|atk|def|ap|sp|np|cv|bd|dvd|pc|vr|ai|url|id|ok|ng|vs|ver|v)\d*\b"
)

_CHINESE_MARKERS = (
    "这个",
    "那个",
    "这些",
    "那些",
    "如果",
    "因为",
    "所以",
    "但是",
    "然后",
    "应该",
    "可以",
    "需要",
    "不能",
    "不会",
    "没有",
    "已经",
    "还是",
    "一下",
    "看看",
    "打开",
    "确认",
    "问题",
    "原因",
    "错误",
    "语法",
    "字符串",
    "节点",
)
_CHINESE_PUNCTUATION = "，？！；："
_COMMON_CHINESE_CHARS = set("我你的是了在有和不这那们把里吗吧呢")
_SIMPLIFIED_ONLY_CHARS = set("语错该节显这们为会览进开关")

# 日语台词里偶发混入简体字时，换成常见日文汉字，避免 GPT-SoVITS ja/auto 推理 400。
_JA_SIMP_TO_JP = str.maketrans(
    {
        "钻": "鑽",
        "脑": "脳",
        "战": "戦",
        "发": "発",
        "开": "開",
        "关": "関",
        "对": "対",
        "时": "時",
        "间": "間",
        "门": "門",
        "图": "図",
        "书": "書",
        "页": "頁",
        "码": "碼",
        "软": "軟",
        "件": "件",
        "网": "網",
        "络": "絡",
        "录": "録",
        "览": "覧",
        "检": "検",
        "测": "測",
        "试": "試",
        "读": "読",
        "写": "書",
        "说": "説",
        "话": "話",
        "语": "語",
        "错": "錯",
        "该": "該",
        "节": "節",
        "显": "顕",
        "为": "為",
        "会": "会",
        "进": "進",
        "这": "這",
        "们": "們",
        "国": "国",
        "学": "学",
    }
)


def should_skip_tts_text(text: str, target_lang: str) -> bool:
    """目标语音为日语时，明显中文的文本不送入 TTS。"""
    if not text.strip():
        return False

    normalized_lang = target_lang.strip().lower()
    if normalized_lang not in {"ja", "all_ja"}:
        return False

    return _looks_obvious_chinese(text)


def sanitize_tts_text_for_lang(text: str, target_lang: str) -> str:
    """为目标语种做轻量文本清洗，降低服务端合成失败率。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    normalized_lang = target_lang.strip().lower()
    # 日语目标下，把偶发简体字换成常见日文汉字（表外字符原样保留）。
    if normalized_lang in {"ja", "all_ja"}:
        return cleaned.translate(_JA_SIMP_TO_JP)
    return cleaned


def has_meaningful_latin_mix(text: str) -> bool:
    """是否含需要切到 auto 的实质英文（忽略 Lv100 等短标签）。"""
    residual = _LATIN_NOISE_TOKEN_RE.sub(" ", text or "")
    return bool(_MEANINGFUL_LATIN_WORD_RE.search(residual))


def _looks_obvious_chinese(text: str) -> bool:
    if _JAPANESE_KANA_RE.search(text):
        return False
    if not _CJK_RE.search(text):
        return False
    return (
        any(marker in text for marker in _CHINESE_MARKERS)
        or any(char in _CHINESE_PUNCTUATION for char in text)
        or sum(1 for char in text if char in _COMMON_CHINESE_CHARS) >= 2
        or any(char in _SIMPLIFIED_ONLY_CHARS for char in text)
    )
