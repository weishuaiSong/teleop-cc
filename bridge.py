import asyncio
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = {
    int(x)
    for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}


def parse_projects(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, path = entry.split("=", 1)
        out[name.strip()] = Path(path.strip()).resolve()
    return out


_ENV_PROJECTS = parse_projects(os.environ.get("PROJECTS", ""))
PROJECTS_FILE = Path(__file__).parent / "projects.json"


def load_file_projects() -> dict[str, Path]:
    if not PROJECTS_FILE.exists():
        return {}
    try:
        raw = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        return {k: Path(v).resolve() for k, v in raw.items()}
    except Exception:
        return {}


def save_file_projects(projects: dict[str, Path]) -> None:
    tmp = PROJECTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({k: str(v) for k, v in projects.items()}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(PROJECTS_FILE)


def all_projects() -> dict[str, Path]:
    merged = dict(_ENV_PROJECTS)
    for k, v in load_file_projects().items():
        merged.setdefault(k, v)
    return merged


def _initial_default() -> tuple[str, Path]:
    merged = all_projects()
    if merged:
        name, path = next(iter(merged.items()))
        return name, path
    return "_default", Path(os.getcwd()).resolve()


DEFAULT_PROJECT, DEFAULT_CWD = _initial_default()
DEFAULT_SESSION_NAME = "main"

DANGEROUS_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit"}
TELEGRAM_CHUNK = 3500
DECISION_TIMEOUT = 300

KNOWN_MODELS = ["opus", "sonnet", "haiku"]

SESSIONS_FILE = Path(__file__).parent / "sessions.json"


def _normalize_record(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"id": raw, "model": None}
    return raw


def load_registry() -> dict[str, dict[str, dict[str, str]]]:
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to load %s, starting empty", SESSIONS_FILE)
        return {}


def save_registry(registry: dict) -> None:
    tmp = SESSIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    tmp.replace(SESSIONS_FILE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bridge")


def chunks(text: str, n: int):
    for i in range(0, len(text), n):
        yield text[i : i + n]


def summarize_tool(name: str, inp: dict) -> str:
    if name == "Bash":
        return f"`{inp.get('command', '')[:500]}`"
    if name in {"Write", "Edit"}:
        return f"{inp.get('file_path', '')}"
    return str(inp)[:300]


class ClaudeSession:
    def __init__(
        self,
        owner: "ChatState",
        project: str,
        name: str,
        cwd: Path,
        resume_id: str | None = None,
        model: str | None = None,
    ):
        self.owner = owner
        self.project = project
        self.name = name
        self.cwd = cwd
        self.resume_id = resume_id
        self.session_id: str | None = resume_id
        self.model = model
        self.client: ClaudeSDKClient | None = None
        self.lock = asyncio.Lock()

    async def _gate_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> dict:
        if tool_name not in DANGEROUS_TOOLS:
            return {"behavior": "allow", "updatedInput": tool_input}

        token = secrets.token_hex(3)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.owner.pending[token] = fut

        summary = summarize_tool(tool_name, tool_input)
        await self.owner.app.bot.send_message(
            self.owner.chat_id,
            f"⚠️ [{self.project}/{self.name}] {tool_name}\n{summary}\n\n/yes {token}  |  /no {token}",
        )

        try:
            allowed = await asyncio.wait_for(fut, timeout=DECISION_TIMEOUT)
        except asyncio.TimeoutError:
            allowed = False
            await self.owner.app.bot.send_message(
                self.owner.chat_id, f"⏱ {token} timed out, denied"
            )
        finally:
            self.owner.pending.pop(token, None)

        if allowed:
            return {"behavior": "allow", "updatedInput": tool_input}
        return {"behavior": "deny", "message": "denied by user"}

    async def _ensure_client(self):
        if self.client is not None:
            return
        opts: dict[str, Any] = dict(
            cwd=str(self.cwd),
            permission_mode="default",
            can_use_tool=self._gate_tool,
        )
        if self.resume_id is not None:
            opts["resume"] = self.resume_id
        if self.model is not None:
            opts["model"] = self.model
        try:
            self.client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
            await self.client.connect()
        except Exception:
            if self.resume_id is None:
                raise
            log.warning(
                "resume failed for %s, falling back to new session",
                self.resume_id,
            )
            self.resume_id = None
            self.session_id = None
            self.owner.forget_session_id(self.project, self.name)
            opts.pop("resume", None)
            self.client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
            await self.client.connect()
        log.info(
            "claude session connected: chat=%s project=%s name=%s cwd=%s resume=%s model=%s",
            self.owner.chat_id,
            self.project,
            self.name,
            self.cwd,
            self.resume_id,
            self.model,
        )

    async def send(self, prompt: str):
        if self.lock.locked():
            await self.owner.app.bot.send_message(
                self.owner.chat_id,
                f"⏳ [{self.project}/{self.name}] busy, switch session or wait",
            )
            return
        async with self.lock:
            await self._ensure_client()
            assert self.client is not None
            await self.client.query(prompt)
            async for msg in self.client.receive_response():
                sid = getattr(msg, "session_id", None)
                if sid and sid != self.session_id:
                    self.session_id = sid
                    self.owner.remember_session(
                        self.project, self.name, session_id=sid
                    )
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await self._send_text(block.text)
                        elif isinstance(block, ToolUseBlock):
                            await self._send_text(
                                f"\U0001f527 {block.name} {summarize_tool(block.name, block.input)}"
                            )
                elif isinstance(msg, ResultMessage):
                    break

    async def _send_text(self, text: str):
        for chunk in chunks(text, TELEGRAM_CHUNK):
            await self.owner.app.bot.send_message(self.owner.chat_id, chunk)

    async def reset(self):
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def switch_model(self, model: str | None):
        self.model = model
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


class ChatState:
    def __init__(self, chat_id: int, app: Application):
        self.chat_id = chat_id
        self.app = app
        self.current_project: str = DEFAULT_PROJECT
        self.current_session: dict[str, str] = {}
        self.sessions: dict[tuple[str, str], ClaudeSession] = {}
        self.pending: dict[str, asyncio.Future] = {}

    def _key(self) -> str:
        return str(self.chat_id)

    def _registry_subtree(self, registry: dict) -> dict:
        return registry.setdefault(self._key(), {})

    def _record(self, project: str, name: str) -> dict | None:
        registry = load_registry()
        raw = registry.get(self._key(), {}).get(project, {}).get(name)
        return _normalize_record(raw)

    def known_session_id(self, project: str, name: str) -> str | None:
        rec = self._record(project, name)
        return rec["id"] if rec else None

    def known_session_model(self, project: str, name: str) -> str | None:
        rec = self._record(project, name)
        return rec["model"] if rec else None

    def remember_session(
        self,
        project: str,
        name: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        registry = load_registry()
        sub = self._registry_subtree(registry)
        proj = sub.setdefault(project, {})
        cur = _normalize_record(proj.get(name)) or {"id": None, "model": None}
        if session_id is not None:
            cur["id"] = session_id
        if model is not None:
            cur["model"] = model
        proj[name] = cur
        save_registry(registry)

    def forget_session_id(self, project: str, name: str) -> None:
        registry = load_registry()
        sub = registry.get(self._key(), {})
        proj = sub.get(project, {})
        if name in proj:
            del proj[name]
            if not proj:
                del sub[project]
            if not sub:
                registry.pop(self._key(), None)
            save_registry(registry)

    def known_sessions(self, project: str) -> list[str]:
        registry = load_registry()
        return list(registry.get(self._key(), {}).get(project, {}).keys())

    def cwd(self) -> Path:
        return all_projects().get(self.current_project, DEFAULT_CWD)

    def session_name(self) -> str:
        return self.current_session.setdefault(self.current_project, DEFAULT_SESSION_NAME)

    def active(self) -> ClaudeSession:
        key = (self.current_project, self.session_name())
        if key not in self.sessions:
            resume_id = self.known_session_id(self.current_project, self.session_name())
            model = self.known_session_model(self.current_project, self.session_name())
            self.sessions[key] = ClaudeSession(
                self,
                self.current_project,
                self.session_name(),
                self.cwd(),
                resume_id=resume_id,
                model=model,
            )
        return self.sessions[key]

    def list_sessions(self) -> tuple[list[str], str]:
        in_mem = {n for (p, n) in self.sessions if p == self.current_project}
        on_disk = set(self.known_sessions(self.current_project))
        names = sorted(in_mem | on_disk)
        current = self.current_session.get(self.current_project, DEFAULT_SESSION_NAME)
        if current not in names:
            names.append(current)
        return names, current

    def switch_project(self, name: str):
        self.current_project = name

    def switch_session(self, name: str):
        self.current_session[self.current_project] = name

    async def reset_active(self) -> None:
        s = self.sessions.pop((self.current_project, self.session_name()), None)
        if s is not None:
            await s.reset()
        self.forget_session_id(self.current_project, self.session_name())

    async def drop_session(self, name: str) -> bool:
        key = (self.current_project, name)
        s = self.sessions.pop(key, None)
        had_disk = self.known_session_id(self.current_project, name) is not None
        if s is not None:
            await s.reset()
        if had_disk:
            self.forget_session_id(self.current_project, name)
        if s is None and not had_disk:
            return False
        if self.current_session.get(self.current_project) == name:
            self.current_session[self.current_project] = DEFAULT_SESSION_NAME
        return True

    def resolve(self, token: str, allowed: bool) -> bool:
        fut = self.pending.get(token)
        if fut is None or fut.done():
            return False
        fut.set_result(allowed)
        return True


chats: dict[int, ChatState] = {}


def get_chat(chat_id: int, app: Application) -> ChatState:
    if chat_id not in chats:
        chats[chat_id] = ChatState(chat_id, app)
    return chats[chat_id]


def require_auth(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or chat.id not in ALLOWED_CHAT_IDS:
            log.warning("unauthorized chat=%s", chat.id if chat else None)
            return
        return await handler(update, context)

    return wrapped


@require_auth
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not update.message.text:
        return
    chat = get_chat(update.effective_chat.id, context.application)
    try:
        await chat.active().send(update.message.text)
    except Exception:
        log.exception("send failed")
        await update.message.reply_text("❌ error, see logs")


@require_auth
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = get_chat(update.effective_chat.id, context.application)
    await chat.reset_active()
    await update.message.reply_text(
        f"reset [{chat.current_project}/{chat.session_name()}]"
    )


@require_auth
async def cmd_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return
    chat = get_chat(update.effective_chat.id, context.application)
    if not chat.resolve(context.args[0], True):
        await update.message.reply_text("no such pending decision")


@require_auth
async def cmd_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return
    chat = get_chat(update.effective_chat.id, context.application)
    if not chat.resolve(context.args[0], False):
        await update.message.reply_text("no such pending decision")


@require_auth
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = get_chat(update.effective_chat.id, context.application)
    session = chat.active()
    await update.message.reply_text(
        f"chat_id={update.effective_chat.id}\n"
        f"project={chat.current_project}\n"
        f"session={chat.session_name()}\n"
        f"model={session.model or '(default)'}\n"
        f"cwd={chat.cwd()}"
    )


@require_auth
async def cmd_proj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("usage: /proj <name>")
        return
    name = context.args[0]
    merged = all_projects()
    if name not in merged:
        await update.message.reply_text(
            f"unknown project: {name}\n/projects to list"
        )
        return
    chat = get_chat(update.effective_chat.id, context.application)
    chat.switch_project(name)
    await update.message.reply_text(
        f"→ project {name}\nsession={chat.session_name()}\ncwd={chat.cwd()}"
    )


@require_auth
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    merged = all_projects()
    if not merged:
        await update.message.reply_text(
            "no projects, set PROJECTS in .env or /addproj <name> <path>"
        )
        return
    chat = get_chat(update.effective_chat.id, context.application)
    lines = []
    for n, p in merged.items():
        cur = "*" if n == chat.current_project else " "
        src = ".env" if n in _ENV_PROJECTS else "live"
        lines.append(f"{cur} {n} ({src}) → {p}")
    await update.message.reply_text("\n".join(lines))


@require_auth
async def cmd_addproj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("usage: /addproj <name> <path>")
        return
    name = context.args[0]
    if any(c in name for c in " ,="):
        await update.message.reply_text("name can't contain spaces, commas, or '='")
        return
    path_str = " ".join(context.args[1:]).strip().strip('"').strip("'")
    if name in all_projects():
        await update.message.reply_text(
            f"already exists: {name} → {all_projects()[name]}"
        )
        return
    p = Path(path_str).resolve()
    if not p.is_dir():
        await update.message.reply_text(f"not a directory: {p}")
        return
    file_p = load_file_projects()
    file_p[name] = p
    save_file_projects(file_p)
    await update.message.reply_text(f"+ {name} → {p}\n/proj {name} to switch")


@require_auth
async def cmd_rmproj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("usage: /rmproj <name>")
        return
    name = context.args[0]
    if name in _ENV_PROJECTS:
        await update.message.reply_text(
            f"{name} is defined in .env PROJECTS=; edit .env and restart to remove"
        )
        return
    file_p = load_file_projects()
    if name not in file_p:
        await update.message.reply_text(f"no such runtime project: {name}")
        return
    chat = get_chat(update.effective_chat.id, context.application)
    switched_back = False
    if chat.current_project == name:
        chat.switch_project(DEFAULT_PROJECT)
        switched_back = True
    del file_p[name]
    save_file_projects(file_p)
    msg = f"- {name}"
    if switched_back:
        msg += f"\n→ switched to default: {DEFAULT_PROJECT}"
    await update.message.reply_text(msg)


@require_auth
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = get_chat(update.effective_chat.id, context.application)
    if not context.args:
        await update.message.reply_text(
            f"current: [{chat.current_project}/{chat.session_name()}]\n"
            "/session <name> to switch, /sessions to list"
        )
        return
    name = context.args[0]
    chat.switch_session(name)
    await update.message.reply_text(f"→ [{chat.current_project}/{name}]")


@require_auth
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = get_chat(update.effective_chat.id, context.application)
    names, current = chat.list_sessions()
    on_disk = set(chat.known_sessions(chat.current_project))
    in_mem = {n for (p, n) in chat.sessions if p == chat.current_project}
    lines = []
    for n in names:
        cur = "*" if n == current else " "
        live = "●" if n in in_mem else "○"
        persist = "💾" if n in on_disk else "  "
        lines.append(f"{cur} {live} {persist} {n}")
    legend = "(* current  ● in-memory  💾 persisted)"
    await update.message.reply_text(
        f"[{chat.current_project}]\n" + "\n".join(lines) + f"\n{legend}"
    )


@require_auth
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = get_chat(update.effective_chat.id, context.application)
    session = chat.active()
    if not context.args:
        cur = session.model or "(default)"
        await update.message.reply_text(
            f"[{chat.current_project}/{chat.session_name()}] model: {cur}\n"
            "/model <name> to switch, /models to list"
        )
        return
    name = context.args[0]
    await session.switch_model(name)
    chat.remember_session(chat.current_project, chat.session_name(), model=name)
    await update.message.reply_text(
        f"→ [{chat.current_project}/{chat.session_name()}] model={name}"
    )


@require_auth
async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "known shortcuts:\n  " + "\n  ".join(KNOWN_MODELS)
        + "\n\n(any value accepted by the Claude CLI also works,\n"
        "e.g. claude-opus-4-7, claude-sonnet-4-6)"
    )


@require_auth
async def cmd_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("usage: /drop <name>")
        return
    name = context.args[0]
    chat = get_chat(update.effective_chat.id, context.application)
    ok = await chat.drop_session(name)
    if not ok:
        await update.message.reply_text(f"no such session: {name}")
        return
    await update.message.reply_text(
        f"dropped [{chat.current_project}/{name}]"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None:
        return
    await update.message.reply_text(f"chat_id={update.effective_chat.id}")


def cli_add(name: str | None, path: str | None) -> int:
    target = Path(path).resolve() if path else Path.cwd().resolve()
    alias = name or target.name
    if any(c in alias for c in " ,="):
        print(f"error: name can't contain spaces, commas, or '='", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 1
    if alias in _ENV_PROJECTS:
        print(
            f"error: '{alias}' is defined in .env PROJECTS=, edit .env to change",
            file=sys.stderr,
        )
        return 1
    file_p = load_file_projects()
    if alias in file_p:
        print(f"error: '{alias}' already registered → {file_p[alias]}", file=sys.stderr)
        return 1
    file_p[alias] = target
    save_file_projects(file_p)
    print(f"+ {alias} → {target}")
    return 0


def cli_rm(name: str) -> int:
    if name in _ENV_PROJECTS:
        print(
            f"error: '{name}' is in .env PROJECTS=, edit .env to remove",
            file=sys.stderr,
        )
        return 1
    file_p = load_file_projects()
    if name not in file_p:
        print(f"error: no such runtime project: {name}", file=sys.stderr)
        return 1
    del file_p[name]
    save_file_projects(file_p)
    print(f"- {name}")
    return 0


def cli_list() -> int:
    merged = all_projects()
    if not merged:
        print("(no projects)")
        return 0
    for n, p in merged.items():
        src = ".env" if n in _ENV_PROJECTS else "live"
        print(f"{n:20} ({src})  {p}")
    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="teleop",
        description="Telegram bridge for Claude Code. Run with no subcommand to start the bot.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser(
        "add", help="register a project (default: current directory)"
    )
    p_add.add_argument(
        "name",
        nargs="?",
        default=None,
        help="alias (default: basename of --path or current directory)",
    )
    p_add.add_argument(
        "--path", default=None, help="project path (default: current directory)"
    )

    p_rm = sub.add_parser("rm", help="remove a runtime project")
    p_rm.add_argument("name")

    sub.add_parser("list", help="list all projects (env + runtime)")

    args = parser.parse_args()

    if args.cmd == "add":
        sys.exit(cli_add(args.name, args.path))
    if args.cmd == "rm":
        sys.exit(cli_rm(args.name))
    if args.cmd == "list":
        sys.exit(cli_list())

    if not BOT_TOKEN:
        print(
            "error: TELEGRAM_BOT_TOKEN not set in .env",
            file=sys.stderr,
        )
        sys.exit(1)
    if not ALLOWED_CHAT_IDS:
        print(
            "warning: ALLOWED_CHAT_IDS is empty — nobody will be able to talk to the bot",
            file=sys.stderr,
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("yes", cmd_yes))
    app.add_handler(CommandHandler("no", cmd_no))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("proj", cmd_proj))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("addproj", cmd_addproj))
    app.add_handler(CommandHandler("rmproj", cmd_rmproj))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("drop", cmd_drop))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info(
        "bridge starting, default_project=%s default_cwd=%s projects=%s allowed=%s",
        DEFAULT_PROJECT,
        DEFAULT_CWD,
        list(all_projects().keys()),
        ALLOWED_CHAT_IDS,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
