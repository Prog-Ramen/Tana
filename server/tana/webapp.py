"""Internet-facing web app: chat with Tana or Claude, browse board conversations, control boards.

Runs on localhost behind Caddy (HTTPS). Every /api route except login requires a session
cookie obtained with password + TOTP. First-time setup lives on the LAN-only device port
(/setup), so credentials are never set up over the internet.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import re
import json
import logging
import secrets
import shutil
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pyotp
import qrcode
import qrcode.image.svg
from aiohttp import web
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from . import claude_code, harness_sessions, metrics
from .assistant import Unavailable
from .cc_runner import ClaudeCodeRunner
from .clients import ServiceDown
from .voice.speech import SentenceSplitter, clean_for_speech, strip_wake_phrase

if TYPE_CHECKING:
    from .app import Hub

log = logging.getLogger("tana.web")
STATIC = Path(__file__).parent / "static"
COOKIE = "tana_session"
ph = PasswordHasher()

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


# ------------------------------------------------------------------ auth helpers
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: web.Request) -> str:
    # X-Forwarded-For is only set by (and trusted from) the reverse proxy
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd and request.remote in request.app["cfg"]["web"]["trusted_proxies"]:
        return fwd.split(",")[-1].strip()
    return request.remote or "?"


def _from_networks(request: web.Request, nets: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(n) for n in nets)


def claude_code_allowed(request: web.Request) -> bool:
    if request.app["cc"] is None:
        return False
    return _from_networks(request, request.app["cfg"]["web"]["claude_code"]["allowed_networks"])


def rameness_allowed(request: web.Request) -> bool:
    if request.app["hub"].rameness is None:
        return False
    return _from_networks(request, request.app["cfg"]["web"]["rameness"]["allowed_networks"])


class Auth:
    WINDOW = 15 * 60
    PER_IP = 5
    GLOBAL = 30

    def __init__(self, hub: "Hub"):
        self.store = hub.store
        self.ttl = hub.cfg["web"]["session_days"] * 86400

    @property
    def configured(self) -> bool:
        return bool(self.store.get("auth.password") and self.store.get("auth.totp"))

    def locked_out(self, ip: str) -> bool:
        since = time.time() - self.WINDOW
        return (self.store.failures_since(since, ip) >= self.PER_IP
                or self.store.failures_since(since) >= self.GLOBAL)

    def check(self, password: str, code: str) -> bool:
        try:
            ph.verify(self.store.get("auth.password") or "", password)
        except (VerifyMismatchError, InvalidHashError):
            return False
        totp = pyotp.TOTP(self.store.get("auth.totp") or "")
        # accept the current step +/- 1, but never the same step twice (replay)
        last = int(self.store.get("auth.totp_last") or 0)
        now_step = int(time.time()) // 30
        for step in (now_step - 1, now_step, now_step + 1):
            if step > last and secrets.compare_digest(totp.at(step * 30), code.strip()):
                self.store.set("auth.totp_last", str(step))
                return True
        return False

    def session_ok(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE)
        return bool(token) and self.store.session_valid(_hash_token(token))


# ------------------------------------------------------------------ middleware
@web.middleware
async def security(request: web.Request, handler):
    app = request.app
    auth: Auth = app["auth"]
    path = request.path
    if request.remote not in app["cfg"]["web"]["trusted_proxies"]:
        raise web.HTTPForbidden(text="use the HTTPS address")
    if path == "/api/shortcut/say":
        # Siri Shortcut: authenticated by a revocable key (Authorization: Bearer ...), not the cookie
        resp = await handler(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp
    if request.method not in ("GET", "HEAD"):
        # CSRF: SameSite=Strict cookie + custom header (forces a CORS preflight we never allow)
        if request.headers.get("X-Tana") != "1":
            raise web.HTTPForbidden(text="missing X-Tana header")
        origin = app["cfg"]["web"]["origin"]
        if origin and request.headers.get("Origin") not in (origin, None):
            raise web.HTTPForbidden(text="bad origin")
    if path.startswith("/api/") and path not in ("/api/login", "/api/state"):
        if not auth.session_ok(request):
            raise web.HTTPUnauthorized(text="login required")
    resp = await handler(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ------------------------------------------------------------------ routes: auth
async def state(request: web.Request) -> web.Response:
    auth: Auth = request.app["auth"]
    hub: Hub = request.app["hub"]
    logged_in = auth.session_ok(request)
    rameness, boards = (await asyncio.gather(hub.reachable("rameness"), hub.reachable("boards"))) if logged_in \
        else (False, False)
    # only what is set up shows in the UI: a missing key, CLI or service hides its buttons and tabs
    return web.json_response({
        "configured": auth.configured,
        "logged_in": logged_in,
        "tana": hub.assistant is not None,
        "claude": hub.claude.available,
        "claude_code": claude_code_allowed(request) and shutil.which(hub.cfg["web"]["claude_code"]["claude_bin"]) is not None,
        "rameness": rameness,
        "rameness_actions": rameness and rameness_allowed(request),
        "boards": boards,
    })


async def login(request: web.Request) -> web.Response:
    auth: Auth = request.app["auth"]
    ip = client_ip(request)
    if not auth.configured:
        raise web.HTTPServiceUnavailable(text="not set up yet; finish setup from your home network")
    if auth.locked_out(ip):
        log.warning("login locked out for %s", ip)
        raise web.HTTPTooManyRequests(text="too many attempts; try again in 15 minutes")
    body = await request.json()
    ok = auth.check(str(body.get("password", "")), str(body.get("code", "")))
    metrics.inc("tana_logins_total", result="ok" if ok else "refused")
    if not ok:
        auth.store.record_failure(ip)
        log.warning("failed login from %s", ip)
        await asyncio.sleep(1)  # slow down guessing
        raise web.HTTPUnauthorized(text="wrong password or code")
    token = secrets.token_urlsafe(32)
    auth.store.add_session(_hash_token(token), auth.ttl, ip, request.headers.get("User-Agent", ""))
    log.info("login from %s", ip)
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=auth.ttl, httponly=True, secure=True,
                    samesite="Strict", path="/")
    return resp


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get(COOKIE)
    if token:
        request.app["auth"].store.delete_session(_hash_token(token))
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


# ------------------------------------------------------------------ routes: threads
def _thread_json(hub: "Hub", t: dict) -> dict:
    out = {k: t[k] for k in ("id", "title", "source", "brain", "created", "updated")}
    if t.get("device_id"):
        out["device"] = t["device_id"][-5:]
    if t["source"] == "claude-code":
        sid = t["id"].removeprefix("cc-")
        out["web_owned"] = hub.store.get(f"cc.web.{sid}") == "1"
        cwd = hub.store.get(f"cc.cwd.{sid}") or ""
        out["project"] = Path(cwd).name if cwd else ""
        out["automated"] = hub.store.get(f"cc.auto.{sid}") == "1"
    return out


async def sync_claude_code(hub: "Hub"):
    dirs = hub.cfg["web"]["claude_code_projects"]
    if dirs:
        await asyncio.get_running_loop().run_in_executor(None, claude_code.sync, hub.store, dirs)


async def list_threads(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    await sync_claude_code(hub)
    return web.json_response([_thread_json(hub, t) for t in hub.store.threads()])


async def create_thread(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    body = await request.json()
    brain = body.get("brain") if body.get("brain") in ("tana", "claude") else "tana"
    tid = hub.store.create_thread("web", brain)
    return web.json_response(_thread_json(hub, hub.store.thread(tid)))


def _get_thread(request: web.Request) -> dict:
    t = request.app["hub"].store.thread(request.match_info["id"])
    if t is None:
        raise web.HTTPNotFound()
    return t


async def get_thread(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    if request.match_info["id"].startswith("cc-"):
        await sync_claude_code(hub)
    t = _get_thread(request)
    return web.json_response({**_thread_json(hub, t), "messages": hub.store.messages(t["id"])})


async def update_thread(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    t = _get_thread(request)
    body = await request.json()
    fields = {}
    if body.get("brain") in ("tana", "claude"):
        fields["brain"] = body["brain"]
    if isinstance(body.get("title"), str):
        fields["title"] = body["title"][:120]
    hub.store.update_thread(t["id"], **fields)
    return web.json_response(_thread_json(hub, hub.store.thread(t["id"])))


async def delete_thread(request: web.Request) -> web.Response:
    t = _get_thread(request)
    if t["source"] == "claude-code":
        raise web.HTTPBadRequest(text="Claude Code sessions are mirrored from disk")
    request.app["hub"].store.delete_thread(t["id"])
    return web.json_response({"ok": True})


# ------------------------------------------------------------------ routes: chat
def _wav(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


async def send_message(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    t = _get_thread(request)
    if t["source"] != "web":
        raise web.HTTPBadRequest(text="board conversations are read-only")
    body = await request.json()
    text = str(body.get("text", "")).strip()[:8000]
    speak = bool(body.get("speak"))
    if not text:
        raise web.HTTPBadRequest(text="empty message")
    use_claude = t["brain"] == "claude"
    earlier = [{"role": m["role"], "content": m["content"]} for m in hub.store.messages(t["id"])]
    hub.store.add_message(t["id"], "user", text)
    history = earlier + [{"role": "user", "content": text}]

    async def from_backend():
        """Tana's reply, streamed from the configured backend (this thread is its chat "web-<thread id>")."""
        if hub.assistant is None:
            yield "(No assistant backend is configured on this server.)"
            return
        first = hub.store.get(f"assistant.chat.{t['id']}") is None
        instructions = hub.cfg["web"]["chat_instructions"]
        if first and earlier:
            # a conversation that began before this backend (an earlier backend, Claude): it gets what was said
            past = "\n".join(f"{m['role']}: {m['content'][:1500]}" for m in earlier[-20:])
            instructions += f"\n\nThis conversation began before you joined it. What was said so far:\n{past}"
        pieces: asyncio.Queue = asyncio.Queue()

        async def on_text(piece: str):
            await pieces.put(piece)

        async def no_tools(name: str, args: dict) -> str:
            return "This client has no tools."

        async def run():
            try:
                await hub.assistant.turn(f"web-{t['id']}", text, [], instructions, on_text, no_tools)
                hub.store.set(f"assistant.chat.{t['id']}", "1")
            except Unavailable as e:
                metrics.inc("tana_assistant_errors_total", backend=hub.assistant.name)
                await pieces.put(f"(Tana's assistant isn't available right now: {e})")
            finally:
                await pieces.put(None)
        task = asyncio.create_task(run())
        while (piece := await pieces.get()) is not None:
            yield piece
        await task

    name = "claude" if use_claude else "tana"
    stream = hub.claude.stream(history) if use_claude else from_backend()

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                       "X-Accel-Buffering": "no"})
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    await resp.prepare(request)
    events: asyncio.Queue = asyncio.Queue()

    async def writer():
        while True:
            ev = await events.get()
            if ev is None:
                return
            try:
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
            except (ConnectionResetError, RuntimeError):
                pass  # client went away; keep generating so the reply is still saved

    sentences: asyncio.Queue = asyncio.Queue()

    async def speaker():
        loop = asyncio.get_running_loop()
        while (sentence := await sentences.get()) is not None:
            clean = clean_for_speech(sentence)
            if not clean:
                continue
            ts = time.monotonic()
            audio, rate = await loop.run_in_executor(None, hub.tts.synthesize, clean)
            metrics.observe("tana_speech_seconds", time.monotonic() - ts)
            aid = secrets.token_urlsafe(12)
            hub.audio_cache[aid] = (_wav(audio, rate), time.time())
            await events.put({"t": "audio", "url": f"/api/audio/{aid}"})

    writer_task = asyncio.create_task(writer())
    speaker_task = asyncio.create_task(speaker()) if speak else None
    splitter = SentenceSplitter()
    full = ""
    t0, first = time.monotonic(), None
    try:
        async for delta in stream:
            if first is None and delta.strip():
                first = time.monotonic() - t0
                metrics.observe("tana_reply_first_text_seconds", first, brain=name)
            full += delta
            await events.put({"t": "delta", "text": delta})
            if speak:
                for s in splitter.push(delta):
                    await sentences.put(s)
    except Exception as e:
        log.exception("%s failed", name)
        msg = f"\n\n(Error from {name}: {e.__class__.__name__})"
        full += msg
        await events.put({"t": "delta", "text": msg})
    finally:
        if speaker_task:
            for s in splitter.flush():
                await sentences.put(s)
            await sentences.put(None)
            await speaker_task
        if full.strip():
            hub.store.add_message(t["id"], "assistant", full.strip(), name)
        metrics.inc("tana_chat_messages_total", brain=name)
        metrics.observe("tana_reply_seconds", time.monotonic() - t0, brain=name)
        await events.put({"t": "done"})
        await events.put(None)
        await writer_task
    return resp


