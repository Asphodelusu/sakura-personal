<div align="center">

# Sakura Desktop Pet — Personal Edition

基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) **0.9.9** 的个人维护分支

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.9.9--personal.2-informational)](VERSION)
[![Upstream](https://img.shields.io/badge/upstream-0.9.9-lightgrey)](https://github.com/Rvosy/Sakura)

</div>

### 一个能主动感知屏幕内容与系统事件的桌宠 Agent

Sakura 会持续对话、记住长期信息，并在合适的时候主动开口；角色包决定她的风格、立绘与音色，内置 Agent 负责工具调用与感知。

本仓库为 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 的 **Personal Edition**（版本号 `0.9.9-personal.N`）：在追平上游 0.9.9 基础设施的同时，保留并强化个人向能力。主要开发方向是 **提高真实度与代入感**（记忆连贯、时间感、互动节奏、屏幕感知等）。

面向熟悉源码环境的用户；**不提供独立官方安装包，也不承诺与上游发布节奏完全同步。**

> 想用开箱即用的完整包，请直接去上游 [Releases](https://github.com/Rvosy/Sakura/releases)（`install.bat` / `start.bat` / `runtime/`），不要和下方源码流程混用。源码流程说明见本文；上游完整包见 [docs/SETUP.md](docs/SETUP.md)。

---

## 环境要求

| 项目 | 说明 |
|------|------|
| 系统 | 主要在 **Windows 10 / 11** 下开发与验证 |
| Python | **3.10+**（推荐 3.11），解释器路径建议为纯英文 |
| 构建 | 首次需本地编出 Tauri **设置页**（Rust / Cargo） |
| 网络 | 能访问所配置的 LLM 服务端点；首次记忆相关依赖可能从镜像拉模型 |
| 角色资源 | 本仓库不附带角色包，需从上游 Release 下载 `.char` |

---

## 快速开始（源码 + `.venv`）

下列步骤已按本仓库当前入口核对：`run.bat` → `.venv\Scripts\python.exe main.py`；设置页二进制解析到 `tools\settings-tauri\...\sakura-settings.exe`；默认角色包下载地址对上游 `v0.9.9` 的 `Sakura.char` 有效（HTTP 302）。

命令以 **Windows CMD** 为准。已有 `.venv` / 已编过设置页 / 已有角色目录时，跳过对应步骤即可。

### 你需要准备的外部文件与配置

| 需要什么 | 从哪里来 | 用在哪里 |
|----------|----------|----------|
| 源码 | 本仓库 `git clone` | 项目根目录 |
| Python 依赖 | `requirements.txt` → `.venv` | `run.bat` 调用该解释器 |
| 设置页程序 | 本地 `cargo build --release` | 首次启动 / 改配置时打开 |
| 角色包 `Sakura.char` | 上游 [Releases · v0.9.9](https://github.com/Rvosy/Sakura/releases/tag/v0.9.9) 附件，或下方 `curl` | 首次设置页里 **导入 .char** |
| LLM API | 你自己的供应商（Base URL / Key / 模型名） | 首次设置页的 **模型 / Profile**；细则见 [docs/API_CONFIG.md](docs/API_CONFIG.md) |

TTS（GPT-SoVITS）可按需后续配置；没有语音也能先对话。

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

含嵌入等相关依赖，**首次安装可能较慢**。

### 3. 编译设置页（仅首次，或设置前端有更新时）

启动时会拉起 `sakura-settings.exe`，缺失会直接报错。检查：

```bat
dir tools\settings-tauri\src-tauri\target\release\sakura-settings.exe
```

若没有该文件：安装 [Rust](https://rustup.rs)（或 `winget install Rustlang.Rustup`），**重新打开 CMD**，然后：

```bat
cd tools\settings-tauri\src-tauri
cargo build --release
cd ..\..\..
```

若 `cargo` 提示找不到 `link.exe`，安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)（勾选「使用 C++ 的桌面开发」）。设置页依赖 WebView2；多数 Win10/11 已自带，窗口起不来再装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

角色工坊**不是**首启必需。要用 `start_studio.bat` 或设置里「修改角色」时再编：

```bat
cd tools\studio-tauri\src-tauri
cargo build --release
cd ..\..\..
```

### 4. 下载默认角色包（仅当还没有 `characters\*\character.json`）

```bat
curl -L -o Sakura.char https://github.com/Rvosy/Sakura/releases/download/v0.9.9/Sakura.char
```

或浏览器打开 [Rvosy/Sakura Releases · v0.9.9](https://github.com/Rvosy/Sakura/releases/tag/v0.9.9) → 下载附件 **`Sakura.char`**（约 309 MB）。

文件先放在任意位置即可；下一步在设置页里导入。

### 5. 启动并完成首次配置

```bat
run.bat
```

若缺少角色包或未配置可用的 chat 模型（Base URL / API Key / 模型名），会进入 Tauri 首次设置：

1. **导入 .char**（选择刚下载的 `Sakura.char`）
2. 配置 API Profile 与模型槽位（至少让 **chat** 能通）
3. 保存后继续启动

配置说明与示例见 [docs/API_CONFIG.md](docs/API_CONFIG.md)。

完成后日常只需再执行 `run.bat`。

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `run.bat` | 启动桌宠（本发行推荐入口） |
| `start_studio.bat` | 独立启动 Tauri 角色工坊 |
| `python -m pytest tests/unit` | 单元测试（部分 UI 用例需 PySide6） |

---

## 与上游 0.9.9 的对齐与差异

### 已对齐

- Tauri 设置页为唯一设置入口；多 Profile + `model_slots`
- Tauri 角色工坊；首次引导 onboarding
- `character_studio` 草稿与备份位于 `data/character_studio/`
- 手机端插件骨架 `plugins/sakura_mobile`

### Personal Edition 增强（摘要）

- 多槽位模型分流（chat / chat_fast / vision / memory_curation 等）
- STT 语音输入、记忆整理 / 反思、时间感与记忆连贯相关改进
- 主动屏幕感知（ProactiveObserver）
- 双语气泡（中文显示 + 日语 TTS，视角色与配置而定）
- 心情笔记、工具路由与 Agent 循环优化等

### 相对上游已移除的旧路径

- Qt 设置对话框、旧 Qt 角色工坊实现
- `dual_endpoint` 双端点主配置路径（由 `model_slots` 取代）

---

## 文档

| 文档 | 适用对象 |
|------|----------|
| 本文「快速开始」 | **本仓库源码用户（优先）** |
| [API 配置](docs/API_CONFIG.md) | Profile、`model_slots`、供应商示例 |
| [安装指南](docs/SETUP.md) | 上游 **Release 完整包**（`install.bat` / `runtime/`） |
| [macOS](docs/MACOS_SETUP.md) | macOS 源码与依赖说明 |
| [技术说明](docs/TECHNICAL_README.md) | 架构与目录 |
| [更新日志](CHANGELOG.md) | 版本变更 |
| [AGENTS.md](AGENTS.md) | 仓库内 AI Agent 协作约定 |

---

## 致谢与开源许可说明

Sakura Desktop Pet 受桌面 Agent、桌宠交互与插件化生态中多个开源项目启发。特别感谢 [Shinsekai](https://github.com/RachelForster/Shinsekai) 及其插件生态在相关方向上的探索。

本仓库为上游 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人 fork，**适用上游 MIT License**。使用、复制、修改、分发时请保留版权声明与许可证文本。

Copyright © 2026 Rvosy

### 第三方代码与兼容说明

内置插件 `plugins/playwright_browser` 基于以下 MIT 项目适配：

- [shinsekai-playwright-browser](https://github.com/RachelForster/shinsekai-playwright-browser)  
  Copyright © 2026 Chihiro

---

## 关于本 fork

Personal Edition（`0.9.9-personal.N`）由 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 维护，供个人使用与实验；与上游正式发行相互独立。问题与讨论请优先在本仓库发起。
