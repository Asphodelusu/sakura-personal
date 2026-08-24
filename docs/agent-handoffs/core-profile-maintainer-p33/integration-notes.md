# P3.3 Integration Notes

## Claude architecture review

- status: Claude CLI quota exhausted before execution (0 cost); equivalent bounded read-only contract was produced by the coordinator's architecture agent.
- session/cost: Claude session `16154f5b-092e-4d4a-832c-e9cb549046a2`, 0 USD, HTTP 429 quota reset notice.
- contract: synchronous maintainer core, metadata-only state, queue transactions, deterministic scheduling/parser/validation, partial-commit generation binding, and no P3.4 wiring.
- risks: exact token usage remains unavailable through the current completion adapter; semantic paraphrase cannot be reliably validated by deterministic code.

## Cursor implementation

- status: implemented-unverified
- files:
  - `app/agent/core_profile_maintainer.py`
  - `tests/unit/test_core_profile_maintainer.py`
  - `app/agent/core_profile_candidates.py`（`candidate_generation_fingerprint`、`mark_processed` 写入处理时 `last_seen_at`）
  - `tests/unit/test_core_profile_candidates.py`
  - `data/config/system_config.yaml`（`lease_ttl_minutes`）
  - `tests/unit/test_settings_service.py`
  - 仅本文件 Cursor section
- RED: 未能执行门禁 pytest / `git diff --check`（Shell 与 `request_smart_mode_approval` 仍为 `Rejected:`，无 stdout）。本轮对抗测试在修前实现上预期失败：ID-only partial repair、租约永不超时、清空全部 pending trigger、keep 带 content / 分节 migrate、全局 correction bypass、skip 路径读 core、`mark_processed` 不刷新 `last_seen_at`。
- GREEN: 未能执行本轮修正后的 pytest 与 `git diff --check`。对应 9 条 blocker 修复：
  1. Partial recovery 绑定 `candidate_id + evidence fingerprint + core revision`（状态文件 `pending_repairs`）；只在安全准入后、且精确世代匹配时 repair；新证据复用稳定 ID 不得标 applied
  2. 全局租约 `lease_ttl_minutes`（默认 30，钳制 1–1440）；过期/崩溃启动可抢占，禁止永久 busy
  3. 只消费本轮 ≤5 选中候选的 pending trigger / explicit batch；未选中的并发 trigger 与 batch 保留
  4. 拒绝重复非 keep 章节、以及同一 candidate 同时出现在 keep 与 applied
  5. `migrate_legacy` 单一 `sections` map；ordinary 延后则 `ordinary_deferred=true`、ops 不计 refine、candidate 保持 pending、不写入 core `candidate_ids`
  6. 锚点覆盖引号/数字、未加引号称呼名、关系身份词、约定 CJK 片段；correction bypass 按 operation+section 绑定
  7. 严格解析：keep 禁止 content、refs 非空且唯一、refine/replace 非空、remove 空、坏类型一律 `MaintainerParseError`
  8. disabled / no eligible / paused / busy 不读 `core_profile()`；repair 仅在准入后
  9. `mark_processed` 把 `last_seen_at` 更新为处理时刻
- session/model: Cursor Grok 4.6
- permission failures:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_maintainer.py tests/unit/test_core_profile_candidates.py tests/unit/test_settings_service.py tests/unit/test_storage_paths.py tests/unit/test_core_profile_schema_v2.py -q`
  - `git diff --check`
- risks:
  - 集成者必须重放门禁；本 worker 没有 pytest / `git diff --check` 输出
  - 未 commit、未 push；未改 `memory.py`、curator/worker/UI、真实 `data/memory/` 或 `.gitignore`
  - 状态丢失时 fail-closed：不凭 core metadata 的稳定 ID 单独 repair
  - 确定性锚点不做语义同义检测；约定保护按连续 CJK 片段，不是自然语言理解

## Coordinator

- review: accepted after Cursor repaired adversarial findings covering generation-bound partial recovery, stale lease takeover, trigger preservation, strict operation conflicts/schema, section-bound anchors, no-core-read admission paths, and processed timestamps. One stale selection-order test was corrected to match the approved pending-trigger priority; scheduler behavior was not weakened.
- verification: `.venv` target gate 221 passed in 6.03s; `git diff --check` passed apart from the pre-existing `.gitignore` EOL warning. No additional GPT-5.6 review pass was run.
