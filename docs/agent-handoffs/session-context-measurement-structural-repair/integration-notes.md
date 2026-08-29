# Integration Notes

## Cursor Desktop / Grok 4.6 High Fast

- status: ready-for-codex-review
- model/session: Cursor Desktop / Grok 4.6 High Fast；工作区 `D:\sakura`，分支 `dev5` ahead 2，HEAD 未由本批 commit
- elapsed: 约 45 分钟
- modified files:
    - `app/llm/payload_inspection.py`（新增）
    - `app/llm/api_client.py`
    - `app/llm/chat_reply.py`
    - `app/agent/tool_loop.py`（仅 `request_purpose`）
    - `app/agent/reply_composer.py`
    - `tests/unit/test_payload_inspection.py`（新增）
    - `tests/unit/test_session_context_measurement_fixtures.py`（新增）
    - `tests/unit/test_request_purpose_propagation.py`（新增）
    - `tests/unit/test_structural_repair_fast_lane.py`（新增）
    - `tests/unit/test_reply_translation_decouple.py`（纯日语路径改为忠实结构修复）
    - 本文件 Cursor Desktop 区块
    - 未改：`characters/`、Observer、`pet_window.py`、`subtitle_controller.py`、TTS、translation sidecar 生产代码、`docs/agent-handoffs/session-context-optimization-research/`
- Phase M RED/GREEN:
    先写失败测试，骨架故意猜错 purpose / 空 usage / 无夹具，再跑：

    ```
    .\.venv\Scripts\python.exe -m pytest tests/unit/test_payload_inspection.py tests/unit/test_session_context_measurement_fixtures.py tests/unit/test_request_purpose_propagation.py -q --tb=line
    ```

    RED（功能缺失，不是 ImportError）：
    1. canonical hash：`NotImplementedError`
    2. `normalize_request_purpose(None)` 猜成 `initial`，不是 `unknown`
    3. `inspect_chat_payload` 未实现
    4. usage 白名单返回 `{}`，无 cached/reasoning
    5. `OpenAICompatibleClient` 无 `payload_inspections`
    6. 合成夹具无 `short_chat`
    7. `complete_with_tools` 未传 `request_purpose`（空串）

    GREEN 后同文件 + fixtures 测试通过；再与 Phase R 一并复跑见下。
- Phase R RED/GREEN:
    ```
    .\.venv\Scripts\python.exe -m pytest tests/unit/test_structural_repair_fast_lane.py -q --tb=short
    ```

    RED：
    1. `classify_chat_reply_failure(合格 JSON)` 恒为 `semantic`，不是 `ok`
    2. `structural_repair_is_faithful` 恒 False
    3. 纯日语第二枪仍是 `semantic_compose`，请求里仍有完整 persona/history
    4. 改写修复没有 `structural_repair → semantic_compose` 回退链

    合格 segments / 仅缺 zh / 工具证据改写答案 三条当时已是正确行为（0 额外结构修复）。

    GREEN 聚焦：

    ```
    .\.venv\Scripts\python.exe -m pytest tests/unit/test_structural_repair_fast_lane.py tests/unit/test_reply_translation_decouple.py tests/unit/test_chat_reply_normalization.py tests/unit/test_request_purpose_propagation.py tests/unit/test_payload_inspection.py tests/unit/test_session_context_measurement_fixtures.py tests/unit/test_api_client.py tests/unit/test_web_search_stall_reply.py -q --tb=line
    ```

    结果：`93 passed`。另跑 `TestLazyToolGroups::test_empty_final_content_triggers_structured_compose` 通过。`git diff --check` 通过。未跑完整 `tests/unit` / `tests/ui`。
- measurement schema example (keys/numbers only):

    ```json
    {
      "interaction_id": "interaction-1",
      "request_index": 1,
      "request_purpose": "initial",
      "model": "synthetic-model",
      "endpoint": "https://api.example.test/v1",
      "partitions": {
        "system": {"bytes": 48, "estimated_tokens": 16, "hash": "64hex"},
        "messages": {"bytes": 120, "estimated_tokens": 40, "hash": "64hex"},
        "runtime": {"bytes": 36, "estimated_tokens": 12, "hash": "64hex"},
        "tools": {"bytes": 80, "estimated_tokens": 25, "hash": "64hex"},
        "image": {"bytes": 0, "estimated_tokens": 0, "hash": "64hex"},
        "whole": {"bytes": 400, "estimated_tokens": 140, "hash": "64hex"}
      },
      "stable_prefix_bytes": 220,
      "stable_prefix_hash": "64hex",
      "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 40,
        "reasoning_tokens": 8
      }
    }
    ```

    DeepSeek-like 夹具有 cached/reasoning；Gemini-like 二者为 null。runtime 无法无歧义拆出时为 `null`。默认 inspection/log 不含 prompt、tool 正文、data URL、Key。