async def audio(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    now = time.time()
    for k in [k for k, (_, ts) in hub.audio_cache.items() if now - ts > 600]:
        del hub.audio_cache[k]
    item = hub.audio_cache.get(request.match_info["id"])
    if item is None:
        raise web.HTTPNotFound()
    return web.Response(body=item[0], content_type="audio/wav")


async def transcribe(request: web.Request) -> web.Response:
    from faster_whisper import decode_audio

    hub: Hub = request.app["hub"]
    data = await request.read()
    if not data:
        raise web.HTTPBadRequest(text="no audio")
    loop = asyncio.get_running_loop()
    try:
        pcm = await loop.run_in_executor(None, lambda: decode_audio(io.BytesIO(data), sampling_rate=16000))
    except Exception:
        raise web.HTTPBadRequest(text="could not decode audio")
    t0 = time.monotonic()
    text = await loop.run_in_executor(None, hub.asr.transcribe, pcm.astype(np.float32))
    metrics.inc("tana_transcriptions_total")
    metrics.observe("tana_transcribe_seconds", time.monotonic() - t0)
    return web.json_response({"text": text})


async def claude_code_run(request: web.Request) -> web.StreamResponse:
    """Send a message to Claude Code (new session, or continue/fork an existing one)."""
    hub: Hub = request.app["hub"]
    runner: ClaudeCodeRunner | None = request.app["cc"]
    if not claude_code_allowed(request):
        log.warning("refused Claude Code run from %s (outside allowed networks)", client_ip(request))
        raise web.HTTPForbidden(text="Claude Code can only be run from an allowed network")
    body = await request.json()
    text = str(body.get("text", "")).strip()[:20000]
    speak = bool(body.get("speak"))
    thread = str(body.get("thread") or "")
    session_id = thread.removeprefix("cc-") if thread.startswith("cc-") else None
    if not text:
        raise web.HTTPBadRequest(text="empty message")
    metrics.inc("tana_claude_code_runs_total")
    log.warning("Claude Code run from %s (session %s): %r", client_ip(request), session_id or "new", text[:200])

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    await resp.prepare(request)
    events: asyncio.Queue = asyncio.Queue()

    async def writer():
        while (ev := await events.get()) is not None:
            try:
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode() if ev != "ping" else b": ping\n\n")
            except (ConnectionResetError, RuntimeError):
                pass

    async def keepalive():  # long tool runs are silent; keep proxies from timing out
        while True:
            await asyncio.sleep(15)
            await events.put("ping")

    sentences: asyncio.Queue = asyncio.Queue()

    async def speaker():
        loop = asyncio.get_running_loop()
        while (sentence := await sentences.get()) is not None:
            clean = clean_for_speech(sentence)
            if clean:
                audio, rate = await loop.run_in_executor(None, hub.tts.synthesize, clean)
                aid = secrets.token_urlsafe(12)
                hub.audio_cache[aid] = (_wav(audio, rate), time.time())
                await events.put({"t": "audio", "url": f"/api/audio/{aid}"})

    run_key = secrets.token_urlsafe(9)
    await events.put({"t": "run", "key": run_key})
    tasks = [asyncio.create_task(writer()), asyncio.create_task(keepalive())]
    speaker_task = asyncio.create_task(speaker()) if speak else None
    splitter = SentenceSplitter()
    try:
        cwd = hub.store.get(f"cc.cwd.{session_id}") if session_id else None
        if cwd and not Path(cwd).is_dir():
            raise RuntimeError(f"this session's folder no longer exists: {cwd}")
        async for ev in runner.run(session_id, text, run_key, cwd):
            if ev["t"] == "break":
                ev = {"t": "delta", "text": "\n\n"}
            await events.put(ev)
            if speak and ev["t"] == "delta":
                for s in splitter.push(ev["text"]):
                    await sentences.put(s)
    except Exception as e:
        log.exception("Claude Code run failed")
        await events.put({"t": "error", "text": f"{e.__class__.__name__}: {e}"})
    finally:
        if speaker_task:
            for s in splitter.flush():
                await sentences.put(s)
            await sentences.put(None)
            await speaker_task
        await sync_claude_code(hub)
        tasks[1].cancel()
        await events.put({"t": "done"})
        await events.put(None)
        await tasks[0]
    return resp


