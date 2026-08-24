# Cursor Task — Kimi K2.6 参数兼容与缺译文直通

## Objective

在不启用 HTTP 流式传输、不改变角色提示与工具协议的前提下：

1. 对 Moonshot `kimi-k2.6` 请求从第一次开始就省略不可修改的 `temperature`，避免依赖一次 400 后的运行时学习。
2. 首轮主模型返回合法 Sakura segments、日语正文可采用但缺少 `zh` 时，直接采用并交给现有异步字幕翻译，不触发 `_compose_structured_final_reply()`。

生产日志证据：2026-08-15 的 interaction-9～12 均因 `reason="missing_translation"` 发起第二次 Kimi 请求，额外约 3.9～4.5 秒。Moonshot 官方文档说明 `kimi-k2.6` 的 temperature 不可修改、无需显式设置；`thinking.type=disabled`、JSON Mode 和 Tool Calls 均受支持。

## Exclusive files

- Modify: `app/llm/api_client.py`
- Modify: `app/agent/reply_composer.py`
- Test: `tests/unit/test_api_client.py`
- Test: `tests/unit/test_reply_translation_decouple.py`
- Report: `docs/agent-handoffs/kimi-k26-compat/integration-notes.md`（只填写 Cursor section）

## Do not modify

- `data/config/api.yaml`、任何 API Key 或运行时数据
- `app/agent/tool_loop.py`
- `app/agent/prompt_builder.py`
- `app/llm/prompts/**`
- `docs/RP_WEB_MODEL_MANUAL_TEST.md`
- `docs/rp-web-replays/**`
- `tools/export_rp_web_replay.py`
- 其他 Agent handoff 批次

## Shared interface and constraints

- 主 Chat 继续调用非流式 `complete_with_tools()`；不要改成 `stream_raw()`，不要增加 `stream=true`。
- `thinking={"type":"disabled"}` 必须继续发送给 K2.6。
- Kimi K2.7 Code 不允许关闭 thinking，本任务不要改其行为。
- provider/model 特判应尽量收敛在请求参数兼容层；不要通过修改用户配置实现。
- 缺 `zh` 直通只适用于解析后已有可采用日语 segments 的情况；纯文本、坏 JSON、空正文等仍走现有结构化修复。
- 已成功执行会改变答案的搜索/读页/记忆工具后，仍保留最终合成逻辑。

## Required workflow

1. 运行 `git status --short --branch`，保护已有未跟踪文件。
2. 先为两个问题分别写失败测试并记录 RED。
3. 实现最小修复，不顺带重构。
4. 运行：
   - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_api_client.py -q`
   - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_reply_translation_decouple.py -q`
   - `git diff --check`
5. 只填写 `integration-notes.md` 的 Cursor section。

## Git policy

- Local commit: allowed，单个主题 commit。
- Push: forbidden。

## Result fields

- status
- commit or diff state
- modified files
- RED evidence
- GREEN evidence
- confirmation that streaming behavior was not changed
- risks

