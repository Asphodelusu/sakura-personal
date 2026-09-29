"""The Core adapter turns on the personal reply style only for guarded characters."""

from __future__ import annotations

import shutil
from pathlib import Path
from threading import Event

import pytest

from app.agent.tools import ToolRegistry
from app.core_host.assistant_adapter import AssistantAdapter

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"


def _runtime(tmp_path: Path, *, guards: bool):
    root = tmp_path / "root"
    shutil.copytree(FIXTURE_ROOT, root)
    guards_path = root / "characters" / "sakura" / "system_guards.md"
    if guards:
        guards_path.write_text("## \u8fb9\u754c\nSYNTHETIC_BOUNDARY", encoding="utf-8")
    else:
        guards_path.unlink(missing_ok=True)
    readiness = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None).initialize(Event())
    session = readiness.session
    assert session is not None
    return session.runtime


@pytest.mark.parametrize("guards", [True, False])
def test_personal_style_follows_the_guards_file(tmp_path: Path, guards: bool) -> None:
    runtime = _runtime(tmp_path, guards=guards)
    try:
        prompt = runtime._build_tool_system_prompt()
        assert ("suppress_tts" in prompt) is guards
        assert ("35-90" in prompt) is not guards
    finally:
        runtime.close()
