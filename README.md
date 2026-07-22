<div align="center">

# Sakura Desktop Pet — Personal Edition

基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人维护分支

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.9.9--personal.3-informational)](VERSION)
[![Upstream](https://img.shields.io/badge/upstream-Rvosy%2FSakura-lightgrey)](https://github.com/Rvosy/Sakura)

</div>

### 一个能主动感知屏幕内容与系统事件的桌宠 Agent

Sakura 会持续对话、记住长期信息，并在合适的时候主动开口。角色包决定她的风格、立绘与音色，内置 Agent 负责工具调用与感知。

本仓库是 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 维护的 **Personal Edition**（当前版本 `0.9.9-personal.3`）。它从上游 Sakura 分出，保留同一套桌宠与 Agent 骨架，并在记忆连贯、时间感、互动节奏和屏幕感知等方向做了更偏个人使用的强化。

面向愿意自己搭源码环境的用户：**不提供独立官方安装包**，也不保证与上游发版节奏同步。若你需要开箱即用的完整包（含 `runtime/`、`install.bat` 等），请直接使用上游 [Releases](https://github.com/Rvosy/Sakura/releases)。

---

## 与上游的关系

主干能力与上游大致相同：Tauri 设置页与多模型槽位、角色工坊、长期记忆、接话、TTS、MCP、Playwright 浏览器插件、可选手机网页端等。日常对话、工具调用、角色导入的基本体验可以按上游项目来理解。

本仓库额外侧重、或实现方式已经不同的部分主要有：

**主动屏幕感知**  
上游常见做法是定时截图、攒一批画面再交给主模型找话题。本仓库改成独立的观察循环：结合前台窗口截图与控件文字，先由视觉模型写下「看到了什么、想到了什么」，再由对话模型决定要不要开口。支持隐私黑名单（敏感进程 / 窗口标题）、按专注程度调整观察间隔，以及「出门了 / 晚安」一类离开时自动暂停。

**记忆更连贯**  
在上游分层记忆与自动整理之上，增加了空闲时的记忆反思（以及对反思再归纳的二阶总结）、人物/事物相关的轻量实体索引，以及按「最近是否被想起」跟踪记忆访问。情绪侧用连续的愉悦度–唤醒度空间去影响「此刻更贴哪类记忆」，而不是只靠离散标签。

**聊天历史存储**  
上游使用 JSONL；本仓库改为 SQLite（读写更快，首次启动可从旧 JSONL 自动迁移），对外调用方式保持兼容。

**语音输入**  
集成本地 STT（SenseVoice），可用快捷键或按钮把说话转成输入，不必只靠打字。

**互动节奏**  
在部分亲密或高互动场景下，可切换更快的回复节奏，并在对方沉默时有限度地续说（由内置工具与本地指引控制，默认仍克制）。

**发行方式**  
上游提供 Windows 完整包与更新脚本等；本仓库只维护源码流程（`.venv` + `run.bat`），方便个人改代码与试验，不维护单独的安装包发行线。

更细的目录与配置说明见 [docs/TECHNICAL_README.md](docs/TECHNICAL_README.md)，版本变更见 [CHANGELOG.md](CHANGELOG.md)。

---

## 环境要求

- **系统**：主要在 Windows 10 / 11 下开发与验证
- **Python**：3.10+（推荐 3.11），解释器路径建议为纯英文
- **构建**：首次需本地编译 Tauri 设置页（Rust / Cargo）
- **网络**：能访问所配置的 LLM 服务端点；首次拉取记忆相关模型时可能较慢
- **角色资源**：本仓库不附带角色包，需从上游 Release 下载 `.char`

---

## 快速开始（源码 + `.venv`）

入口：`run.bat` → `.venv\Scripts\python.exe main.py`。设置页二进制位于 `tools\settings-tauri\...\sakura-settings.exe`。角色包可从上游 [v0.9.9 Release](https://github.com/Rvosy/Sakura/releases/tag/v0.9.9) 获取。

命令以 **Windows CMD** 为准。已有 `.venv`、已编过设置页、或已有角色目录时，跳过对应步骤即可。

你需要准备：本仓库源码、`requirements.txt` 依赖、本地编好的设置页程序、上游的 `Sakura.char`，以及自己的 LLM API（Base URL / Key / 模型名）。TTS 可后配，没有语音也能先对话。

### 1. 拉取源码

```bat
git clone https://github.com/Asphodelusu/sakura-personal.git
cd sakura-personal
```

### 2. 创建虚拟环境并安装依赖（仅首次）

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

含嵌入等相关依赖，首次安装可能较慢。

### 3. 编译设置页（仅首次，或设置前端有更新时）

启动时会拉起 `sakura-settings.exe`，缺失会直接报错。检查：

```bat
dir tools\settings-tauri\src-tauri\target\release\sakura-settings.exe
```

若没有该文件：安装 [Rust](https://rustup.rs)（或 `winget install Rustlang.Rustup`），重新打开 CMD，然后：

```bat
cd tools\settings-tauri\src-tauri
cargo build --release
cd ..\..\..
```

若 `cargo` 提示找不到 `link.exe`，安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)（勾选「使用 C++ 的桌面开发」）。设置页依赖 WebView2；多数 Win10/11 已自带，窗口起不来再装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

角色工坊不是首启必需。要用 `start_studio.bat` 或设置里「修改角色」时再编：

```bat
cd tools\studio-tauri\src-tauri
cargo build --release
cd ..\..\..
```

### 4. 下载默认角色包（仅当还没有 `characters\*\character.json`）

```bat
curl -L -o Sakura.char https://github.com/Rvosy/Sakura/releases/download/v0.9.9/Sakura.char
```

或在浏览器打开 [Rvosy/Sakura Releases · v0.9.9](https://github.com/Rvosy/Sakura/releases/tag/v0.9.9)，下载附件 **`Sakura.char`**（约 309 MB）。文件先放在任意位置即可，下一步在设置页里导入。

### 5. 启动并完成首次配置

```bat
run.bat
```

若缺少角色包或未配置可用的 chat 模型，会进入 Tauri 首次设置：

1. **导入 .char**（选择刚下载的 `Sakura.char`）
2. 配置 API Profile 与模型槽位（至少让 **chat** 能通；主动看屏还需配置支持识图的 **vision_chat**）
3. 保存后继续启动

配置说明见 [docs/API_CONFIG.md](docs/API_CONFIG.md)。完成后日常只需再执行 `run.bat`。

---

## 常用命令

- `run.bat` — 启动桌宠
- `start_studio.bat` — 独立启动 Tauri 角色工坊
- `python -m pytest tests/unit` — 单元测试（部分 UI 用例需要可用的 PySide6）

---

## 文档

- [API 配置](docs/API_CONFIG.md) — Profile、`model_slots`、供应商示例
- [安装指南](docs/SETUP.md) — 上游 Release 完整包（`install.bat` / `runtime/`）说明
- [macOS](docs/MACOS_SETUP.md) — macOS 源码与依赖
- [技术说明](docs/TECHNICAL_README.md) — 架构与目录
- [更新日志](CHANGELOG.md) — 版本变更
- [AGENTS.md](AGENTS.md) — 仓库内 AI Agent 协作约定

---

## 致谢与开源许可说明

Sakura Desktop Pet 受桌面 Agent、桌宠交互与插件化生态中多个开源项目启发。特别感谢 [Shinsekai](https://github.com/RachelForster/Shinsekai) 及其插件生态在相关方向上的探索。

本仓库为上游 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人 fork，适用上游 MIT License。使用、复制、修改、分发时请保留版权声明与许可证文本。

Copyright © 2026 Rvosy

### 第三方代码与兼容说明

内置插件 `plugins/playwright_browser` 基于以下 MIT 项目适配：

- [shinsekai-playwright-browser](https://github.com/RachelForster/shinsekai-playwright-browser)  
  Copyright © 2026 Chihiro

---

## 关于本仓库

Personal Edition（当前 `0.9.9-personal.3`）由 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 维护，供个人使用与实验，与上游正式发行相互独立。问题与讨论请优先在本仓库发起。
