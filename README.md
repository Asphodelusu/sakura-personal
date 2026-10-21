# Sakura Desktop Pet — Personal Edition

基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人维护分支，由 [Asphodelusu](https://github.com/Asphodelusu) 维护。当前源码版本为 **1.1.1-personal.2**，以仓库 [VERSION](VERSION) 为准。

本分支以个人版的角色设定、关系连续性、记忆和聚焦观察行为为基准，采用上游 Tauri 桌面外壳与 Python Core 架构。上游架构或 UI 更新不代表自动采用上游的人格、记忆或主动感知策略。

[English](docs/README.en.md) · [文档总览](docs/README.md) · [个人迁移对账](docs/records/audits/PERSONAL_MIGRATION_STATUS.md)

## 当前状态

旧 Qt 个人版保存在 `dev5`；新架构在 `dev6` 开发。GitHub 默认分支 `main`、候选分支与本机日用安装可能处于不同提交，源码版本号相同也不代表安装内容相同。

**迁移尚未全部完成。** 普通聊天 fast-tier 自动分流、亲密轻量召回及旧版流式进度/异步字幕方案仍存在差异。具体证据、未部署修复和待核实项见[迁移对账](docs/records/audits/PERSONAL_MIGRATION_STATUS.md)，不能把旧版特性清单视为新版验收结果。

## 个人版的主要方向

- **人格与关系连续性**：保留角色卡、系统约束、关系指引、lore，以及按角色持久化的关系状态；分离时间与互动结算有独立运行链路。
- **个人长期记忆**：SQLite 历史、BGE/BM25 检索、精排与个人整理逻辑。配置、索引和模型规模需要按个人入口适配，不能直接用同 ID 的上游插件覆盖。
- **聚焦 Observer**：以窗口聚焦时长、正文变化和两阶段评估为主要路径；不是仅用固定间隔截图替代旧版观察行为。
- **对话与语音**：GPT-SoVITS、逐段译文检查、内心独白与关系主动判定。存在调用链不等于每个模型与每种场景都已完成真人验收。
- **桌面体验**：采用 Tauri 外壳，保留个人模型设置与重启入口。新架构的回复显示和首段语音等待仍需继续完善。

## 使用与更新

本分支主要面向 Windows 个人使用。上游 [Releases](https://github.com/Rvosy/Sakura/releases) 提供的是**上游版本**，不包含本分支的个人迁移成果，不能作为个人版更新包直接覆盖安装。

已有个人安装应使用其现有启动入口。源码 checkout 不包含个人凭据、角色私有内容、记忆库及完整模型资源；不要把源码压缩包当作可直接运行的个人发行包。开发准备与架构说明见[贡献指南](.github/CONTRIBUTING.md)和[技术文档](docs/devdocs/TECHNICAL_README.md)。继承的安装与功能指南可能描述上游通用行为，个人差异以迁移对账及对应个人契约为准。

更新前区分代码、配置与个人数据。不要为同步架构重置记忆库、覆盖角色卡，或将上游插件默认配置当作个人配置的权威来源。

## 文档与来源

- [个人迁移对账与已知缺口](docs/records/audits/PERSONAL_MIGRATION_STATUS.md)
- [个人运行时连续性契约](docs/specs/runtime-v2/personal-runtime-continuity.md)
- [API 配置说明](docs/userdocs/API_CONFIG.md)
- [运行日志与故障排查](docs/userdocs/RUNTIME_LOG_TROUBLESHOOTING.md)
- [插件 SDK](docs/devdocs/SAKURA_PLUGIN_SDK.md)
- [更新日志](docs/CHANGELOG.md)：其中上游历史条目不等于个人版部署记录。

新架构分支从上游历史建立，再移植个人能力，未将旧个人分支的完整提交历史合入。因此 GitHub 默认分支的贡献统计不能代表全部个人开发工作。旧个人提交保留在 `dev5`，旧 main 另存于 `archive/main-before-runtime-v2-20261008`。本项目保留上游与第三方的作者署名和许可。

## 致谢与开源许可说明

Sakura Desktop Pet 受桌面 Agent、桌宠交互与插件化生态中多个开源项目启发。特别感谢 [Shinsekai](https://github.com/RachelForster/Shinsekai) 项目及其插件生态在桌宠、角色交互、插件扩展等方向上的探索，为 Sakura 的兼容设计和功能设计提供了参考。

本项目采用 MIT License 开源。你可以自由使用、复制、修改、合并、发布、分发、再授权或销售本项目代码，但需要保留本项目的版权声明和 MIT License 文本。

Copyright © 2026 Rvosy

### 第三方代码与兼容说明

本项目中的可选插件 `plugins/optional/playwright_browser` 包含基于以下 MIT 开源项目的代码与改动：

- Project: [`shinsekai-playwright-browser`](https://github.com/RachelForster/shinsekai-playwright-browser)
- License: MIT License
- Copyright: Copyright © 2026 Chihiro

Sakura 在此基础上进行了适配和修改，用于提供 Playwright 浏览器自动化能力。

感谢所有开源项目作者和贡献者。
