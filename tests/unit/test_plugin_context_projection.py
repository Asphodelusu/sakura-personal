"""Large imported histories must remain usable by out-of-process providers."""
from app.core_host.plugin_runtime_application import _context_request_mapping
from app.llm.prompts.types import ContextMessage, ContextRequest
from app.plugins.sakura_plugin_sdk import json_value


def test_large_history_fits_plugin_frame_without_mutating_history_or_current_input():
    messages = tuple(ContextMessage('user' if i % 2 else 'assistant', f'{i}:' + '旧记忆😀' * 600)
                     for i in range(15000))
    request = ContextRequest(current_input='完整当前输入' * 1000, character_id='Sakura',
                             current_turn_id='current', source_entry_ids=('source-row',),
                             recent_messages=messages)
    payload = _context_request_mapping(request)
    json_value({'handle': 'callback', 'method': 'context.contributor', 'args': [payload]})
    assert payload['recent_messages'] == [
        {'role': item.role, 'content': item.content[:2000]} for item in messages[-32:]]
    assert payload['current_input'] == request.current_input
    assert payload['character_id'] == 'Sakura'
    assert payload['source_entry_ids'] == ('source-row',)
    assert request.recent_messages is messages
    assert len(messages[0].content) > 2000


def test_small_context_projection_is_lossless():
    request = ContextRequest(current_input='hello', recent_messages=(ContextMessage('user', 'hello'),))
    assert _context_request_mapping(request)['recent_messages'] == [{'role': 'user', 'content': 'hello'}]