async def claude_code_stop(request: web.Request) -> web.Response:
    runner: ClaudeCodeRunner | None = request.app["cc"]
    if not claude_code_allowed(request):
        raise web.HTTPForbidden(text="Claude Code can only be run from an allowed network")
    body = await request.json()
    stopped = bool(runner) and await runner.stop(str(body.get("key", "")))
    return web.json_response({"stopped": stopped})


# ------------------------------------------------------------------ routes: Rameness (through its API)
ROUTES = {"delegate": "I'll have an agent do that and report back.",
          "decompose": "That's a bigger job: I'll split it between agents and report back.",
          "research": "I'll have an agent look into it and report back.",
          "clarify": "Can you tell me more about what you want?", "direct": "On it."}
STEPS = {"created", "status", "merged", "discarded", "closed", "fork", "stall", "warning", "error", "program",
         "cycle", "tool", "message"}


def _rm(request: web.Request):
    r = request.app["hub"].rameness
    if r is None:
        raise web.HTTPNotFound(text="Rameness is not configured")
    return r


def _rm_action(request: web.Request):
    r = _rm(request)
    if not rameness_allowed(request):
        log.warning("refused Rameness action from %s (outside allowed networks)", client_ip(request))
        raise web.HTTPForbidden(text="Rameness can only be directed from an allowed network")
    return r


