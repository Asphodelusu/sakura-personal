<div align="center">

# Sakura Desktop Pet — Personal Edition

基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) **0.9.9** 的个人维护分支（[`main`](https://github.com/Asphodelusu/sakura-personal)）。

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
| 构建 | 首次需本地编出 Tauri **设置页**（见下方）；角色工坊可选 |
| 网络 | 需能访问所配置的 LLM / TTS 服务端点；角色包从上游 Release 下载 |

---

## 快速开始（Windows CMD）

下列命令按 **CMD** 书写。已有 `.venv`、已编过设置页、已有角色包时，**跳过对应步骤**，直接 `run.bat` 即可，不必重装。

> 本 fork 使用 `run.bat` + 自建 `.venv`，**不要**跟 `docs/SETUP.md` 里上游的 `install.bat` / `start.bat` / `runtime/`（那是 Release 完整包流程）。

### 1. 拉取

```bat
git clone https://github.com/Asphodelusu/sakura-personal.git
cd sakura-personal
```

### 2. Python 依赖（仅当还没有 `.venv`）

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

已有可用的 `.venv` 则跳过。`requirements.txt` 含 `sentence-transformers` 等，**首次安装可能较慢**。

### 3. Tauri 设置页（仅当还没有 `sakura-settings.exe`）

首次启动会打开设置程序；缺失时会直接报错。检测：

```bat
dir tools\settings-tauri\src-tauri\target\release\sakura-settings.exe
```

若提示找不到文件，先装好 [Rust](https://rustup.rs)（或 `winget install Rustlang.Rustup`），装完**重新打开 CMD**，再在项目根目录执行：

```bat
cd tools\settings-tauri\src-tauri
cargo build --release
cd ..\..\..
```

Windows 上若 `cargo` 报找不到 `link.exe`，需另装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)（勾选“使用 C++ 的桌面开发”）。多数 Win10/11 已带 WebView2；若设置页窗口起不来再装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

角色工坊**不是**首启必需。只有要用 `start_studio.bat` / 设置里改角色时才编：

```bat
cd tools\studio-tauri\src-tauri
cargo build --release
cd ..\..\..
```

### 4. 默认角色包（仅当还没有 `characters\*\character.json`）

本仓库不附带角色资源（`characters/` 已 gitignore）。可从上游 **v0.9.9** Release 下载默认包（约数百 MB）：

```bat
curl -L -o Sakura.char https://github.com/Rvosy/Sakura/releases/download/v0.9.9/Sakura.char
```

浏览器下载也行：[Rvosy/Sakura Releases](https://github.com/Rvosy/Sakura/releases) → 附件 **`Sakura.char`**。

启动后会进入首次设置页，在界面里 **导入 .char**（选刚下的文件）。已有角色目录则可跳过本步。

### 5. 启动

```bat
run.bat
```

首次进入 Tauri 设置后配置 API Profile、模型槽位，并完成角色导入。详细说明见 [docs/API_CONFIG.md](docs/API_CONFIG.md)。

---

## 与上游 0.9.9 的对齐与差异

### 已对齐（0.9.9 基线）

- **Tauri 设置页** — 唯一设置入口；多 API Profile + `model_slots`（聊天 / 视觉 / 记忆整理）
- **Tauri 角色工坊** — `start_studio.bat` 与设置内「修改角色」共用同一宿主
- **首次引导** — Tauri onboarding
- **`character_studio` 后端** — 草稿与备份位于 `data/character_studio/`
- **手机端插件骨架** — `plugins/sakura_mobile`

### 个人向增强（相对上游）

- **多模型槽位分流** — chat / chat_fast / vision / memory_curation 独立配置（`api_profiles` + `model_slots`）
- **STT 语音输入** — SenseVoice，`Alt+T` 快捷键，VAD 自动结束
- **记忆系统** — 记忆整理（混合静默触发）+ 记忆反思（定时深层回顾），向量数据库时间衰减召回
- **主动屏幕感知** — ProactiveObserver + 定时截图 + 隐私过滤，视觉模型评估是否发言
- **双语对话** — 气泡显示中文，TTS 播放日语，多参考音色自动选段
- **心情系统（心の記録）** — Sakura 自行判断是否记录心情笔记，不强制
- **Agent 工具循环优化** — 工具组按需激活、网页搜索收束、同轮去重
- **立绘映射** — tone → portrait 自动回退
- **本地 LLM 路由预留** — `RoutingLlmClient`（默认走云端，本地为实验项）
- **Backchannel 接话** — 对话间隙自然插话（⚠️ 测试中，效果不理想，不建议使用）

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
| [安装指南](docs/SETUP.md) | 上游 Release 完整包流程（**本 fork 源码请优先看上方快速开始**） |
| [技术说明](docs/TECHNICAL_README.md) | 架构与目录结构 |
| [更新日志](CHANGELOG.md) | 版本变更记录 |
| [AGENTS.md](AGENTS.md) | 仓库内 AI Agent 协作约定 |

---

## 致谢与开源许可说明

Sakura Desktop Pet 受桌面 Agent、桌宠交互与插件化生态中多个开源项目启发。特别感谢 [Shinsekai](https://github.com/RachelForster/Shinsekai) 项目及其插件生态在桌宠、角色交互、插件扩展等方向上的探索，为 Sakura 的兼容设计和功能设计提供了参考。

本仓库为上游 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人 fork，**仍适用上游 MIT License**。你可以自由使用、复制、修改、合并、发布、分发、再授权或销售本项目代码，但需要保留本项目的版权声明和 MIT License 文本。

Copyright © 2026 Rvosy

### 第三方代码与兼容说明

本项目中的内置插件 `plugins/playwright_browser` 包含基于以下 MIT 开源项目的代码与改动：

- **Project:** [shinsekai-playwright-browser](https://github.com/RachelForster/shinsekai-playwright-browser)
- **License:** MIT License
- **Copyright:** Copyright © 2026 Chihiro

Sakura 在此基础上进行了适配和修改，用于提供 Playwright 浏览器自动化能力。

感谢所有开源项目作者和贡献者。

---

## 关于本 fork

Personal Edition（`0.9.9-personal.N`）由 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 维护，仅供个人使用；与上游发布版本相互独立。