- request/repair state transitions:
    1. 合格 JSON segments（含仅缺 zh）→ 0 额外请求，`purpose=initial`，翻译仍走 sidecar。
    2. 完整纯日语 / 围栏包裹 / 可抽取日文的 syntax/schema → `structural_repair`：只发最小 schema + 原文 +「不得改写日文/顺序」；无 persona、history、tools、runtime、图片。
    3. 修复日文不等价、乱序、非法 tone/portrait、空输出 → 拒绝，回退现有 `semantic_compose`（完整上下文）。
    4. 空输出、语义不足、工具结果会改写答案 → 不进快车道，仍 `_compose_structured_final_reply`。
    5. 现有最后一枪格式修复仍标 `structural_repair`（purpose）；本批未把它改成第二套最小请求，避免增加调用上限。
    6. P0 观测不改 provider payload；`request_purpose` / `request_index` / `interaction_id` 不进入 HTTP 体。
- denied or failed commands:
    - 无被拒绝命令。
    - 按合同未执行完整 `tests/unit`、`tests/ui`，未 commit，未 push，未打真实 API。
- remaining risks:
    - 首泡/首声仍靠现有 interaction stage 离线关联，本批未改 UI。
    - `api_client` 既有「HTTP 请求体已构建」仍可能带 payload 正文；本批只新增无正文的 `payload_inspection`，未扩大也未删除旧诊断。
    - 纯日语快修成功依赖「规范化后全文等价」；合理分段但拼接不等价会被拒并回退合成。
    - `_parse_final_reply_with_retry` 若单独吃到「可当 segment 的纯日语」仍会直接采用原文，不会再包 JSON；主路径 `handle_user_message` 先走 `_resolve_final_reply_content`。
    - `local_client` 用 `**kwargs` 转发 purpose，未改该文件。
    - 无真实 provider usage/缓存数据；合成夹具只证明构造与调用次数。
    - 未跑 `test_prompt_templates` 全文件与完整 UI。

## Codex

- review: 初版方向成立，但未直接放行。独立审查发现并修复五项合同缺口：inspection 通过隐藏 `_whole_bytes` 与无界 list 长期保留完整 payload；`complete_raw()` 未观测；DeepSeek `prompt_cache_hit_tokens` 未进入白名单；完整上下文最后修复枪被误标为 `structural_repair`；忠实校验允许结构修复器擅自填写 `zh`。进一步扩大 AgentRuntime 回归后发现通用截断 JSON 可能只含半段语义，因此 `syntax` 改为 fail closed，直接走 semantic compose；快车道只保留有可采用正文的 envelope/schema。
- review-fix RED:
  - 最窄 6 条审查测试：**5 failed, 1 passed**。失败分别为隐藏 `_whole_bytes` 存在、DS cache hit 为 null、raw inspection 为空、非空 zh 被接受、full-context repair 被标 structural；有界测试因 raw 尚未接线暂时假绿。
  - 首轮扩大回归：**96 passed, 2 failed**。两处旧夹具让 structural repair 返回非空 zh，正确拒绝后 mock 枪数不足。
  - AgentRuntime 扩大回归：**66 passed, 2 failed**。其中 reminder 截断 JSON 只含“时间到了”而缺“喝水”，证明通用 syntax 不具备语义完整性；纯日语旧夹具仍期待被二次改写与翻译，和新快车道合同冲突。
- verification:
  - 最终聚焦：`test_payload_inspection.py test_session_context_measurement_fixtures.py test_request_purpose_propagation.py test_structural_repair_fast_lane.py test_reply_translation_decouple.py test_chat_reply_normalization.py test_api_client.py test_web_search_stall_reply.py test_agent_runtime.py test_json_completion.py test_memory_query_rewrite.py test_inner_thought.py test_inner_thought_parallel.py -q --tb=short` → **167 passed in 9.46s**。
  - `complete_raw` 成功 usage 与 purpose/index 可关联；失败时 inspection 先入有界 deque，usage 保持 null。runtime-role fallback 会记录第二次逻辑请求。
  - inspection deque 与 interaction index map 均上限 128；inspection 对象不含正文，client 仅私有保留上一个 payload 用于相邻前缀比较。
- integration decision: 接受 P0 + P1-A，待最终 `git diff --check` 后按单一主题 commit。P0 当前测的是非流式逻辑请求；`stream_raw` 未覆盖，compatibility helper 内部参数剥离/网络重试也未逐 HTTP attempt 单列。stable-prefix 是 canonical payload bytes 代理，不等同 provider 内部 token-cache 命中；真实缓存效果仍需后续授权实测。request index 为 client-local，同一 interaction 跨模型槽需结合 endpoint/model 与日志时间排序。