async def _rm_call(coro):
    try:
        return await coro
    except ServiceDown as e:
        raise web.HTTPServiceUnavailable(text=f"Rameness: {e}")


def _agents(node: dict, out: list) -> list:
    out.append({k: node.get(k) for k in ("id", "parent", "role", "kind", "title", "status", "runtime", "slot",
                                         "branch", "updated")} | {"chat": (node.get("meta") or {}).get("chat"),
                                                                   "closed": bool((node.get("meta") or {}).get("closed"))})
    for c in node.get("children") or []:
        _agents(c, out)
    return out


async def rameness_overview(request: web.Request) -> web.Response:
    rm = _rm(request)
    chats, st = await _rm_call(asyncio.gather(rm.chats(), rm.state()))
    recent = time.time() - request.app["cfg"]["web"]["rameness"]["recent_hours"] * 3600
    agents = [a for a in _agents(st["tree"], []) if a["role"] == "manager"
              or a["status"] in ("queued", "starting", "running", "blocked", "paused") or (a["updated"] or 0) >= recent]
    return web.json_response({
        "chats": [c for c in chats if not c["id"].startswith("web-")],   # web chats are this app's own threads
        "agents": agents, "needs": len(st["escalations"]), "actions": rameness_allowed(request)})


def _need(e: dict, agents: dict) -> dict:
    opts = [o for o in e.get("options") or [] if o != "__triaged"]
    a = agents.get(e.get("agent")) or {}
    return {"id": e["id"], "agent": e.get("agent"), "who": a.get("title") or "the manager", "kind": e.get("kind"),
            "question": e["question"], "options": [o for o in opts if not o.startswith("<")],
            "free_text": any(o.startswith("<") for o in opts) or not opts, "chat": a.get("chat")}


async def rameness_chat(request: web.Request) -> web.Response:
    """A Rameness chat as a conversation: what was asked, the manager's replies, agents' results; the rest folds
    into steps."""
    rm, chat = _rm(request), request.match_info["chat"]
    feed, st = await _rm_call(asyncio.gather(rm.chat_events(chat), rm.state()))
    agents = {a["id"]: a for a in _agents(st["tree"], [])}
    msgs = []
    for e in feed["events"]:
        who = (agents.get(e["agent"]) or {}).get("title") or e["agent"]
        if e["type"] == "intake":
            route, _, text = e["message"].partition(": ")
            msgs.append({"role": "user", "content": text})
            if route in ROUTES:
                msgs.append({"role": "assistant", "content": ROUTES[route]})
        elif e["type"] == "reply":
            msgs.append({"role": "assistant", "content": e["message"]})
        elif e.get("result") is not None:
            done = "-> done" in e["message"].split(":")[0]
            head = f"**{e.get('title') or who}** {'finished' if done else 'failed'}."
            msgs.append({"role": "assistant", "content": head + (f"\n\n{e['result'].strip()[-4000:]}" if e["result"].strip() else "")})
        elif e["type"] in STEPS:
            msgs.append({"role": "tool", "content": f"{who}: {e['type']} {e['message']}".strip()})
    needs = [_need(e, agents) for e in st["escalations"] if (agents.get(e.get("agent")) or {}).get("chat") == chat]
    return web.json_response({"id": chat, "kind": "chat", "messages": msgs, "needs": needs})


