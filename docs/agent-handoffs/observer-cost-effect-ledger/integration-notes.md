# Integration notes

## Cursor

- status: 分类修正已落盘（未 commit、未 push）。本会话 Shell 仍被拒绝，未能实跑 pytest / `git diff --check`。
- commit or diff state: 工作区未提交改动；合同禁止 commit/push，已遵守。
- modified files:
  - `app/perception/observer.py`
  - `tests/unit/test_observer_ledger.py`
  - 本文件 Cursor 段
- RED evidence:
  - 新增失败用例（实现前会得到错误 outcome）：`capture_error` 当时记 `decision_error`；VLM HTTP/解析当时记 `decision_error`；本进程/隐私当时 finally 成 `silent`；空观测包当时 finally 成 `silent`。
  - 同步补了决策失败才记 `decision_error`（VLM 成功 + `rejected_invalid_output`），以及 `should_speak=true` 且 comment 为空保持 `silent`。
  - focused pytest 未能执行（Shell 拒绝）。合同命令：`.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_ledger.py -q`
- GREEN / 分类:
  - `capture_error`：截图失败，未调用 VLM/决策。
  - `vlm_error`：VLM HTTP 异常或返回无法解析 JSON（可带 `vlm_usage` / `vlm_elapsed_ms`，无决策阶段）。
  - `decision_error`：仅发言决策失败/未配置/非法输出；屏幕路径可同时带 VLM 与决策 usage。
  - `preflight_skip`：模型前中止（本进程焦点、前台 PID 为自己、隐私拦截）。
  - `empty_perception`：VLM JSON 可解析但观测包为空（VLM 已花费、未进决策）。这是最小准确增量，避免并进 `vlm_error` 或 `silent`。
  - `should_speak=true` 且 comment 为空：保持 `silent`（决策已完成且 `decision_format`/`decision_usage` 可归因；不另增 outcome）。
  - 仍保留：`speak` / `silent` / `dedup_skip` / `stale_cancel`；exactly-once 与 denylist 不变。
- focused tests: 未跑（Shell 拒绝）。合同命令：`.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_ledger.py tests/unit/test_observer_plain_dialogue_fallback.py tests/unit/test_relationship_timer.py tests/unit/test_proactive_focus.py -q`
- full gate: 未跑。合同命令：`.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q`
- `git diff --check`: 未跑（Shell 拒绝）。
- remaining risks:
  - 协调者必须在真实环境复跑 focused + `tests/unit tests/ui` 和 `git diff --check` 后再收口。
  - 同窗跳过发言决策仍走 `silent`（VLM 已花费、无决策 usage）；未再拆 outcome。
  - 未改 prompt、persona、请求 payload、重试、翻译、冷却、设置或 UI。

## Codex

- reviewed the actual diff for exactly-once settlement, privacy, usage copying, and runtime overhead.
- returned one attribution issue to the same Cursor session: capture and VLM failures were initially collapsed into `decision_error`; final taxonomy separates `preflight_skip`, `capture_error`, `vlm_error`, `empty_perception`, and `decision_error`.
- focused verification: `74 passed in 5.98s` for ledger, P2 parser, relationship timer, and proactive focus tests.
- full verification: `1750 passed, 1 skipped in 36.29s` for `tests/unit tests/ui`.
- `git diff --check`: passed.
- accepted overhead: one small in-memory attempt object and one existing `debug_log` call per actual evaluation; no extra model call, retry, database, background task, or dedicated file writer.
- final reviewer correction: semantic same-window dedup after a completed VLM call now settles as `dedup_skip` rather than default `silent`; regression coverage verifies VLM usage remains attributed while the decision model is not called.
