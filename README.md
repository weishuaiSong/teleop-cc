<div align="center">

# teleop-cc

**用语音遥控 Claude Code · 躺着也能写代码**

*A voice-driven remote for Claude Code — code from anywhere*

[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![uv](https://img.shields.io/badge/managed%20by-uv-261230.svg)](https://github.com/astral-sh/uv)
[![Claude Code](https://img.shields.io/badge/built%20on-Claude%20Code-d97757.svg)](https://github.com/anthropics/claude-code)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#-路线图--roadmap)

</div>

---

<a id="readme-zh"></a>

## 中文

把手机当作 Claude Code 的远程入口：手机上语音转文字 → 发给 Telegram Bot → bridge 在你的开发机上启动一个 Claude Code 会话替你执行。读文件、改代码、跑命令、看 diff，结果切片回传到 Telegram。

**典型场景**

- 躺在床上 / 沙发上继续白天没写完的功能
- 通勤路上让 Claude 在公司机器上跑测试 / 改 PR
- 离开座位时用手机推进任务，回来直接看结果

### ✨ 特性

- 🎙️ **为语音设计**：命令短、好念（`/proj teleop`、`/session work`、`/model haiku`）
- 🔀 **多 session 并行**：一个项目里维持多个独立的 Claude 对话，互不污染上下文
- 💾 **断电不丢历史**：session_id 落盘，bridge 重启 / 进程崩溃后能续接 Claude 完整对话
- 🧠 **每 session 独立模型**：`main` 用 Opus、`quick` 用 Haiku，灵活省 token
- ⚠️ **危险操作二次确认**：`Bash` / `Write` / `Edit` / `NotebookEdit` 强制 `/yes` 确认，5 分钟超时默认拒绝
- 🔒 **chat_id 白名单**：只有你自己的 Telegram 账号能驱动 bridge
- 🚀 **运行时加项目**：命令行 `teleop add` 或 bot 内 `/addproj`，不用改配置、不用重启 bridge
- 📦 **uv 全程管理**：单文件 + `pyproject.toml`，几条命令装完跑

### 🏗️ 架构

```
手机 (语音 → 文本)
      │
      ▼
Telegram Bot ─────► bridge.py ─────► Claude Agent SDK ─► Claude Code CLI
      ▲                │
      └──── 回复 ───────┘
```

一个 Telegram chat ⇄ 多个命名 session（按项目分组）。每个 session 是一个独立的 `ClaudeSDKClient`，拥有自己的对话历史；切 session 不影响其他 session 的状态。

### 🚀 快速开始

**前置依赖**

- Python ≥ 3.10
- [`uv`](https://github.com/astral-sh/uv) 管理 Python 环境
- [Claude Code CLI](https://github.com/anthropics/claude-code) 已登录：`npm i -g @anthropic-ai/claude-code`
- 从 [@BotFather](https://t.me/BotFather) 拿到的 Telegram Bot token

**安装**

```bash
git clone https://github.com/weishuaiSong/teleop-cc.git
cd teleop-cc
uv sync
cp .env.example .env       # 编辑 .env，填 token 和项目路径
uv run python bridge.py
```

**首次启动获取 chat_id**

如果你还不知道自己的 `chat_id`：

1. `.env` 里先填 `ALLOWED_CHAT_IDS=0`，启动 bridge
2. Telegram 里给你的 Bot 发 `/id`（这条命令故意不做鉴权）
3. 把回的 chat_id 填回 `.env`，重启

### 💬 它用起来什么样

**修个 typo**

```
你   把 bridge.py 里 main 函数那个 typo 修了，是 Aplication 拼错
Bot  🔧 Grep Aplication
Bot  🔧 Read bridge.py:660-680
Bot  ⚠️ [teleop/main] Edit
     C:\work\teleop\bridge.py
     /yes a1b2c3  |  /no a1b2c3
你   /yes a1b2c3
Bot  🔧 Edit C:\work\teleop\bridge.py
Bot  已修复 line 663：Aplication → Application
```

**切模型省 token**

```
你   /model haiku
Bot  → [teleop/main] model=haiku
你   列一下当前所有 .md 文件
Bot  🔧 Glob *.md
Bot  找到 2 个文件: README.md, LICENSE
```

**多 session 并行**

```
你   /session debug
Bot  → [teleop/debug]
你   跑一下 pytest 看看
Bot  🔧 Bash pytest -q
     ...
你   /session main
Bot  → [teleop/main]
你   继续刚才那个重构，把 ChatState 抽成单独的文件
```

### 🛠️ 命令参考

#### 命令行（开发机本地）

不需要 bridge 在跑，也能管理项目列表；改完 bridge 立即可见，**不用重启**。

| 命令 | 作用 |
| --- | --- |
| `uv run python bridge.py` | 启动 bridge（无 subcommand） |
| `uv run python bridge.py add [名字]` | 注册当前目录为项目（默认别名 = 目录名） |
| `uv run python bridge.py add 别名 --path C:\path` | 注册任意路径 |
| `uv run python bridge.py rm 别名` | 删除一个运行时项目 |
| `uv run python bridge.py list` | 列出所有项目（`.env` + 运行时） |

全局安装让 `teleop` 在任何目录可用：

```bash
uv tool install --editable .
uv tool update-shell    # 第一次需要，把 ~/.local/bin 加进 PATH
# 重开终端
cd C:\work\some-project
teleop add              # 一行加进项目列表
teleop list
```

#### Telegram bot 命令

| 命令 | 作用 |
| --- | --- |
| `/id` | 显示当前 chat_id（无鉴权，首次发现用） |
| `/whoami` | 显示 chat / project / session / model / cwd |
| `/projects` | 列出所有项目（标注 `.env` / `live`） |
| `/proj <别名>` | 切到对应项目 |
| `/addproj <别名> <路径>` | 运行时新增一个项目 |
| `/rmproj <别名>` | 移除一个运行时项目 |
| `/sessions` | 列出当前项目下所有 session |
| `/session <名字>` | 切到/新建一个命名 session |
| `/drop <名字>` | 删除一个 session（内存 + 落盘） |
| `/model [名字]` | 显示或切换当前 session 的模型 |
| `/models` | 列出已知模型别名 |
| `/new` | 重置当前 session（清空历史） |
| `/yes <token>` / `/no <token>` | 确认/拒绝危险工具调用 |
| _（普通文字）_ | 作为下一轮用户消息发给当前 session |

`/sessions` 输出示例：

```
[teleop]
* ● 💾 main
  ○ 💾 debug
  ● 💾 quick
(* current  ● in-memory  💾 persisted)
```

- `*` 当前活跃 session
- `●` 内存里有 live SDK client
- `💾` 落盘过，下次 `/session` 切过去会 resume 完整历史

### ⚙️ 配置

#### `.env`

```dotenv
TELEGRAM_BOT_TOKEN=<@BotFather 给的 token>
ALLOWED_CHAT_IDS=<你的 chat_id，多个用逗号分隔>
PROJECTS=teleop=C:\work\teleop,foo=C:\path\to\foo
```

`PROJECTS` 第一条是默认项目（启动时落到这里）。`.env` 里的项目优先级高于 `projects.json`，不能用 `/rmproj` 或 `teleop rm` 删。

#### `projects.json`（自动生成）

`/addproj` 和 `teleop add` 加的项目落到这里：

```json
{
  "myproj": "C:\\path\\to\\myproj"
}
```

#### `sessions.json`（自动生成）

每个 session 第一次有回复后 session_id 和 model 写入：

```json
{
  "8285174078": {
    "teleop": {
      "main":  { "id": "33ecb5f9-...", "model": null },
      "debug": { "id": "abcd1234-...", "model": "haiku" }
    }
  }
}
```

bridge 重启 → `/session main` → SDK 用 `resume="33ecb5f9-..."` 续上完整对话。

### 🔒 安全模型

- **chat_id 白名单**：除 `/id` 外所有命令要求 `chat_id ∈ ALLOWED_CHAT_IDS`
- **危险工具拦截**：`Bash` / `Write` / `Edit` / `NotebookEdit` 经 `can_use_tool` 回调发到 Telegram，需要 `/yes <token>` 确认，5 分钟超时默认拒绝
- **只读工具放行**：`Read` / `Glob` / `Grep` 等不弹确认
- **token 随机**：6 位十六进制，每次工具调用现生成

威胁模型限定**单用户、可信开发机**。bridge 进程在开发机以你的权限运行 Claude Code，等同你账号本人。不要在不信任的机器上跑、不要泄露 Bot token。

### 📦 项目结构

```
.
├── bridge.py          # 单文件 bridge
├── pyproject.toml     # uv 管的元数据 + 依赖
├── uv.lock
├── .env.example
├── sessions.json      # 自动生成
├── projects.json      # 自动生成
├── LICENSE
└── README.md
```

### 🐛 故障排查

**启动卡住，`getMe ConnectTimeout`**
代理和 `api.telegram.org` 握手超时。换节点或重连梯子再启动。

**Bot 没反应**
- 看日志有没有 `Application started`
- 看日志里有没有 `unauthorized chat=xxx`——chat_id 没进白名单

**`/sessions` 显示 `○ 💾 main` 但发消息后没历史**
SDK resume 极少数情况会失败，会自动降级为新建 session 并清掉那条 `sessions.json` 记录。日志里会看到 `resume failed for ..., falling back to new session`。

**`teleop` 命令找不到**
`uv tool install --editable .` 之后要 `uv tool update-shell` + **重开终端**。已开窗口认不到新 PATH。

### 🗺️ 路线图 / Roadmap

- [ ] inline keyboard 按钮代替 `/yes` `/no`（语音场景更省事）
- [ ] `/import <session-id>` 把 IDE Claude 的会话接到 bot
- [ ] TTS 回传：让 bot 用语音念回复，开车通勤场景更友好
- [ ] Linux / macOS 验证（当前在 Windows 11 跑通）

### 📄 协议

[MIT](LICENSE) © 2026 Weishuai Song

---

<a id="readme-en"></a>

## English

A Telegram-to-Claude-Code bridge for voice-driven development. Speak into any voice-to-text app on your phone, send the text to a Telegram bot, and Claude Code runs against your project on a remote machine. Read files, edit code, run commands, review diffs — all streamed back as chat messages.

**Typical scenarios**

- Keep coding from the couch / bed when you're done sitting at the desk
- On the commute: have Claude run tests or update a PR on your office machine
- Away from the keyboard: nudge tasks forward by phone, review when you're back

### ✨ Features

- 🎙️ **Voice-first ergonomics** — short, speakable commands (`/proj teleop`, `/session work`, `/model haiku`)
- 🔀 **Multiple sessions per project** — independent Claude conversations that don't pollute each other's context
- 💾 **Survives restarts** — session IDs are persisted, the bridge resumes Claude's full history after a restart
- 🧠 **Per-session model** — pin `main` to Opus, `quick` to Haiku
- ⚠️ **Safety gate** — `Bash` / `Write` / `Edit` / `NotebookEdit` require an explicit `/yes` confirmation in chat
- 🔒 **Chat-ID whitelist** — only your own Telegram account talks to the bridge
- 🚀 **Live project registration** — add a project via `teleop add` from any terminal, or `/addproj` from the bot, no restart needed
- 📦 **uv all the way** — one file, one `pyproject.toml`, a few commands and you're running

### 🏗️ Architecture

```
Phone (voice → text)
      │
      ▼
Telegram Bot ─────► bridge.py ─────► Claude Agent SDK ─► Claude Code CLI
      ▲                │
      └─── replies ────┘
```

One Telegram chat ⇄ many named Claude sessions, scoped per project. Each session is an independent `ClaudeSDKClient` with its own history; switching sessions doesn't affect the others.

### 🚀 Quick Start

**Prerequisites**

- Python ≥ 3.10
- [`uv`](https://github.com/astral-sh/uv) for dependency management
- [Claude Code CLI](https://github.com/anthropics/claude-code) authenticated: `npm i -g @anthropic-ai/claude-code`
- A Telegram Bot token from [@BotFather](https://t.me/BotFather)

**Install**

```bash
git clone https://github.com/weishuaiSong/teleop-cc.git
cd teleop-cc
uv sync
cp .env.example .env       # edit .env, fill in token and project paths
uv run python bridge.py
```

**First-run: discovering your `chat_id`**

1. Set `ALLOWED_CHAT_IDS=0` in `.env` and start the bridge.
2. In Telegram, open your bot and send `/id`. The `/id` command is intentionally unauthenticated.
3. Paste the returned id into `.env` and restart.

### 💬 What It Feels Like

**Fix a typo**

```
You   Fix the typo in bridge.py main(): "Aplication" should be "Application"
Bot   🔧 Grep Aplication
Bot   🔧 Read bridge.py:660-680
Bot   ⚠️ [teleop/main] Edit
      C:\work\teleop\bridge.py
      /yes a1b2c3  |  /no a1b2c3
You   /yes a1b2c3
Bot   🔧 Edit C:\work\teleop\bridge.py
Bot   Fixed line 663: Aplication → Application
```

**Switch model to save tokens**

```
You   /model haiku
Bot   → [teleop/main] model=haiku
You   List all markdown files
Bot   🔧 Glob *.md
Bot   Found 2: README.md, LICENSE
```

**Parallel sessions**

```
You   /session debug
Bot   → [teleop/debug]
You   Run pytest please
Bot   🔧 Bash pytest -q
You   /session main
Bot   → [teleop/main]
You   Continue the refactor — extract ChatState into its own file
```

### 🛠️ Command Reference

#### CLI (on the dev machine)

You don't need the bridge running to manage projects; changes are picked up live — **no restart needed**.

| Command | Effect |
| --- | --- |
| `uv run python bridge.py` | Start the bridge (no subcommand) |
| `uv run python bridge.py add [name]` | Register cwd as a project (alias defaults to the directory name) |
| `uv run python bridge.py add name --path C:\path` | Register an arbitrary path |
| `uv run python bridge.py rm name` | Remove a runtime project |
| `uv run python bridge.py list` | List all projects (env + runtime) |

For a globally available `teleop` command:

```bash
uv tool install --editable .
uv tool update-shell    # first time only — adds ~/.local/bin to PATH
# reopen terminal
cd C:\work\some-project
teleop add              # one-liner project registration
teleop list
```

#### Telegram bot commands

| Command | Effect |
| --- | --- |
| `/id` | Reply with `chat_id` (no auth). |
| `/whoami` | Show chat / project / session / model / cwd. |
| `/projects` | List configured projects (annotated `.env` / `live`). |
| `/proj <name>` | Switch project. |
| `/addproj <name> <path>` | Register a new project at runtime. |
| `/rmproj <name>` | Remove a runtime project. |
| `/sessions` | List sessions in the current project. |
| `/session <name>` | Switch to / create a named session. |
| `/drop <name>` | Delete a session (memory + persisted id). |
| `/model [name]` | Show or switch the current session's model. |
| `/models` | List known model shortcuts. |
| `/new` | Reset the current session (clear history). |
| `/yes <token>` / `/no <token>` | Approve / deny a pending dangerous tool call. |
| _(plain text)_ | Sent to the current session as the next user message. |

Example `/sessions` output:

```
[teleop]
* ● 💾 main
  ○ 💾 debug
  ● 💾 quick
(* current  ● in-memory  💾 persisted)
```

- `*` currently active
- `●` in-memory (live SDK client)
- `💾` persisted (resumes Claude's full history on next use)

### ⚙️ Configuration

#### `.env`

```dotenv
TELEGRAM_BOT_TOKEN=<from @BotFather>
ALLOWED_CHAT_IDS=<your chat id, comma-separated for multiple>
PROJECTS=teleop=C:\work\teleop,foo=C:\path\to\foo
```

The first `PROJECTS` entry is the default project on startup. `.env` entries take priority over `projects.json` and cannot be removed via `/rmproj` or `teleop rm`.

#### `projects.json` (auto-generated)

Projects added via `/addproj` or `teleop add`:

```json
{
  "myproj": "C:\\path\\to\\myproj"
}
```

#### `sessions.json` (auto-generated)

Persisted after the first reply in each session:

```json
{
  "8285174078": {
    "teleop": {
      "main":  { "id": "33ecb5f9-...", "model": null },
      "debug": { "id": "abcd1234-...", "model": "haiku" }
    }
  }
}
```

After bridge restart, `/session main` will reconnect via the SDK's `resume="33ecb5f9-..."` option.

### 🔒 Security Model

- **Chat-ID whitelist**: every command except `/id` requires `chat_id ∈ ALLOWED_CHAT_IDS`.
- **Dangerous-tool gate**: `Bash` / `Write` / `Edit` / `NotebookEdit` route through `can_use_tool` to the chat; user must reply `/yes <token>` within 5 minutes or the call is denied.
- **Read-only tools pass through**: `Read`, `Glob`, `Grep`, etc.
- **Random tokens**: 6 hex characters, regenerated per tool call.

Threat model assumes a **single-user, trusted dev machine**. The bridge runs Claude Code on that machine with your privileges — effectively as you. Don't host this on untrusted hardware, don't share your Bot token.

### 📦 Project Layout

```
.
├── bridge.py          # single-file bridge
├── pyproject.toml     # uv-managed metadata + deps
├── uv.lock
├── .env.example
├── sessions.json      # auto-generated
├── projects.json      # auto-generated
├── LICENSE
└── README.md
```

### 🐛 Troubleshooting

**Startup hangs on `getMe ConnectTimeout`**
Your proxy is timing out reaching `api.telegram.org`. Switch nodes or reconnect the VPN and try again.

**Bot doesn't respond**
- Check the log for `Application started`.
- Check for `unauthorized chat=xxx` — the chat is not in the whitelist.

**`/sessions` shows `○ 💾 main` but no history after a message**
SDK resume occasionally fails; the bridge automatically falls back to a new session and clears the stale entry from `sessions.json`. You'll see `resume failed for ..., falling back to new session` in the log.

**`teleop` command not found**
After `uv tool install --editable .`, run `uv tool update-shell` and **reopen the terminal**. Already-open shells don't pick up the new PATH.

### 🗺️ Roadmap

- [ ] Inline-keyboard buttons instead of `/yes` `/no` (better for voice)
- [ ] `/import <session-id>` to attach a Claude Code IDE conversation to the bot
- [ ] TTS replies (drive / commute-friendly)
- [ ] Verified on Linux / macOS (currently developed on Windows 11)

### 📄 License

[MIT](LICENSE) © 2026 Weishuai Song