async def rameness_turn(request: web.Request) -> web.Response:
    """Send a message into any Rameness chat (a board's, a CLI one); the reply comes back whole."""
    _rm_action(request)
    hub, chat = request.app["hub"], request.match_info["chat"]
    text = await _text(request)
    log.warning("Rameness message from %s to chat %s: %r", client_ip(request), chat, text[:200])
    out: list[str] = []

    async def on_text(piece: str):
        out.append(piece)

    async def no_tools(name: str, args: dict) -> str:
        return "This client has no tools."
    try:
        await hub.assistant.turn(chat, text, [], hub.cfg["web"]["chat_instructions"], on_text, no_tools)
    except Unavailable as e:
        raise web.HTTPServiceUnavailable(text=f"Rameness: {e}")
    return web.json_response({"text": "".join(out)})


async def rameness_agent(request: web.Request) -> web.Response:
    rm, aid = _rm(request), request.match_info["agent"]
    a, st = await _rm_call(asyncio.gather(rm.agent(aid), rm.state()))
    agents = {x["id"]: x for x in _agents(st["tree"], [])}
    msgs = [{"role": "user", "content": a.get("task") or ""}]
    msgs += [{"role": "tool", "content": f"{e['type']} {e['message']}".strip()} for e in a.get("events") or []
             if e["type"] in STEPS | {"turn", "plan"}]
    if a.get("status") in ("done", "failed") and (a.get("result") or "").strip():
        msgs.append({"role": "assistant", "content": a["result"].strip()[-4000:]})
    meta = a.get("meta") or {}
    return web.json_response({
        "id": aid, "kind": "agent", **{k: a.get(k) for k in ("title", "role", "status", "runtime", "slot", "env", "branch")},
        "closed": bool(meta.get("closed")), "chat": meta.get("chat"),
        "parent_title": (agents.get(a.get("parent")) or {}).get("title"),
        "messages": msgs, "log": a.get("log") or "",
        "needs": [_need(e, agents) for e in st["escalations"] if e.get("agent") == aid]})


async def _text(request: web.Request, key: str = "text") -> str:
    text = str((await request.json()).get(key, "")).strip()[:20000]
    if not text:
        raise web.HTTPBadRequest(text="empty message")
    return text


async def rameness_answer(request: web.Request) -> web.Response:
    rm = _rm_action(request)
    answer = await _text(request, "answer")
    log.warning("Rameness answer from %s to %s: %r", client_ip(request), request.match_info["id"], answer[:200])
    return web.json_response(await _rm_call(rm.answer(request.match_info["id"], answer)))


async def rameness_prompt(request: web.Request) -> web.Response:
    rm = _rm_action(request)
    text = await _text(request)
    log.warning("Rameness message from %s to agent %s: %r", client_ip(request), request.match_info["agent"], text[:200])
    return web.json_response(await _rm_call(rm.prompt(request.match_info["agent"], text)))


async def rameness_retire(request: web.Request) -> web.Response:
    rm = _rm_action(request)
    log.warning("Rameness retire from %s: %s", client_ip(request), request.match_info["agent"])
    return web.json_response(await _rm_call(rm.retire(request.match_info["agent"])))


# ------------------------------------------------------------------ routes: other harnesses' sessions (read-only)
async def harnesses(request: web.Request) -> web.Response:
    """Codex, pi and dsh sessions on this machine; only harnesses that have sessions are listed."""
    found = await asyncio.get_running_loop().run_in_executor(None, harness_sessions.available)
    return web.json_response({name: {"label": harness_sessions.SOURCES[name].label, "sessions": sessions}
                              for name, sessions in found.items()})


async def harness_session(request: web.Request) -> web.Response:
    src = harness_sessions.SOURCES.get(request.match_info["harness"])
    if src is None:
        raise web.HTTPNotFound()
    try:
        msgs = await asyncio.get_running_loop().run_in_executor(None, src.read, request.match_info["id"])
    except KeyError:
        raise web.HTTPNotFound()
    return web.json_response({"harness": src.name, "label": src.label, "messages": msgs})


# ------------------------------------------------------------------ routes: phone as microphone
class Clients:
    """Open browser tabs, connected over /api/events. A 'display' (e.g. the car) runs the chat;
    a 'mic' (e.g. a phone) records speech and sends the text to a chosen display."""

    def __init__(self):
        self.clients: dict[str, dict] = {}

    def displays(self) -> list[dict]:
        return [{"id": cid, "name": c["name"], "busy": c["busy"]}
                for cid, c in self.clients.items() if c["role"] == "display"]

    async def send(self, cid: str, ev: dict) -> bool:
        c = self.clients.get(cid)
        if c is None:
            return False
        await c["queue"].put(ev)
        return True

    async def to_mics(self, ev: dict):
        for cid, c in list(self.clients.items()):
            if c["role"] == "mic":
                await c["queue"].put(ev)


