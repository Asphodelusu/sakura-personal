# Session Context Measurement + Structural Repair

实现已批准的两阶段小批次：

1. P0：建立不记录正文的请求分区、purpose、稳定前缀与 usage 测量体系。
2. P1-A：对纯结构失败使用最小上下文修复；语义不足继续走完整合成。

执行者：Cursor Desktop / Grok 4.6 High Fast。必须先完成并验证 P0，再实现 P1-A。Codex 负责最终 diff、指标口径和 fail-closed 语义审查。

设计依据：`docs/agent-handoffs/session-context-optimization-research/`。不得改角色卡、翻译 sidecar、Observer/relationship、字幕/TTS 或真实运行时数据。
