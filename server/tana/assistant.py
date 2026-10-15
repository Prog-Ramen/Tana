"""Tana's assistant backend: what answers Tana's chats (web, boards, the Siri Shortcut).

The backend is chosen by config (`assistant.backend`) and is swappable: every backend implements the same
`turn()` (a message in, the reply streamed out through on_text, the client's tools called back through run_tool),
plus `events()` and `condense()` where it can. Backends:

  rameness     a Rameness manager's chat-turn API (`assistant.url`), described below; the only one that can
               call the client's tools (a board's volume, home controls) and announce work that finishes later
  claude-code  Claude Code (`claude -p`), one session per chat
  codex        Codex (`codex exec --json`), one thread per chat
  pi           pi (`pi -p --mode json`), one session per chat
  dsh          the DeepSeek Harness (`dsh --profile headless`), the adapter keeping each chat's recent exchanges

To add one, subclass Backend (or CliBackend for a command-line agent), implement turn(), register it in BACKENDS.

The chat-turn API (rameness):
A turn is POST <url>/api/chats/<chat>/turn with {"text", "tools", "instructions"}; the reply streams back as
server-sent events: {"t": "text"} pieces, {"t": "tool_call"} (we run the tool and POST the result to
<url>/api/turns/<turn>/results while the stream waits), {"t": "agents"} when the work was handed off, and
{"t": "done"}. Work that finishes later shows up in the chat's feed, <url>/api/chats/<chat>/events.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("tana.assistant")


class Unavailable(Exception):
    """The assistant could not be reached (not running, or it failed before replying)."""


class Backend:
    """What Tana needs from an assistant. A chat id keeps a conversation's history on the backend's side."""

    name = "?"

    async def turn(self, chat: str, text: str, tools: list[dict], instructions: str,
                   on_text: Callable[[str], Awaitable[None]],
                   run_tool: Callable[[str, dict], Awaitable[str]]) -> dict:
        raise NotImplementedError

    async def events(self, chat: str, since: int) -> dict:
        """Work that finished later in a chat (for announcements); backends without it report nothing."""
        return {"events": [], "last": since}

    async def condense(self, text: str, length: str = "concise") -> str:
        return text

    async def close(self):
        pass


class TurnApi(Backend):
    """A Rameness manager (or anything else speaking the chat-turn API)."""

    name = "rameness"

    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/")
        self.timeout = cfg.get("timeout", 300)
        self.http: aiohttp.ClientSession | None = None

    def _session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=5,
                                                                            sock_read=self.timeout))
        return self.http

    async def close(self):
        if self.http and not self.http.closed:
            await self.http.close()

    async def turn(self, chat: str, text: str, tools: list[dict], instructions: str,
                   on_text: Callable[[str], Awaitable[None]],
                   run_tool: Callable[[str, dict], Awaitable[str]]) -> dict:
        """Send a message; stream the reply into on_text and run the tools it calls. Returns the "done" event,
        with "route" and "agents" added."""
        http = self._session()
        body = {"text": text, "tools": tools, "instructions": instructions}
        out: dict = {"text": "", "route": None, "agents": []}
        try:
            async with http.post(f"{self.url}/api/chats/{chat}/turn", json=body) as r:
                if r.status != 200:
                    raise Unavailable(f"turn returned {r.status}: {(await r.text())[:200]}")
                buf = b""
                async for chunk in r.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        line, buf = buf.split(b"\n\n", 1)
                        if not line.startswith(b"data: "):
                            continue
                        ev = json.loads(line[6:])
                        t = ev.get("t")
                        if t == "text":
                            await on_text(ev["text"])
                        elif t == "route":
                            out["route"] = ev["route"]
                        elif t == "agents":
                            out["agents"] = ev["agents"]
                        elif t == "tool_call":
                            result = await run_tool(ev["name"], ev.get("input") or {})
                            async with http.post(f"{self.url}/api/turns/{ev['turn']}/results",
                                                 json={"id": ev["id"], "content": result}) as rr:
                                await rr.read()
                        elif t == "error":
                            raise Unavailable(ev.get("error", "assistant error"))
                        elif t == "done":
                            out["text"] = ev.get("text", "")
                            return out
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            raise Unavailable(f"{type(e).__name__}: {e}") from None
        raise Unavailable("the reply ended early")

    async def events(self, chat: str, since: int) -> dict:
        """The chat's feed since an event id: {"events": [...], "last": id}. Finished work carries "result"."""
        try:
            async with self._session().get(f"{self.url}/api/chats/{chat}/events", params={"since": since}) as r:
                return await r.json()
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            raise Unavailable(f"{type(e).__name__}: {e}") from None

    async def condense(self, text: str, length: str = "concise") -> str:
        try:
            async with self._session().post(f"{self.url}/api/condense", json={"text": text, "length": length}) as r:
                return (await r.json()).get("text") or text
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            raise Unavailable(f"{type(e).__name__}: {e}") from None