def _client_name(request: web.Request) -> str:
    ua = request.headers.get("User-Agent", "")
    if "Tesla" in ua:
        return "Tesla"
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS", "Mac"),
                      ("Windows", "Windows PC"), ("Linux", "Linux")):
        if key in ua:
            return name
    return "Browser"


async def events(request: web.Request) -> web.StreamResponse:
    clients: Clients = request.app["clients"]
    cid = request.query.get("id", "")[:40] or secrets.token_urlsafe(8)
    role = "mic" if request.query.get("role") == "mic" else "display"
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    await resp.prepare(request)
    queue: asyncio.Queue = asyncio.Queue()
    name = request.query.get("name", "").strip()[:40] or _client_name(request)
    clients.clients[cid] = {"role": role, "name": name, "busy": False, "queue": queue}
    await clients.to_mics({"t": "displays", "displays": clients.displays()})
    try:
        if role == "mic":
            await resp.write(f"data: {json.dumps({'t': 'displays', 'displays': clients.displays()})}\n\n".encode())
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), 15)
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        if clients.clients.get(cid, {}).get("queue") is queue:
            del clients.clients[cid]
            await clients.to_mics({"t": "displays", "displays": clients.displays()})
    return resp


async def remote_status(request: web.Request) -> web.Response:
    """A display reports whether it is busy (answering or speaking), so mics can pause."""
    clients: Clients = request.app["clients"]
    body = await request.json()
    c = clients.clients.get(str(body.get("id", "")))
    if c and c["role"] == "display":
        c["busy"] = bool(body.get("busy"))
        await clients.to_mics({"t": "displays", "displays": clients.displays()})
    return web.json_response({"ok": True})


async def remote_text(request: web.Request) -> web.Response:
    """A mic sends text (typed or dictated) to a display."""
    clients: Clients = request.app["clients"]
    body = await request.json()
    text = str(body.get("text", "")).strip()[:8000]
    if not text:
        raise web.HTTPBadRequest(text="empty")
    if not await clients.send(str(body.get("display", "")), {"t": "remote_text", "text": text}):
        raise web.HTTPNotFound(text="that screen is no longer connected")
    return web.json_response({"text": text})


async def remote_audio(request: web.Request) -> web.Response:
    """A mic sends a recording; it is transcribed here and the text goes to the display.
    With ?wake=1 the text is only forwarded if it starts with the wake phrase ("Hey Tana, ..."),
    which is stripped; a bare wake phrase returns woke=true so the phone listens for the request."""
    from faster_whisper import decode_audio

    hub: Hub = request.app["hub"]
    clients: Clients = request.app["clients"]
    display = request.query.get("display", "")
    if display not in clients.clients:
        raise web.HTTPNotFound(text="that screen is no longer connected")
    data = await request.read()
    loop = asyncio.get_running_loop()
    try:
        pcm = await loop.run_in_executor(None, lambda: decode_audio(io.BytesIO(data), sampling_rate=16000))
    except Exception:
        raise web.HTTPBadRequest(text="could not decode audio")
    text = (await loop.run_in_executor(None, hub.asr.transcribe, pcm.astype(np.float32))).strip()
    heard = text
    woke = False
    if request.query.get("wake") == "1" and text:
        wcfg = hub.cfg["wake"]
        rest = strip_wake_phrase(text, wcfg["names"], wcfg["search_words"])
        if rest is None:
            return web.json_response({"text": "", "heard": heard, "woke": False, "sent": False})
        woke, text = True, rest
    sent = bool(text) and any(ch.isalnum() for ch in text)
    if sent:
        await clients.send(display, {"t": "remote_text", "text": text})
    return web.json_response({"text": text, "heard": heard, "woke": woke, "sent": sent})


# ------------------------------------------------------------------ routes: Siri Shortcut keys
def _keys(store) -> list[dict]:
    return json.loads(store.get("shortcut.keys") or "[]")


def _save_keys(store, keys: list[dict]):
    store.set("shortcut.keys", json.dumps(keys))


async def list_keys(request: web.Request) -> web.Response:
    keys = _keys(request.app["hub"].store)
    return web.json_response([{k: key[k] for k in ("id", "name", "created", "last_used")} for key in keys])


async def create_key(request: web.Request) -> web.Response:
    store = request.app["hub"].store
    body = await request.json()
    token = "tana_" + secrets.token_urlsafe(32)
    key = {"id": secrets.token_hex(4), "name": str(body.get("name") or "Siri Shortcut")[:40],
           "hash": _hash_token(token), "created": time.time(), "last_used": None}
    _save_keys(store, _keys(store) + [key])
    log.info("created shortcut key %s (%s)", key["id"], key["name"])
    return web.json_response({"id": key["id"], "name": key["name"], "token": token})  # shown once


async def delete_key(request: web.Request) -> web.Response:
    store = request.app["hub"].store
    _save_keys(store, [k for k in _keys(store) if k["id"] != request.match_info["id"]])
    return web.json_response({"ok": True})


