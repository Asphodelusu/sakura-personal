# Acceptance — 调研文档（无代码 RED/GREEN）

本批不改生产代码，没有 pytest 门禁。Codex 完成时，下列全部为真。

## 文档齐全

- 存在 `findings.md`、`options.md`、`plan.md`。
- `integration-notes.md` 的 Codex 段已填，status 为 `research-complete` 或说明阻塞。

## findings.md

- 有从「用户回车」到「首句可展示」的层序列，并点到函数/模块名。
- 写清内存窗口、SQLite 历史、digest、recipe 静态块、runtime 片段、tools、裁剪、合成重打各自职责。
- 标出与 `docs/context-token-budget.md` 不一致的数字或流程（例如消息上限、是否按 token 裁剪）。
- Alife 对照只谈概念（唯一会话、分区稳定、明文调用、压缩记忆），无大段外来代码。
- 无聊天正文、无 Key、无角色卡摘录。

## options.md

- 至少 3 个、至多 6 个优化选项（不含「什么都不做」；「什么都不做」可另作基线段）。
- 每个选项有统一的效果 / 效率 / 损耗 / 风险栏，效率有区间或「未知 + 量法」。
- 有「明确不做」清单，且包含：整仓插件化、复制 Alife、把翻译绑回主模型、默认关掉独白/记忆/观察。
- `thinking_level`（或 Gemini 关思考）若值得做，单独成选项或子项，不和「会话稳定」绑死成一件事。

## plan.md

- 有 P0 / P1（可选 P2），P0 不改人格卡、不改角色资源。
- 每个阶段写：要动的模块方向、依赖、如何证明变好（日志字段或夹具）。
- 说明下一份实现合同应如何按文件切开，避免两人改同一生产文件。
- 不要求本批写出完整 pytest 用例，但要指出现有哪些测试最可能被后续实现碰到。

## 工作区卫生

- `git status` 中生产代码无因本任务产生的 diff。
- 未 commit、未 push。
