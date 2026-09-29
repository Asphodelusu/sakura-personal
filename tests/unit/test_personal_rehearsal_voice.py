import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest


def fixture_voice(tmp_path):
    runtime, target = tmp_path / "runtime", tmp_path / "trial"
    (runtime / "runtime").mkdir(parents=True)
    (runtime / "runtime/python.exe").touch()
    (runtime / "api_v2.py").write_text(
        "import socket,sys\n"
        "port=int(sys.argv[sys.argv.index('-p')+1])\n"
        "s=socket.socket();s.bind(('127.0.0.1',port));s.listen()\n"
        "while True:\n c,_=s.accept();c.close()\n")
    target.mkdir()
    (target / "tts-infer.yaml").write_text("custom: {}")
    for name in ("sakura.tts", "sakura.tts.gpt-sovits"):
        path = target / "user/data/plugins" / name / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"preserve": true}')
    return runtime, target


@pytest.mark.parametrize('character_id', ['sakura', 'Sakura'])
def test_owned_voice_process_and_configuration_are_released_after_failure(tmp_path, monkeypatch, character_id):
    from tools.personal_rehearsal_voice import managed_voice
    runtime, target = fixture_voice(tmp_path)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    original = subprocess.Popen
    spawned = []
    def start(command, **kwargs):
        process = original([sys.executable, *command[1:]], **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", start)
    with pytest.raises(RuntimeError, match="desktop failed"):
        with managed_voice(runtime, target, port=port, user_root=target / 'user', character_id=character_id):
            assert spawned[0].poll() is None
            selection = json.loads((target / "user/data/plugins/sakura.tts/config.json").read_text())
            assert list(selection['selections']) == [character_id]
            assert selection["selections"][character_id]["enabled"]
            raise RuntimeError("desktop failed")
    assert spawned[0].poll() is not None
    for path in (target / "user/data/plugins").glob("*/config.json"):
        assert path.read_text() == '{"preserve": true}'
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_occupied_port_is_not_adopted_or_stopped(tmp_path):
    from tools.personal_rehearsal_voice import managed_voice
    runtime, target = fixture_voice(tmp_path)
    with socket.socket() as external:
        external.bind(("127.0.0.1", 0))
        external.listen()
        port = external.getsockname()[1]
        with pytest.raises(RuntimeError, match="already in use"):
            with managed_voice(runtime, target, port=port):
                pytest.fail("must not adopt an external service")
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    for path in (target / "user/data/plugins").glob("*/config.json"):
        assert path.read_text() == '{"preserve": true}'