async def shortcut_say(request: web.Request) -> web.Response:
    """POST {"text": "..."} with Authorization: Bearer <key>.
    Sends the text to an open screen (preferring one named like a car), or, if none is open,
    asks Tana directly. Returns {"speak": "..."} for the Shortcut to read out."""
    hub: Hub = request.app["hub"]
    auth: Auth = request.app["auth"]
    clients: Clients = request.app["clients"]
    ip = client_ip(request)
    if auth.locked_out(ip):
        raise web.HTTPTooManyRequests(text="too many attempts")
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    keys = _keys(hub.store)
    match = next((k for k in keys if given and secrets.compare_digest(k["hash"], _hash_token(given))), None)
    if match is None:
        metrics.inc("tana_shortcut_requests_total", result="bad key")
        hub.store.record_failure(ip)
        log.warning("bad shortcut key from %s", ip)
        raise web.HTTPUnauthorized(text="bad key")
    match["last_used"] = time.time()
    _save_keys(hub.store, keys)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str(body.get("text", "")).strip()[:4000]
    if not text:
        return web.json_response({"speak": "I didn't hear anything."})

    displays = clients.displays()
    if displays:
        target = next((d for d in displays if re.search(r"tesla|car", d["name"], re.I)), displays[-1])
        await clients.send(target["id"], {"t": "remote_text", "text": text, "from": "shortcut"})
        log.info("shortcut key %s -> screen %s", match["id"], target["name"])
        return web.json_response({"speak": "", "sent_to": target["name"]})

    # No screen open: Rameness answers and Siri speaks it (one chat, "siri", for every Shortcut question)
    tid = hub.store.get("shortcut.thread")
    if not tid or not hub.store.thread(tid):
        tid = hub.store.create_thread("web", "tana", title="Siri")
        hub.store.set("shortcut.thread", tid)
    hub.store.add_message(tid, "user", text)
    if hub.assistant is None:
        return web.json_response({"speak": "No assistant is configured on this server."})
    reply: list[str] = []

    async def on_text(piece: str):
        reply.append(piece)

    async def no_tools(name: str, args: dict) -> str:
        return "This client has no tools."
    try:
        await hub.assistant.turn(f"web-{tid}", text, [], hub.cfg["web"]["spoken_instructions"], on_text, no_tools)
    except Unavailable:
        metrics.inc("tana_shortcut_requests_total", result="assistant unavailable")
        return web.json_response({"speak": "My AI isn't available right now."})
    metrics.inc("tana_shortcut_requests_total", result="answered")
    said = "".join(reply).strip()
    hub.store.add_message(tid, "assistant", said, "tana")
    return web.json_response({"speak": clean_for_speech(said)})


# ------------------------------------------------------------------ routes: boards, rooms & lights (tana-boards)
def _boards(request: web.Request):
    b = request.app["hub"].boards
    if b is None:
        raise web.HTTPNotFound(text="tana-boards is not configured")
    return b


async def rooms(request: web.Request) -> web.Response:
    try:
        return web.json_response(await _boards(request).rooms())
    except ServiceDown as e:
        raise web.HTTPServiceUnavailable(text=f"tana-boards: {e}")


async def devices(request: web.Request) -> web.Response:
    try:
        return web.json_response(await _boards(request).boards())
    except ServiceDown as e:
        raise web.HTTPServiceUnavailable(text=f"tana-boards: {e}")


async def set_device(request: web.Request) -> web.Response:
    body = await request.json()
    values = {k: body[k] for k in ("volume", "brightness") if k in body}
    try:
        return web.json_response(await _boards(request).set_board(request.match_info["id"], values))
    except ServiceDown as e:
        raise web.HTTPServiceUnavailable(text=f"tana-boards: {e}")


# ------------------------------------------------------------------ static
def _asset_version() -> str:
    h = hashlib.sha256()
    for name in ("app.js", "app.css"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:10]


async def index(request: web.Request) -> web.Response:
    # Version the asset URLs so browsers (the car's especially) never run stale code after an update
    v = _asset_version()
    html = (STATIC / "index.html").read_text()
    html = html.replace("/static/app.js", f"/static/app.js?v={v}").replace("/static/app.css", f"/static/app.css?v={v}")
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


def build_web_app(hub: "Hub") -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=12 * 1024 * 1024)
    app["hub"] = hub
    app["cfg"] = hub.cfg
    app["auth"] = Auth(hub)
    app["clients"] = Clients()
    cc_cfg = hub.cfg["web"]["claude_code"]
    app["cc"] = ClaudeCodeRunner(cc_cfg, hub.store) if cc_cfg["enabled"] else None
    r = app.router
    r.add_get("/", index)
    r.add_get("/mic", index)  # same app; opens straight into microphone mode
    r.add_static("/static/", STATIC, append_version=False)
    r.add_get("/api/state", state)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_get("/api/threads", list_threads)
    r.add_post("/api/threads", create_thread)
    r.add_get("/api/threads/{id}", get_thread)
    r.add_patch("/api/threads/{id}", update_thread)
    r.add_delete("/api/threads/{id}", delete_thread)
    r.add_post("/api/threads/{id}/messages", send_message)
    r.add_get("/api/audio/{id}", audio)
    r.add_post("/api/transcribe", transcribe)
    r.add_post("/api/claude-code/run", claude_code_run)
    r.add_post("/api/claude-code/stop", claude_code_stop)
    r.add_get("/api/rameness", rameness_overview)
    r.add_get("/api/rameness/chats/{chat}", rameness_chat)
    r.add_post("/api/rameness/chats/{chat}/turn", rameness_turn)
    r.add_get("/api/rameness/agents/{agent}", rameness_agent)
    r.add_post("/api/rameness/needs/{id}", rameness_answer)
    r.add_post("/api/rameness/agents/{agent}/prompt", rameness_prompt)
    r.add_post("/api/rameness/agents/{agent}/retire", rameness_retire)
    r.add_get("/api/harnesses", harnesses)
    r.add_get("/api/harnesses/{harness}/{id}", harness_session)
    r.add_get("/api/keys", list_keys)
    r.add_post("/api/keys", create_key)
    r.add_delete("/api/keys/{id}", delete_key)
    r.add_post("/api/shortcut/say", shortcut_say)
    r.add_get("/api/events", events)
    r.add_post("/api/remote/status", remote_status)
    r.add_post("/api/remote/text", remote_text)
    r.add_post("/api/remote/audio", remote_audio)
    r.add_get("/api/rooms", rooms)
    r.add_get("/api/devices", devices)
    r.add_post("/api/devices/{id}", set_device)
    return app