Assistant = TurnApi       # the name the rest of the code used before backends were swappable


class CliBackend(Backend):
    """A coding-agent CLI run once per message, each Tana chat continuing one of its sessions.

    Settings (assistant.*): bin (the program), args (extra arguments), workdir (where it runs; default
    ~/.tana/assistant/work), state_dir (chat -> session ids; default ~/.tana/assistant), timeout (seconds).
    These CLIs run their own tools, so the client's tools (a board's volume, home controls) are not offered."""

    default_bin = ""

    def __init__(self, cfg: dict):
        import shutil
        from pathlib import Path
        self.cfg = cfg
        self.bin = cfg.get("bin") or shutil.which(self.default_bin) or self.default_bin
        self.args = list(cfg.get("args") or self.default_args())
        state = Path(cfg.get("state_dir") or Path.home() / ".tana" / "assistant").expanduser()
        self.workdir = Path(cfg.get("workdir") or state / "work").expanduser()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_file = state / f"{self.name}-sessions.json"
        self.timeout = cfg.get("timeout", 600)

    def default_args(self) -> list[str]:
        return []

    def _sessions(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, chat: str, value) -> None:
        s = self._sessions()
        s[chat] = value
        self.state_file.write_text(json.dumps(s))

    def argv(self, chat: str, text: str, instructions: str, session) -> list[str]:
        raise NotImplementedError

    async def handle(self, ev: dict, chat: str, on_text) -> None:
        """One JSON line of the CLI's output."""

    async def turn(self, chat, text, tools, instructions, on_text, run_tool) -> dict:
        import asyncio
        session = self._sessions().get(chat)
        argv = self.argv(chat, text, instructions, session)
        self._said: list[str] = []

        async def say(piece: str):
            self._said.append(piece)
            await on_text(piece)
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=str(self.workdir), stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                        limit=16 * 1024 * 1024)
        except OSError as e:
            raise Unavailable(f"{self.name}: {e}") from None
        proc.stdin.write(self.stdin(text, instructions, session).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def read():
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    await self.plain(line, say)
                    continue
                await self.handle(ev, chat, say)
            return await proc.wait()
        try:
            code = await asyncio.wait_for(read(), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise Unavailable(f"{self.name}: no answer within {self.timeout} s") from None
        if code != 0 and not self._said:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            raise Unavailable(f"{self.name} exited {code}: {err[-300:]}")
        self.finished(chat, text, "".join(self._said))
        return {"text": "".join(self._said), "route": self.name, "agents": []}

    def stdin(self, text: str, instructions: str, session) -> str:
        return ""

    async def plain(self, line: str, say) -> None:
        """A line that is not JSON (CLIs that print plain text)."""

    def finished(self, chat: str, text: str, reply: str) -> None:
        pass


class ClaudeCode(CliBackend):
    """Claude Code (`claude -p`): the chat's first message sets a session id, later ones resume it."""

    name, default_bin = "claude-code", "claude"

    def default_args(self):
        return ["--permission-mode", "acceptEdits"]

    def argv(self, chat, text, instructions, session):
        import uuid
        argv = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", *self.args]
        if session:
            argv += ["--resume", session]
        else:
            session = str(uuid.uuid4())
            self._remember(chat, session)
            argv += ["--session-id", session]
        return argv + (["--append-system-prompt", instructions] if instructions else [])

    def stdin(self, text, instructions, session):
        return text

    async def handle(self, ev, chat, say):
        if ev.get("type") == "stream_event":
            inner = ev.get("event") or {}
            delta = inner.get("delta") or {}
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                await say(delta.get("text", ""))
            elif inner.get("type") == "message_stop" and self._said and not self._said[-1].endswith("\n"):
                await say("\n\n")                 # text before and after a tool call: separate paragraphs


class Codex(CliBackend):
    """Codex (`codex exec --json`): the first message starts a thread, later ones resume it by id."""

    name, default_bin = "codex", "codex"

    def default_args(self):
        return ["--sandbox", "workspace-write"]

    def argv(self, chat, text, instructions, session):
        if session:
            return [self.bin, "exec", "resume", session, "--json", "--skip-git-repo-check", text]
        prompt = f"{instructions}\n\n{text}" if instructions else text
        return [self.bin, "exec", "--json", "--skip-git-repo-check", *self.args, prompt]

    async def handle(self, ev, chat, say):
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            self._remember(chat, ev["thread_id"])
        elif ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            await say(((ev["item"].get("text") or "") + "\n\n") if self._said else (ev["item"].get("text") or ""))


class Pi(CliBackend):
    """pi (`pi -p --mode json`): one session id per chat, created on the first message."""

    name, default_bin = "pi", "pi"

    def argv(self, chat, text, instructions, session):
        import uuid
        if not session:
            session = str(uuid.uuid4())
            self._remember(chat, session)
        argv = [self.bin, "-p", "--mode", "json", "--session-id", session, *self.args]
        return argv + (["--append-system-prompt", instructions] if instructions else []) + [text]

    async def handle(self, ev, chat, say):
        e = ev.get("assistantMessageEvent") or {}
        if ev.get("type") == "message_update" and e.get("type") == "text_delta":
            await say(e.get("delta", ""))


class Dsh(CliBackend):
    """dsh, the DeepSeek Harness (`dsh --profile headless`): answers one task and exits, with no session to
    resume, so the adapter keeps each chat's recent exchanges and sends them along (settings: profile)."""

    name, default_bin = "dsh", "dsh"
    KEEP = 10                                  # exchanges sent along with each message

    def argv(self, chat, text, instructions, session):
        past = "\n".join(f"User: {u}\nAssistant: {a}" for u, a in (session or [])[-self.KEEP:])
        prompt = "\n\n".join(x for x in (instructions, f"The conversation so far:\n{past}" if past else "",
                                          f"User: {text}") if x)
        return [self.bin, "--profile", self.cfg.get("profile") or "headless", *self.args, prompt]

    async def plain(self, line, say):
        await say(line + "\n")

    def finished(self, chat, text, reply):
        history = (self._sessions().get(chat) or [])[-(self.KEEP - 1):]
        self._remember(chat, history + [[text, reply.strip()]])


BACKENDS = {"rameness": TurnApi, "claude-code": ClaudeCode, "codex": Codex, "pi": Pi, "dsh": Dsh}


def make_backend(cfg: dict) -> Backend | None:
    """The configured assistant backend (`assistant.backend`); None when not configured."""
    if not cfg or (cfg.get("backend", "rameness") == "rameness" and not cfg.get("url")):
        return None
    kind = cfg.get("backend") or "rameness"
    if kind not in BACKENDS:
        raise ValueError(f"unknown assistant backend {kind!r}; known: {', '.join(BACKENDS)}")
    return BACKENDS[kind](cfg)
