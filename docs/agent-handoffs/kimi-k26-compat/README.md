# Kimi K2.6 Compatibility Batch

目标：保持 Sakura 主 Chat 的非流式行为，修正 Kimi K2.6 的供应商参数兼容，并让缺少 `zh` 但已有合格日语 segments 的首轮回复进入异步翻译，而不是二次调用主模型。

分工：Cursor 负责实现与目标测试；Codex 负责核对官方约束、审查 diff、复跑测试与新日志验证。共享工作区，不使用 worktree；Cursor 不得触碰现有未跟踪 RP replay 文档和生成器。