async def metrics_page(request: web.Request) -> web.Response:
    """GET /metrics (local port only): this process's counters plus Tana's current state."""
    hub = request.app["hub"]
    rameness, boards = await asyncio.gather(hub.reachable("rameness"), hub.reachable("boards"))
    db = hub.store.db
    sessions = db.execute("SELECT COUNT(*) FROM web_sessions WHERE expires > ?", (time.time(),)).fetchone()[0]
    threads = [({"source": src, "brain": brain}, n) for src, brain, n in
               db.execute("SELECT source, brain, COUNT(*) FROM threads GROUP BY source, brain")]
    text = metrics.render([
        ("tana_up", "1 while the web app runs, with its assistant backend.",
         [({"backend": hub.assistant.name if hub.assistant else "none"}, 1)]),
        ("tana_service_up", "Whether a service Tana uses answers (cached 30 s).",
         [({"service": "rameness"}, int(rameness)), ({"service": "boards"}, int(boards))]),
        ("tana_web_sessions", "Signed-in browser sessions.", [({}, sessions)]),
        ("tana_threads", "Conversations Tana keeps, by source and who answers them.", threads),
    ])
    return web.Response(text=text, content_type="text/plain", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


def build_local_app(hub: "Hub") -> web.Application:
    """First-time login setup and /metrics, on their own port that is never forwarded (local network only)."""
    app = web.Application()
    app["hub"] = hub
    app.router.add_get("/setup", setup_page)
    app.router.add_post("/setup", setup_submit)
    app.router.add_get("/metrics", metrics_page)
    return app


# ------------------------------------------------------------------ LAN-only first-time setup
SETUP_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Tana setup</title>
<style>body{{font:16px system-ui;max-width:420px;margin:40px auto;padding:0 16px;background:#111;color:#eee}}
input,button{{font:inherit;width:100%;padding:10px;margin:6px 0;box-sizing:border-box;border-radius:8px;border:1px solid #444;background:#222;color:#eee}}
button{{background:#4c7cf0;border:0}} .qr{{background:#fff;padding:12px;border-radius:8px;width:220px}}
code{{word-break:break-all}} .err{{color:#f77}}</style></head><body>
<h1>Tana setup</h1>{body}</body></html>"""


def _qr_svg(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return img.to_string(encoding="unicode")


async def setup_page(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    if hub.store.get("auth.password"):
        body = "<p>Already set up. To reset, run <code>tana-admin reset-auth</code> on the server.</p>"
        return web.Response(text=SETUP_PAGE.format(body=body), content_type="text/html")
    secret = hub.pending_totp or pyotp.random_base32()
    hub.pending_totp = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name="Tana", issuer_name="Tana")
    err = request.query.get("err", "")
    body = f"""<p>1. Scan this with your authenticator app (Google Authenticator, 1Password, Authy…):</p>
<div class="qr">{_qr_svg(uri)}</div><p>or enter the key: <code>{secret}</code></p>
<form method="post" action="/setup">
<p>2. Choose a password (at least 12 characters):</p>
<input type="password" name="password" autocomplete="new-password" required minlength="12" placeholder="Password">
<input type="password" name="password2" autocomplete="new-password" required placeholder="Repeat password">
<p>3. Enter the 6-digit code from the app:</p>
<input name="code" inputmode="numeric" autocomplete="one-time-code" required pattern="[0-9]{{6}}" placeholder="123456">
<p class="err">{err}</p><button>Save</button></form>"""
    return web.Response(text=SETUP_PAGE.format(body=body), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def setup_submit(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    if hub.store.get("auth.password"):
        raise web.HTTPForbidden(text="already set up")
    form = await request.post()
    pw, pw2, code = str(form.get("password", "")), str(form.get("password2", "")), str(form.get("code", ""))
    secret = hub.pending_totp
    if len(pw) < 12:
        raise web.HTTPFound("/setup?err=Password+must+be+at+least+12+characters")
    if pw != pw2:
        raise web.HTTPFound("/setup?err=Passwords+do+not+match")
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        raise web.HTTPFound("/setup?err=That+code+did+not+match;+check+the+app+and+try+again")
    hub.store.set("auth.password", ph.hash(pw))
    hub.store.set("auth.totp", secret)
    hub.pending_totp = None
    log.info("web login configured from %s", request.remote)
    body = "<p>Done. You can now log in to the web app.</p>"
    return web.Response(text=SETUP_PAGE.format(body=body), content_type="text/html")
