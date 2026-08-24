# Integration Notes — Kimi K2.6 Compatibility

## Cursor

- status: done
- commit or diff state: 本地 commit（未 push）。`kimi-k2.6` 首次请求省略 `temperature`；解析后已有可采用日语 JSON segments 时不再调用 `_compose_structured_final_reply()`。
- modified files:
  - `app/llm/api_client.py`
  - `app/agent/reply_composer.py`
  - `tests/unit/test_api_client.py`
  - `tests/unit/test_reply_translation_decouple.py`
  - `docs/agent-handoffs/kimi-k26-compat/integration-notes.md`（仅本 section）
- RED evidence:
  - `test_kimi_k26_omits_temperature_on_first_payload`：payload 仍含 `temperature=0.6`
  - `test_complete_with_tools_kimi_k26_omits_temperature_keeps_thinking`：首次 `complete_with_tools` 仍发送 `temperature=0.8`
  - `test_adoptable_json_with_prose_prefix_does_not_compose`：`_structured_compose_reason` 返回 `missing_translation`
  - 标准 ja-only segments 的 compose spy（`test_ja_only_segments_do_not_call_compose_structured_final_reply`）在实现前已绿，属 Phase 1 已覆盖路径
- GREEN evidence:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_api_client.py -q` → 48 passed
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_reply_translation_decouple.py -q` → 11 passed
  - `git diff --check` → 通过
- streaming unchanged: 主 Chat 仍走非流式 `complete_with_tools()`；未改用 `stream_raw()`，未增加 `stream=true`。`stream_raw()` 仍是唯一写入 `stream: True` 的路径。K2.6 继续发送 `thinking: {type: disabled}`；`kimi-k2.7-code` 仍发送配置温度。
- risks:
  - 模型名特判只覆盖 `kimi-k2.6` / `kimi-k2.6-*` 与 `*/kimi-k2.6`；若网关使用完全不同的 id，仍会先发 temperature 再走 400 回退
  - 纯日语无 JSON 正文仍会二次合成（合同要求保留）
  - 会改变答案的成功搜索/读页/记忆工具后仍走最终合成
  - 未跑完整 `tests/unit tests/ui`；未做实机 5～10 轮验证

## Codex integration review

- status: code and automated-test review complete; runtime follow-up pending
- diff review: 文件范围符合合同；主 Chat 未改流式路径。K2.6 在 payload 构建层省略 temperature，thinking 参数保留；可解析的日语 JSON segments 缺 zh 时绕过二次 compose，纯文本/坏 JSON仍保留修复路径。
- targeted verification: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_api_client.py tests/unit/test_reply_translation_decouple.py -q` → 59 passed
- full verification: `.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q` → 1430 passed, 6 skipped
- runtime verification: 待重启 Sakura 后实聊 5–10 轮，确认普通无工具回复不再因 `missing_translation` 二次请求 Kimi，并记录首句延迟。
- final decision: 当前 K2.6 主槽位修复可进入实机验证；未发现阻塞 push 的自动化回归。后续单独处理 K2.7 Code 同样不应发送 temperature 的官方兼容差异，不扩大本批次范围。
