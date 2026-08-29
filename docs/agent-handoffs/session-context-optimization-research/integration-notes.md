# Integration Notes

## Cursor

- status: contract-ready（仅新建本批次文档，未改生产代码，未 commit）
- modified files:
  - `docs/agent-handoffs/session-context-optimization-research/README.md`
  - `docs/agent-handoffs/session-context-optimization-research/requirements.md`
  - `docs/agent-handoffs/session-context-optimization-research/codex-task.md`
  - `docs/agent-handoffs/session-context-optimization-research/acceptance-tests.md`
  - 本文件 Cursor 段
- notes: 需求来自用户要求对照 Alife 会话处理做规划调研。分支 `dev5`。

## Codex

- status: research-complete
- files written:
  - `docs/agent-handoffs/session-context-optimization-research/findings.md`
  - `docs/agent-handoffs/session-context-optimization-research/options.md`
  - `docs/agent-handoffs/session-context-optimization-research/plan.md`
  - 本文件 Codex 段
- key recommendation: 先补 payload 分区、request purpose 与稳定前缀观测；随后优先把纯结构失败改为最小上下文修复，保留语义失败的完整合成。静态前缀/工具缓存与 Gemini thinking 分开实施，工具动态暴露和摘要增强只在测量证明值得时进入 P2。
- options considered:
  - 完整 payload 观测与稳定前缀指纹（P0 推荐）
  - 静态 recipe/tools/message 前缀与会话级构建缓存（P1 推荐）
  - 结构修复快车道、语义合成保留完整上下文（P1 推荐）
  - 按意图暴露 native tool 工具包（P2 候选）
  - Gemini provider-specific thinking 控制（独立候选）
  - 实时窗口 + digest + 长期记忆的连续生命线增强（P2 候选，不做全历史回放）
- measurement gaps:
  - PromptInspector 尚不覆盖 messages、图片估算、tools schema 与完整 payload fingerprint。
  - 缺 request purpose/index、stable-prefix bytes、统一首泡/首声时间及厂商 cached/thinking usage。
  - 当前 4KB/31KB、7.7s/13–15s/53s 仅为脱敏个例或近期观察，不是 P50/P90；需 synthetic fixtures 和后续用户授权实测。
- risks:
  - 错误缓存动态 persona/intimacy/权限上下文会跨轮或跨角色污染。
  - 小型修复若误分类语义失败会答非所问，必须做 ja 等价校验并 fail closed。
  - 动态工具包可能漏能力；XML 双轨、永久全历史回放与翻译回绑均不推荐。
  - Gemini thinking 参数依模型/endpoint 变化，实施前必须复核官方字段并用 fake payload 先测。
- actual files read:
  - `docs/agent-handoffs/CONTEXT.md`
  - 本批次 `README.md`、`requirements.md`、`acceptance-tests.md`、`codex-task.md`、`integration-notes.md`
  - `docs/context-token-budget.md`
  - `app/ui/pet_window.py`、`app/core/chat_worker.py`
  - `app/agent/context_orchestrator.py`、`context_builder.py`、`session_state_context.py`、`local_context.py`、`sensory_context.py`、`inner_thought.py`、`memory_recall.py`、`lore.py`
  - `app/agent/prompt_builder.py`、`tool_loop.py`、`tool_routing.py`、`builtin_tools.py`、`tools/registry.py`、`reply_composer.py`
  - `app/llm/context_trimming.py`、`api_client.py`、`chat_reply.py`、`prompts/runtime.py`、`prompts/types.py`
  - `app/storage/chat_history.py`、`history_digest.py`
  - `docs/agent-handoffs/translation-decoupling/integration-notes.md`、`provider-adapter-decision.md`
  - `docs/agent-handoffs/spark-history-channel/README.md`、`integration-notes.md`
  - Alife GitHub 公开 `README.md`（master；只读公开概念，未 clone、未复制源码/prompt/XML）
