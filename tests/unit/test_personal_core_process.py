"""Opt-in bundled Python Core protocol smoke test with a new empty user root."""
import io
import os
from pathlib import Path
import subprocess

import pytest

from app.core_host.protocol import encode_frame, read_frame


@pytest.mark.skipif(not os.environ.get("SAKURA_TEST_CORE_PYTHON"), reason="explicit bundled Python required")
def test_bundled_core_handshake_health_and_shutdown(tmp_path):
    root = Path(__file__).resolve().parents[2]
    credential = "11" * 16
    generation = "00000000-0000-4000-8000-000000001c01"
    frames = []
    for name in ("system.hello", "system.health", "system.shutdown"):
        payload = {"protocol": {"major": 2, "minMinor": 0, "maxMinor": 1},
                   "requiredCapabilities": ["system.hello", "system.health", "system.shutdown"],
                   "optionalCapabilities": []} if name == "system.hello" else {}
        frames.append(encode_frame({"protocolMajor": 2, "protocolMinor": 1, "kind": "request",
            "generationId": generation, "generationCredential": credential, "id": name,
            "name": name, "payload": payload, "deadlineMs": 3000, "priority": "control"}))
    bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('app.core_host',run_name='__main__')"
    result = subprocess.run([os.environ["SAKURA_TEST_CORE_PYTHON"], "-I", "-c", bootstrap, str(root),
        "--distribution-root", str(root), "--user-root", str(tmp_path / "user"),
        "--generation-id", generation], input=bytes.fromhex(credential) + b"".join(frames),
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-3000:]
    output, responses = io.BytesIO(result.stdout), {}
    while (frame := read_frame(output)) is not None:
        if frame["kind"] == "response":
            responses[frame["id"]] = frame
    assert set(responses) == {"system.hello", "system.health", "system.shutdown"}
    assert all(not response.get("error") for response in responses.values())
