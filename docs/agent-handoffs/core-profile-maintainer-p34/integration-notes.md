# P3.4 Integration Notes

## Cursor

- status: implemented-unverified
- files:
  - `app/agent/memory_curator.py`
  - `app/agent/core_profile_maintainer_worker.py`（新建：独立 worker、completion adapter、persist/admission 接线）
  - `app/ui/pet_window.py`
  - `tests/unit/test_memory_curator.py`
  - `tests/ui/test_pet_window.py`
  - `tests/integration/test_core_profile_maintainer_flow.py`
  - `tests/unit/test_memory_language_policy.py`（旧断言改为：禁止直接写 `core_profile`，`core_candidate` 白名单应含四正式章节名）
  - 仅本文件 Cursor section
- RED: 未能执行门禁 pytest / `git diff --check`（Cursor Shell 对本会话全部命令返回 `Rejected:`，无 stdout）。按 TDD 先写的对抗测试在修前实现上预期失败：`MemoryCurationResult` 无 `core_candidates`、`layer=core_profile` 仍会 `create_memory`/`update_memory`、整理完成不会 persist/schedule、busy 时可能再开线程。
- GREEN: 未能执行本轮实现后的 pytest 与 `git diff --check`。对应接线：
  1. 普通整理只接受 P3.1 契约候选（`core_candidate` 或带契约字段的 `core_profile` 写意图），写入 `MemoryCurationResult.core_candidates`；不再直接 create/update/delete `core_profile`
  2. 单方面 explicit 降为 observed；吃醋/一次性亲密等 transient 丢弃
  3. 整理成功后才 persist 到 `CoreCandidateQueue`；失败只记元数据，不否定普通整理
  4. 仅当 P3.3 admission 指示有工作时，在独立 `core_profile_maintainer_thread` 上跑 `run_once`；busy 则 defer 并保留候选
  5. maintainer 只拿到 scoped MemoryStore + memory-curation `complete_raw` adapter + queue/state/settings；snapshot 于启动时读取当前 client/settings
  6. 虚构集成场景：双方确认立即维护、短暂吃醋不写 core、observed 等阈值、称呼纠正 replace、重复喜欢 keep
- session/model: Cursor Grok 4.6
- permission failures:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_memory_curator.py tests/unit/test_core_profile_candidates.py tests/unit/test_core_profile_maintainer.py tests/ui/test_pet_window.py tests/integration/test_core_profile_maintainer_flow.py -q`
  - `git diff --check`
- risks:
  - 集成者必须重放门禁；本 worker 没有 pytest / `git diff --check` 输出
  - 未 commit、未 push；未改 P3.1–P3.3 核心模块、`memory_curation_worker.py`、`.gitignore`、真实 `data/memory/` 或 API/TTS/Observer
  - `run_once` 本身不接收 cancel checker，窗口关闭时只能在调用前后打断，模型请求中途可能要等资源管理器 lingering
  - 未把 72h stale 做成独立定时器；依赖后续整理完成时的 admission 再调度


## Coordinator

- scope review: passed; production changes are limited to curator, dedicated maintainer worker, and pet-window lifecycle wiring. P3.1-P3.3 core modules were not changed.
- target verification: 323 passed, 6 skipped. One new fictional episodic fixture was corrected because its content was not grounded by its evidence; production grounding remained unchanged.
- full gate: `tests/unit tests/ui tests/integration` -> 1804 passed, 8 skipped in 31.57s. One old language-policy assertion was updated because the new candidate protocol intentionally lists the four formal section names while still forbidding direct core-profile writes.
- decision: accepted for the P3.4 thematic commit; no push.
