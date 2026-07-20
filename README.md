<div align="center">

# Sakura Desktop Pet

**Personal Edition** — 基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人维护发行

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.9.9--personal-informational)](VERSION)
[![Upstream](https://img.shields.io/badge/upstream-0.9.9-lightgrey)](https://github.com/Rvosy/Sakura)

</div>

Sakura 是一个运行在桌面上的 **Agent / 数字生命** 应用：可持续对话、长期记忆、可选语音与屏幕感知，并以角色包承载人格、立绘与音色。

本仓库为 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 维护的 **Personal Edition**（版本号形如 `0.9.9-personal.N`），在追平上游基础设施的同时保留个人向能力增强。面向熟悉源码环境的用户；**不提供独立官方安装包，也不承诺与上游发布节奏完全同步。**

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 对话 Agent | 工具循环、按需激活工具组、网页搜索与提醒等 |
| 长期记忆 | 向量召回、记忆整理与反思、心情笔记（心の記録） |
| 多模型分流 | `api_profiles` + `model_slots`（chat / chat_fast / vision / memory_curation） |
| 语音 | TTS 朗读；可选 STT 语音输入（`Alt+T`） |
| 屏幕感知 | 对话内截图 / 观察；主动观察（ProactiveObserver） |
| 角色与设置 | Tauri 设置页 + 可选角色工坊；手机网页端插件骨架 |

相对上游的增强与已知差异见下文「与上游的关系」。

---

## 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | 主要在 **Windows 10 / 11** 下开发与验证 |
| Python | **3.10+**（推荐 3.11）；解释器路径建议为纯英文 |
| 构建工具 | 首次需能编译 Tauri **设置页**（Rust / Cargo；见快速开始） |
| 网络 | 能访问所配置的 LLM / TTS 服务端点 |
| 角色资源 | 本仓库不附带角色包；需自行从上游 Release 获取 `.char` |

---

## 快速开始（推荐：源码 + `.venv`）

本发行使用 **`run.bat` + 项目内 `.venv`**。  
若你下载的是上游 **Release 完整包**（含 `runtime/`、`install.bat` / `start.bat`），请改看 [docs/SETUP.md](docs/SETUP.md)，**不要**与下方步骤混用。

下列命令以 **Windows CMD** 为准。已有 `.venv`、已编过设置页、已有角色目录时，跳过对应步骤即可。

### 1. 获取源码

```bat
git clone https://github.com/Asphodelusu/sakura-personal.git
cd sakura-personal
```

### 2. 创建虚拟环境并安装依赖（仅首次）

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`requirements.txt` 含嵌入等依赖，**首次安装可能较慢**。

### 3. 编译设置页（仅首次或更新设置前端后）

启动时会拉起 `sakura-settings.exe`；缺失会报错。检查：

```bat
dir tools\settings-tauri\src-tauri\target\release\sakura-settings.exe
```

若无此文件：安装 [Rust](https://rustup.rs)（或 `winget install Rustlang.Rustup`），**重新打开 CMD**，然后：

```bat
cd tools\settings-tauri\src-tauri
cargo build --release
cd ..\..\..
```

若 `cargo` 提示找不到 `link.exe`，请安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)（勾选「使用 C++ 的桌面开发」）。设置页依赖 WebView2；多数 Win10/11 已自带，若窗口无法打开再安装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

角色工坊**不是**首启必需。仅在使用 `start_studio.bat` 或设置内「修改角色」时编译：

```bat
cd tools\studio-tauri\src-tauri
cargo build --release
cd ..\..\..
```

### 4. 准备角色包（仅当还没有 `characters\*\character.json`）

`characters/` 不入库。可从上游 **v0.9.9** Release 下载默认包（体积较大）：

```bat
curl -L -o Sakura.char https://github.com/Rvosy/Sakura/releases/download/v0.9.9/Sakura.char
```

或在浏览器打开 [Rvosy/Sakura Releases](https://github.com/Rvosy/Sakura/releases)，下载附件 **`Sakura.char`**。

### 5. 启动

```bat
run.bat
```

首次进入 Tauri 设置：配置 API Profile / 模型槽位，并 **导入 `.char`**。  
配置细则见 [docs/API_CONFIG.md](docs/API_CONFIG.md)。

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `run.bat` | 启动桌宠（Personal Edition 推荐入口） |
| `start_studio.bat` | 独立启动 Tauri 角色工坊 |
| `python -m pytest tests/unit` | 单元测试（部分 UI 用例需 PySide6） |

---

## 文档索引

| 文档 | 适用对象 |
|------|----------|
| 本文「快速开始」 | **本仓库源码用户（优先）** |
| [API 配置](docs/API_CONFIG.md) | 配置 Profile、`model_slots`、供应商示例 |
| [安装指南](docs/SETUP.md) | 上游 **Release 完整包**（`install.bat` / `runtime/`） |
| [macOS](docs/MACOS_SETUP.md) | macOS 源码与依赖说明 |
| [技术说明](docs/TECHNICAL_README.md) | 架构与目录 |
| [更新日志](CHANGELOG.md) | 版本变更 |
| [AGENTS.md](AGENTS.md) | 仓库内 AI Agent 协作约定 |

---

## 与上游的关系

### 已对齐的 0.9.9 基线

- Tauri 设置页为唯一设置入口；多 Profile + `model_slots`
- Tauri 角色工坊；首次引导 onboarding
- `character_studio` 草稿与备份位于 `data/character_studio/`
- 手机端插件骨架 `plugins/sakura_mobile`

### Personal Edition 增强（摘要）

- 多槽位模型分流（含 `chat_fast` 等）
- STT 语音输入、记忆整理 / 反思、主动屏幕感知
- 双语气泡（中文显示 + 日语 TTS，视角色与配置而定）
- 心情笔记、工具路由与 Agent 循环优化等

### 相对上游已移除的旧路径

- Qt 设置对话框、旧 Qt 角色工坊实现
- `dual_endpoint` 双端点主配置路径（由 `model_slots` 取代）

---

## 致谢与许可

Sakura Desktop Pet 受桌面 Agent、桌宠交互与插件化生态中多个开源项目启发。特别感谢 [Shinsekai](https://github.com/RachelForster/Shinsekai) 及其插件生态在相关方向上的探索。

本仓库为上游 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 的个人 fork，**适用上游 MIT License**。使用、复制、修改、分发时请保留版权声明与许可证文本。

Copyright © 2026 Rvosy

### 第三方说明

内置插件 `plugins/playwright_browser` 基于以下 MIT 项目适配：

- [shinsekai-playwright-browser](https://github.com/RachelForster/shinsekai-playwright-browser)  
  Copyright © 2026 Chihiro

---

## 维护说明

Personal Edition 由 [Asphodelusu/sakura-personal](https://github.com/Asphodelusu/sakura-personal) 维护，供个人使用与实验；与上游正式发行相互独立。问题与讨论请优先在本仓库发起。
