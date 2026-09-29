"""Character voice authority; legacy fields are read only for unconverted packs."""
from collections.abc import Mapping

GPT_SOVITS_EXTENSION = "sakura.tts.gpt-sovits"
VOICE_FIELDS = {
    "tone_refs": "toneRefs", "gpt_model": "gptModel", "sovits_model": "sovitsModel",
    "ref_lang": "refLang", "text_lang": "textLang",
}


def voice_fields(manifest: Mapping) -> dict | None:
    extensions = manifest.get("extensions", {})
    if not isinstance(extensions, Mapping):
        raise ValueError("character.extensions 必须是对象。")
    if GPT_SOVITS_EXTENSION in extensions:
        provider = extensions[GPT_SOVITS_EXTENSION]
        if not isinstance(provider, Mapping):
            raise ValueError("sakura.tts.gpt-sovits 必须是对象。")
        values = {old: provider[new] for old, new in VOICE_FIELDS.items() if new in provider}
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("GPT-SoVITS 语音字段必须是字符串。")
        return values or None
    legacy = manifest.get("voice")
    if legacy is not None and not isinstance(legacy, Mapping):
        raise ValueError("voice 必须是对象。")
    return dict(legacy) if legacy is not None else None


def write_voice_fields(manifest: dict, voice: Mapping | None) -> None:
    """Publish only extension fields; keep unknown provider/plugin options."""
    extensions = dict(manifest.get("extensions") or {})
    if voice is None:
        provider = dict(extensions.get(GPT_SOVITS_EXTENSION) or {})
        for key in VOICE_FIELDS.values():
            provider.pop(key, None)
        if provider:
            extensions[GPT_SOVITS_EXTENSION] = provider
        else:
            extensions.pop(GPT_SOVITS_EXTENSION, None)
    else:
        provider = dict(extensions.get(GPT_SOVITS_EXTENSION) or {})
        for old, new in VOICE_FIELDS.items():
            if old in voice and voice[old] is not None:
                provider[new] = voice[old]
            else:
                provider.pop(new, None)
        extensions[GPT_SOVITS_EXTENSION] = provider
    extensions.pop("sakura.tts", None)
    manifest.pop("voice", None)
    if extensions:
        manifest["extensions"] = extensions
    else:
        manifest.pop("extensions", None)
