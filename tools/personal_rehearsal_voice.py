"""Own one local GPT-SoVITS process for an already prepared isolated rehearsal."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from app.plugin_sdk.sakura_process import terminate_process_tree


@contextmanager
def managed_voice(runtime: Path, target: Path, *, port: int = 19880,
                  user_root: Path | None = None, character_id: str = "sakura"):
    runtime, target = runtime.resolve(), target.resolve()
    python, script = runtime / "runtime/python.exe", runtime / "api_v2.py"
    profile = target / "tts-infer.yaml"
    user = Path(user_root).resolve() if user_root is not None else target / "user"
    hub = user / "data/plugins/sakura.tts/config.json"
    provider = user / "data/plugins/sakura.tts.gpt-sovits/config.json"
    for path in (python, script, profile, hub, provider):
        if not path.is_file():
            raise FileNotFoundError(path)
    # Never adopt or stop a pre-existing service, including the daily instance.
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"Rehearsal TTS port {port} is already in use") from error
    before = {path: path.read_bytes() for path in (hub, provider)}
    process = None
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                   "PYTHONIOENCODING": "utf-8", "NUMBA_CACHE_DIR": str(target / "tts-numba-cache")}
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    with (target / "tts-service.log").open("a", encoding="utf-8") as log:
        try:
            print(f"Starting owned rehearsal TTS on 127.0.0.1:{port}...", flush=True)
            process = subprocess.Popen(
                [str(python), str(script), "-a", "127.0.0.1", "-p", str(port), "-c", str(profile)],
                cwd=runtime, env=environment, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Rehearsal TTS exited before readiness; see tts-service.log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.25):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Rehearsal TTS did not become ready within 120 seconds")
                    time.sleep(.1)
            hub_config = json.loads(before[hub])
            hub_config.setdefault("selections", {})[character_id] = {
                "enabled": True, "provider": "sakura.tts.gpt-sovits"}
            provider_config = json.loads(before[provider])
            provider_config.update(customBaseUrl=f"http://127.0.0.1:{port}", endpointMode="custom")
            hub.write_text(json.dumps(hub_config), encoding="utf-8")
            provider.write_text(json.dumps(provider_config), encoding="utf-8")
            print("Rehearsal TTS is ready.", flush=True)
            yield
        finally:
            try:
                if process is not None:
                    terminate_process_tree(process, timeout=5)
            finally:
                for path, data in before.items():
                    path.write_bytes(data)
