"""STT 测试工具

用法:
    python scripts/test_stt.py              # 离线文件测试
    python scripts/test_stt.py --live       # 麦克风实时测试
    python scripts/test_stt.py --devices    # 列出录音设备
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.stt import SenseVoiceRecognizer, STTManager


def test_offline():
    """用内置测试音频验证模型加载和识别。"""
    model_dir = Path("D:/sakura/data/models/sense-voice")
    recognizer = SenseVoiceRecognizer.get_instance(str(model_dir))

    for lang in ["zh", "en"]:
        wav = model_dir / "test_wavs" / f"{lang}.wav"
        if wav.exists():
            import soundfile as sf
            audio, sr = sf.read(str(wav), dtype="float32", always_2d=True)
            audio = audio[:, 0]
            text = recognizer.transcribe(audio, sr)
            print(f"[{lang}] {text}")


def test_live():
    """麦克风实时测试。"""
    manager = STTManager()
    if not manager.available:
        print("错误: 未找到语音识别模型")
        return

    print("按 Enter 开始录音，说完话自动停止...")
    input()

    manager.start_listening()
    print("🎤 录音中...")

    text = manager.finish_listening()
    if text:
        print(f"📝 {text}")
    else:
        print("未识别到语音")


def test_devices():
    from app.stt.capture import AudioCapture
    for d in AudioCapture.list_devices():
        print(f"  [{d['index']}] {d['name']}")


if __name__ == "__main__":
    if "--live" in sys.argv:
        test_live()
    elif "--devices" in sys.argv:
        test_devices()
    else:
        test_offline()
