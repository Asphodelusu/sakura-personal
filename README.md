<div align="center">

# Sakura Desktop Pet — Personal Edition

基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) **0.9.9** 的个人维护分支（[`dev2`](https://github.com/Asphodelusu/sakura-personal)）。

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.9.9--personal.2-informational)](VERSION)

</div>

## 说明

本仓库为**个人自用 fork**，在追平上游 0.9.9 基础设施的同时，保留并强化个人向能力（多供应商模型分流、STT、记忆反思、主动屏幕感知等）。**不提供官方 Release，不保证通用环境可用，不承诺跟进上游节奏。**

版本号采用 `0.9.9-personal.N` 格式，与原作者发布版本区分。

---

## 环境要求

| 项目 | 说明 |
|------|------|
| 系统 | 主要在 **Windows 10/11** 下开发与测试 |
| Python | 3.10+（推荐 3.11），路径需为**纯英文** |
| 构建 | Tauri 设置/工坊需预先 `cargo build`（见下方） |
| 网络 | 需能访问所配置的 LLM / TTS 服务端点 |

---

## 快速开始

```powershell
git clone https://github.com/Asphodelusu/sakura-personal.git
cd sakura-personal

python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 首次构建 Tauri 宿主（设置 + 角色工坊）
cd tools\settings-tauri\src-tauri && cargo build --release && cd ..\..\..
cd tools\studio-tauri\src-tauri && cargo build --release && cd ..\..\..

# 配置 data/config/api.yaml、characters.yaml 等（见 docs/API_CONFIG.md）
.\run.bat
```

> 本 fork 使用 `run.bat` + 自建 `.venv`，而非上游 `start.bat` + 内置 `runtime/`。

---

## 与上游 0.9.9 的对齐与差异

### 已对齐（0.9.9 基线）

- **Tauri 设置页** — 唯一设置入口；多 API Profile + `model_slots`（聊天 / 视觉 / 记忆整理）
- **Tauri 角色工坊** — `start_studio.bat` 与设置内「修改角色」共用同一宿主
- **首次引导** — Tauri onboarding
- **`character_studio` 后端** — 草稿与备份位于 `data/character_studio/`
- **手机端插件骨架** — `plugins/sakura_mobile`

### 个人向增强（相对上游）

- **三分流 LLM** — 例如 DeepSeek 聊天 + 智谱视觉/记忆整理（`api_profiles` + `model_slots`）
- **STT 语音输入** — SenseVoice，`Alt+T` 快捷键
- **记忆反思** — 空闲时自动回顾与整理长期记忆
- **主动屏幕感知** — ProactiveObserver + 定时批次截图上下文
- **Agent 工具循环优化** — 工具组按需激活、网页搜索收束、同轮去重
- **立绘映射** — tone → portrait 自动回退
- **本地 LLM 路由预留** — `RoutingLlmClient`（默认走云端，本地为实验项）

### 已移除的旧实现

- Qt 设置对话框（`SettingsDialog`）
- Qt 角色工坊（`tools/studio/` 实现，仅保留转发入口）
- `dual_endpoint` 双端点配置心智（由 `model_slots` 取代）

---

## 常用命令

| 命令 | 作用 |
|------|------|
| `run.bat` | 启动桌宠 |
| `start_studio.bat` | 独立启动 Tauri 角色工坊 |
| `python -m pytest tests/unit` | 单元测试（部分 UI 测试需 PySide6） |

---

## 文档

| 文档 | 内容 |
|------|------|
| [API 配置](docs/API_CONFIG.md) | Profile、model_slots、供应商示例 |
| [安装指南](docs/SETUP.md) | 详细安装与角色包 |
| [技术说明](docs/TECHNICAL_README.md) | 架构与目录结构 |
| [更新日志](CHANGELOG.md) | 版本变更记录 |
| [AGENTS.md](AGENTS.md) | 仓库内 AI Agent 协作约定 |

---

## 致谢

本项目基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura)（MIT License）开发。感谢原作者及 [Shinsekai](https://github.com/RachelForster/Shinsekai) 等开源项目在桌宠与插件生态上的探索。

Copyright © 2026 Rvosy（上游） · Personal fork maintained by Asphodelusu
