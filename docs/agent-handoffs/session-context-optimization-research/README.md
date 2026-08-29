# Session Context Optimization Research

目标：对照 Alife 的「永久唯一会话 / 分区稳定上下文 / 省 token」思路，把 Sakura 当前会话与上下文管道摸清，并给出可落地的优化方案与效果/效率/损耗对照。本批只做调研与规划，不改生产代码。

执行者：Codex（规划 + 调研）。Cursor 只建合同，不实现。

参考灵感来自 [BDFFZI/Alife](https://github.com/BDFFZI/Alife) 的会话设计，**只借鉴思路，禁止复制其代码、提示词或协议**（AGPL-3.0）。
