"""Opt-in real Core, personal worker and BGE-M3; synthetic data/local HTTP only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from tests.personal_memory_fixture import create_synthetic_memory, install_personal_plugin
from tests.integration import test_core_host_real_chat_integration as chat
from tests.integration.test_wp_4_01_memory_capability import _negotiate_mem0_plugin


@pytest.mark.skipif(
    not (os.environ.get("SAKURA_TEST_PERSONAL_SNAPSHOT") and
         os.environ.get("SAKURA_TEST_PERSONAL_DEPENDENCIES")),
    reason="explicit local model and private dependencies required",
)
def test_personal_recall_reaches_core_provider_and_shuts_down(tmp_path, monkeypatch):
    from app.legacy_import.personal_copy import prepare_personal_copy
    from app.storage.paths import StoragePaths

    repository = Path(__file__).resolve().parents[2]
    snapshot = Path(os.environ["SAKURA_TEST_PERSONAL_SNAPSHOT"])
    source = tmp_path / "source"
    create_synthetic_memory(source, snapshot)
    distribution = tmp_path / "distribution"
    install_personal_plugin(repository, distribution, Path(os.environ["SAKURA_TEST_PERSONAL_DEPENDENCIES"]))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("MEM0_TELEMETRY", "False")
    provider, thread = chat._start_provider("complete")
    process = None
    try:
        configured = chat._configure_app_root(tmp_path, provider.server_address[1])
        work = tmp_path / "work"
        prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
        shutil.copytree(configured, work, dirs_exist_ok=True)
        config = StoragePaths(work).plugin_data_for("sakura.memory.mem0") / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"personalSnapshot": str(snapshot)}))
        bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('app.core_host',run_name='__main__')"
        process = subprocess.Popen([
            sys.executable, "-I", "-c", bootstrap, str(repository),
            "--distribution-root", str(distribution), "--user-root", str(work),
            "--generation-id", chat.GENERATION_ID,
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        process.stdin.write(bytes.fromhex(chat.GENERATION_CREDENTIAL))
        process.stdin.flush()
        _negotiate_mem0_plugin(process)
        deadline = time.monotonic() + 90
        attempt = 0
        while True:
            operation = f"personal-recall-{attempt}"
            chat._send(process, chat._request(operation, "chat.send", {
                "message": "Where is the blue notebook?", "operationId": operation,
            }))
            frames = [chat._read(process) for _ in range(3)]
            assert {frame.get("name") for frame in frames} == {
                "chat.started", "chat.send", "chat.completed",
            }, frames
            sent = json.dumps(chat._ProviderHandler.requests[-1], ensure_ascii=False)
            assert "FOREIGN_SCOPE_312" not in sent
            if "CORE_RECALL_312" in sent:
                break
            assert time.monotonic() < deadline, "personal memory never reached model context"
            attempt += 1
            time.sleep(.25)
        assert chat._exchange(process, chat._request("shutdown", "system.shutdown", {}))["ok"]
        assert process.wait(timeout=5) == 0
    finally:
        if process is not None:
            chat._stop(process)
        chat._stop_provider(provider, thread)
