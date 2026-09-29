"""GPT-SoVITS output gate: a silent WAV must not count as successful synthesis."""

from __future__ import annotations

import wave
from pathlib import Path

from plugins.builtin.sakura_gpt_sovits._support import _verify_wav, _wav_problem


def _write_wav(path: Path, sample: bytes, *, frames: int = 21_000, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(width)
        handle.setframerate(32_000)
        handle.writeframes(sample * frames)
    return path


def test_all_zero_pcm_is_rejected_as_silent(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "zero.wav", b"\x00\x00")

    assert _wav_problem(path) == "TTS_AUDIO_SILENT"
    assert _verify_wav(path) is False


def test_near_silent_pcm_is_rejected(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "hiss.wav", (20).to_bytes(2, "little", signed=True))

    assert _wav_problem(path) == "TTS_AUDIO_SILENT"


def test_audible_pcm_passes(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "voice.wav", (1000).to_bytes(2, "little", signed=True))

    assert _wav_problem(path) is None
    assert _verify_wav(path) is True


def test_truncated_wav_is_a_format_problem(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "cut.wav", (1000).to_bytes(2, "little", signed=True))
    path.write_bytes(path.read_bytes()[:30])

    assert _wav_problem(path) == "TTS_AUDIO_FORMAT_INVALID"
    assert _verify_wav(path) is False
