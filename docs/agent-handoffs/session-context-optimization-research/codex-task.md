# Codex Task — 会话上下文优化调研与规划

## Objective

按 `requirements.md` 完成只读调研，写出 `findings.md`、`options.md`、`plan.md`，并填写本批次 `integration-notes.md` 的 Codex 段。不改生产行为。目的：给后续实现提供可比较的会话优化方案，以及效果 / 效率 / 损耗变化。

## Required reading

- `docs/agent-handoffs/CONTEXT.md`
- `docs/agent-handoffs/session-context-optimization-research/requirements.md`
- `docs/agent-handoffs/session-context-optimization-research/acceptance-tests.md`
- `docs/context-token-budget.md`（核对是否过期）
- 需求 3.3 列出的只读实现入口；可按调用链增补，并在 findings 列出实际读过的路径。

参考（思路 only）：<https://github.com/BDFFZI/Alife> 公开 README。不要把 Alife 源码拷进本仓库。

## Exclusive files

只允许创建或修改：

- `docs/agent-handoffs/session-context-optimization-research/findings.md`
- `docs/agent-handoffs/session-context-optimization-research/options.md`
- `docs/agent-handoffs/session-context-optimization-research/plan.md`
- `docs/agent-handoffs/session-context-optimization-research/integration-notes.md` 的 Codex 段

## Do not modify

- `app/`、`tests/`、`plugins/`、`characters/`、`data/`
- 其他 `docs/agent-handoffs/*` 批次
- `docs/context-token-budget.md`（过期处写在 findings，不改原稿）
- `.gitignore`、配置、角色资源

## Do not read or quote

- `data/config/api.yaml` 及任何 API Key
- `data/memory/` 档案正文、`data/chat_history/` 消息正文
- 角色卡 / intimacy / 关系 guide 正文
- 日志里的用户/助手对白；只可用结构字段（`elapsed_ms`、`bytes`、`chars`、`reason`、model 名）

## Shared interface

本批无生产接口变更。规划里若提出新模块名，标成「建议」，不要假装已经存在。

## Required workflow

1. `git status --short --branch`。工作区可能有他人改动，一律不碰。
2. 只读摸清会话管道，对照需求 Q1–Q6。
3. 写 `findings.md` → `options.md` → `plan.md`。
4. 对照 `acceptance-tests.md` 自检。
5. 只填 integration notes 的 Codex 段。
6. 不运行会改数据的脚本；不要为了「测一下」对真实 API 发请求。
7. 单元测试不是本批门禁。若打开测试文件，只为理解行为，不改测试。

## Git policy

- Local commit: forbidden
- Push: forbidden

## Result fields

- status: `research-complete` / `research-blocked`
- files written
- key recommendation（三句话内）
- options considered
- measurement gaps
- risks
- actual files read（路径列表）
