# Observer Relationship Arbitration P1

目标：在不增加提示词禁令、不改人格卡的前提下，让关系主动不再被屏幕沉默吞掉，并避免用户离开电脑时反复调用关系模型。

本批仅调整 Observer 调度与配置，不包含聊天历史 `source` 持久化或数据库迁移。

执行者：Cursor / Grok 4.6 High Fast。Codex 负责最终审查、验证、commit 和 push。
