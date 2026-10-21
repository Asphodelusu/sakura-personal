"""Personal intimacy recall crosses the Core/plugin boundary without losing due commitments."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from time import perf_counter

import pytest

from app.agent.runtime import AgentRuntime
from app.plugins.models import ContextProviderContribution
from app.core_host.plugin_runtime_application import _context_request_mapping
from plugins.builtin.sakura_mem0.plugin import _context_request
from plugins.builtin.sakura_mem0.domain_types import ContextRequest
from plugins.builtin.sakura_mem0.memory_recall import MemoryRecallService
from plugins.builtin.sakura_mem0.personal_recall import PersonalRecallPolicy


class Client:
    settings = SimpleNamespace(context_window_tokens=32768, context_window_source="fallback",
                               max_tokens=None, model="synthetic", timeout_seconds=60)

    def resolve_dialogue_params(self):
        return 0.8, {}

    def complete_with_tools(self, *args, **kwargs):
        return SimpleNamespace(content='{"segments":[{"ja":"はい","zh":"好","tone":"中性"}]}',
                               tool_calls=[], runtime_context_role="system", trace_call=None)


@pytest.mark.parametrize("active,screen,expected", [(False, False, "normal"),
                         (True, False, "intimacy"), (True, True, "screen_awareness"),
                         ("exited", False, "normal"), ("no_guide", False, "normal")])
def test_runtime_mode_survives_plugin_projection(active, screen, expected):
    requests = []
    runtime = AgentRuntime(Client(), "synthetic persona", character_id="fixture",
                           reply_tones=["中性"], context_providers=[ContextProviderContribution("fixture", "fixture", lambda req: requests.append(req) or [])])
    runtime.configure_intimacy("" if active == "no_guide" else "synthetic guide")
    if active:
        runtime.note_intimacy_user_turn("贴紧")
    if active == "exited":
        runtime.note_intimacy_user_turn("苹果")
    try:
        runtime._run_tool_loop([{"role": "user", "content": "synthetic input"}],
                              allow_screen_observation=False, turn_started_at=perf_counter(),
                              screen_awareness_mode=screen)
        projected = [_context_request(_context_request_mapping(req)) for req in requests
                     if req.source == "chat"]
        assert projected and all(req.mode == expected for req in projected)
    finally:
        runtime.close()


@pytest.mark.parametrize("mode,candidates,semantic_count", [("normal", 30, 3), ("intimacy", 3, 2)])
def test_personal_light_recall_limits_semantics_but_keeps_due_commitments(tmp_path, mode, candidates, semantic_count):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    due = [{"id": "due", "memory": "synthetic appointment", "metadata": {
        "memory_kind": "commitment", "event_time": (now + timedelta(days=1)).date().isoformat()}}]
    policy = PersonalRecallPolicy(tmp_path, clock=lambda: now, list_memories=lambda: due)
    calls = []
    rows = [{"id": str(i), "content": f"synthetic fact {i}", "source": "explicit",
             "semanticScore": 0.9 - i * 0.1, "updatedAt": now.isoformat()} for i in range(3)]

    class Store:
        recall_policy = policy

        def search_memory(self, arguments, *, wait=False):
            calls.append(arguments)
            return {"status": "ready", "memories": rows}

    try:
        result = MemoryRecallService(Store()).recall(ContextRequest(current_input="fixture", mode=mode))
        assert calls[0]["limit"] == candidates
        ids = [fragment.metadata["memory_id"] for fragment in result.fragments]
        assert ids[0] == "due"
        assert len(ids) == semantic_count + 1
        assert ids[1:] == [str(i) for i in range(semantic_count)]
    finally:
        policy.close()
