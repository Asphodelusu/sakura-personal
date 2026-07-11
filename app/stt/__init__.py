try:
    import sherpa_onnx  # noqa: F401
    _STT_AVAILABLE = True
except ImportError:
    _STT_AVAILABLE = False

if _STT_AVAILABLE:
    from app.stt.manager import STTManager
    from app.stt.recognizer import SenseVoiceRecognizer
else:
    STTManager = None  # type: ignore
    SenseVoiceRecognizer = None  # type: ignore

__all__ = ["STTManager", "SenseVoiceRecognizer"]
