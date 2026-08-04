# 遗留红测试清理（dev4 基线）

- [x] 诊断 test_api_client.py 9 个失败（urllib mock 过时 → httpx 重写；2 个产品 bug → skip）
- [x] 诊断 test_config.py 2 个失败（默认值断言过时，已修）
- [x] 诊断 test_context_trimming.py 1 个失败（消息数上限 → token 预算语义，已修）
- [x] 诊断 test_plugin_*.py 21 个失败（api_version 1→2 + 去 build 参数，已修）
- [x] 诊断 test_prompt_templates.py 2 个失败（__new__ 伪造 runtime 缺属性，已修）
- [x] 诊断 test_tts_bundle.py 5 个失败（install 状态/断点续传语义/GPU 缓存/显式 GPU，已修）
- [x] 修复测试 / skip 产品问题，逐个文件回归
- [x] 全量 tests/unit 跑绿（1113 passed, 12 skipped，连续两次稳定）
- [x] commit + push dev4

## 已修复（原 skip 产品 bug）
- [x] `test_list_models_wraps_http_error` / `test_google_ai_studio_auth_error_gets_actionable_message`：
  `_send_http_with_retries` 对 `status_code >= 400` 抛 `_format_api_http_error`（对齐 stream 路径），skip 已移除。

## 本轮 skip 的产品 bug（tests/integration/test_agent_core.py）
- [ ] 自动浏览器快照结果未回传给模型：
  `runtime._execute_auto_browser_snapshot` 生成带 `tool_call_id="auto_browser_snapshot_*"` 的
  tool 消息，但 assistant 消息的 `tool_calls` 里没有对应 id；下一轮 planning 前
  `trim_messages_for_model` → `sanitize_tool_conversation_messages` 会把这条快照 tool 消息丢弃，
  导致模型看不到页面文本。受影响的测试：
  `test_browser_interaction_request_auto_snapshots_without_fast_forward`、
  `test_browser_lookup_does_not_fast_forward_when_auto_snapshot_has_no_content`。
  修复方向：让 `_execute_auto_browser_snapshot` 的快照消息沿用真实 assistant tool_call id
  （或在 assistant 消息中补一条匹配的 tool_call），确保快照内容能进入下一轮模型上下文。
