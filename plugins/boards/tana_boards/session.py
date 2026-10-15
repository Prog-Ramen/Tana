"""One WebSocket voice session with a Tana device (xiaozhi protocol v1)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import soxr
from aiohttp import WSMsgType, web

from tana.voice.audio import FRAME_MS, OUT_RATE, OpusIn, OpusOut, VadStream
from tana.voice.speech import SentenceSplitter, clean_for_speech, first_emotion, strip_wake_phrase

from . import home
from tana import metrics
from tana.assistant import Unavailable

if TYPE_CHECKING:
    from .server import Hub

log = logging.getLogger("tana.boards.session")

PREBUFFER_FRAMES = 5  # frames sent ahead of real time so playback never starves
LOCAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "end_conversation",
            "description": "End the voice conversation after your reply, e.g. when the user says goodbye.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _tool_name(mcp_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", mcp_name)


class Session:
    def __init__(self, hub: "Hub", ws: web.WebSocketResponse, device_id: str, client_id: str):
        self.hub = hub
        self.ws = ws
        self.device_id = device_id
        self.client_id = client_id
        self.session_id = str(uuid.uuid4())
        self.opus_in: OpusIn | None = None
        self.opus_out = OpusOut()
        vcfg = hub.cfg["vad"]
        self.vad = VadStream(hub.vad, vcfg["threshold"], vcfg["silence_ms"], vcfg["min_speech_ms"],
                             vcfg["max_speech_s"])
        self.manual_audio: list[np.ndarray] = []
        self.listening = False
        self.mode = "auto"
        self.turn: asyncio.Task | None = None
        self.last_activity = time.monotonic()
        self.close_after_reply = False
        # Ambient devices stream constantly; we only respond once addressed by name.
        self.ambient = False
        self.awake = True
        # MCP client state (the device is the MCP server)
        self.mcp_supported = False
        self.mcp_next_id = 1
        self.mcp_pending: dict[int, asyncio.Future] = {}
        self.mcp_tools: dict[str, dict] = {}  # openai-safe name -> mcp tool
        self.board_name = ""

    # ---------------------------------------------------------------- transport
    async def send_json(self, msg: dict):
        msg.setdefault("session_id", self.session_id)
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(msg, ensure_ascii=False))

    async def run(self):
        watchdog = asyncio.create_task(self._idle_watchdog())
        announcer = asyncio.create_task(self._announce_finished_work()) if self.hub.assistant else None
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_json(json.loads(msg.data))
                elif msg.type == WSMsgType.BINARY:
                    self._on_audio(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("ws error: %s", self.ws.exception())
        finally:
            watchdog.cancel()
            if announcer:
                announcer.cancel()
            if self.turn:
                self.turn.cancel()
            for fut in self.mcp_pending.values():
                fut.cancel()

    async def _idle_watchdog(self):
        scfg = self.hub.cfg["server"]
        ticks = 0
        while not self.ws.closed:
            await asyncio.sleep(1)
            ticks += 1
            if self.ambient and ticks % 30 == 0:
                # the firmware drops a channel that has been silent for 120 s
                await self.send_json({"type": "tana", "state": "ping"})
            busy = self.turn is not None and not self.turn.done()
            quiet = time.monotonic() - self.last_activity
            if busy or self.vad.in_speech:
                continue
            if self.ambient:
                if self.awake and quiet > scfg["follow_up_seconds"]:
                    await self.set_awake(False)
            elif quiet > scfg["idle_timeout"]:
                log.info("[%s] idle for %ss, closing", self.device_id, scfg["idle_timeout"])
                await self.ws.close()

    async def set_awake(self, awake: bool):
        if not self.ambient or awake == self.awake:
            return
        self.awake = awake
        self.last_activity = time.monotonic()
        if awake:
            metrics.inc("tana_boards_wakes_total")
        log.info("[%s] %s", self.device_id, "awake" if awake else "dormant")
        await self.send_json({"type": "tana", "state": "awake" if awake else "dormant"})

    # ---------------------------------------------------------------- messages
    async def _on_json(self, msg: dict):
        kind = msg.get("type")
        if kind == "hello":
            await self._on_hello(msg)
        elif kind == "listen":
            await self._on_listen(msg)
        elif kind == "abort":
            log.info("[%s] abort (%s)", self.device_id, msg.get("reason", ""))
            await self._cancel_turn()
            await self.send_json({"type": "tts", "state": "stop"})
        elif kind == "tana" and isinstance(msg.get("sense"), dict):
            # sensing must never take down the board's connection (a detector bug did, 10/12)
            for name, fn in (("presence", lambda: self.hub.presence.motion(self.device_id, msg["sense"])),
                             ("stillness", lambda: self.hub.stillness.feed(self.device_id, time.time(), msg["sense"])),
                             ("occupancy", lambda: self.hub.occupancy.sense(self.device_id, msg["sense"]))):
                try:
                    fn()
                except Exception:
                    log.exception("[%s] %s failed on a sense report (connection kept)", self.device_id, name)
        elif kind == "tana" and isinstance(msg.get("sonar_result"), dict):
            # Multi-channel sonar arrives as one message per channel; resolve once all are in
            res = msg["sonar_result"]
            parts = self.hub.sonar_parts.setdefault(self.device_id, {})
            if "channel" in res:
                parts[res["channel"]] = res
                if len(parts) < res.get("channels", 1):
                    return
            result = [parts[k] for k in sorted(parts)] if parts else [res]
            self.hub.sonar_parts[self.device_id] = {}
            fut = self.hub.sonar_waiters.pop(self.device_id, None)
            if fut and not fut.done():
                fut.set_result(result)
        elif kind == "tana" and isinstance(msg.get("probe_result"), dict):
            self.hub.presence.probe_results[self.device_id] = msg["probe_result"]
        elif kind == "mcp":
            self._on_mcp(msg.get("payload") or {})
        else:
            log.debug("[%s] unhandled message: %s", self.device_id, msg)

    async def _on_hello(self, msg: dict):
        params = msg.get("audio_params") or {}
        rate = int(params.get("sample_rate", 16000))
        frame = int(params.get("frame_duration", FRAME_MS))
        self.opus_in = OpusIn(rate, frame)
        features = msg.get("features") or {}
        self.mcp_supported = bool(features.get("mcp"))
        self.ambient = bool(features.get("ambient"))
        self.awake = not self.ambient
        await self.send_json(
            {
                "type": "hello",
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": OUT_RATE,
                    "channels": 1,
                    "frame_duration": FRAME_MS,
                },
            }
        )
        log.info("[%s] hello (mcp=%s ambient=%s)", self.device_id, self.mcp_supported, self.ambient)
        if self.ambient:
            await self.send_json({"type": "tana", "state": "dormant"})
        node = (self.hub.cfg.get("ruview") or {}).get("stream_devices", {}).get(self.device_id)
        if node:
            host = self.hub.cfg["ruview"]["host"] or self.hub.cfg["server"]["public_host"]
            await self.send_json({"type": "tana", "csi_stream": {"host": host, "port": self.hub.cfg["ruview"]["port"],
                                                                  "node": int(node)}})
        if self.mcp_supported:
            asyncio.create_task(self._mcp_discover())

    async def _on_listen(self, msg: dict):
        state = msg.get("state")
        self.last_activity = time.monotonic()
        if state == "start":
            self.mode = msg.get("mode", "auto")
            self.listening = True
            self.vad.reset()
            self.manual_audio = []
        elif state == "stop":
            self.listening = False
            if self.mode == "manual":
                audio = np.concatenate(self.manual_audio) if self.manual_audio else None
                self.manual_audio = []
                if audio is not None and len(audio) > 16000 * 0.3:
                    self._start_turn(audio)
        elif state == "detect":
            log.info("[%s] wake word: %s", self.device_id, msg.get("text", ""))
            if self.turn and not self.turn.done():
                await self._cancel_turn()
            await self.set_awake(True)

    def _on_audio(self, packet: bytes):
        if not self.listening or self.opus_in is None:
            return
        if self.turn and not self.turn.done():
            return  # already answering; ignore mic until the device listens again
        try:
            pcm = self.opus_in.decode(packet)
        except Exception:  # corrupt frame
            return
        if self.mode == "manual":
            self.manual_audio.append(pcm)
            return
        was_speaking = self.vad.in_speech
        try:
            self.hub.occupancy.audio(self.device_id, pcm, self.vad.in_speech)
        except Exception:
            log.exception("[%s] occupancy failed on audio (connection kept)", self.device_id)
        if self.vad.feed(pcm):
            audio = self.vad.utterance()
            self.vad.reset()
            self._start_turn(audio)
        if self.vad.in_speech and not was_speaking:
            self.hub.presence.speech(self.device_id)  # someone is talking in this room
            log.info("[%s] speech detected (presence hint)", self.device_id)
        if self.vad.in_speech and self.awake:
            self.last_activity = time.monotonic()

    # ---------------------------------------------------------------- a turn
    def _start_turn(self, audio: np.ndarray):
        self.last_activity = time.monotonic()
        self.turn = asyncio.create_task(self._turn(audio))

    async def _cancel_turn(self):
        if self.turn and not self.turn.done():
            self.turn.cancel()
            try:
                await self.turn
            except (asyncio.CancelledError, Exception):
                pass

    async def _turn(self, audio: np.ndarray):
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        self.turn_started = t0                      # for the time to the first spoken audio
        text, no_speech, logprob = await loop.run_in_executor(
            None, lambda: self.hub.asr.transcribe_scored(audio, hint=self.awake))
        metrics.observe("tana_boards_transcribe_seconds", time.monotonic() - t0)
        log.info("[%s] heard (%.2fs%s, nospeech %.2f, logprob %.2f): %r", self.device_id,
                 time.monotonic() - t0, "" if self.awake else ", dormant", no_speech, logprob, text)
        if not text or not re.search(r"\w", text):
            return
        if not self.awake:
            wcfg = self.hub.cfg["wake"]
            rest = strip_wake_phrase(text, wcfg["names"], wcfg["search_words"])
            if rest is None:
                return  # not for us
            # Reject wakes Whisper itself doubts; a bare "Tana" from a short sound needs more certainty
            bare = not re.search(r"\w", rest)
            if no_speech > wcfg["max_no_speech"] or logprob < wcfg["min_logprob"] or (
                    bare and (no_speech > wcfg["bare_max_no_speech"] or logprob < wcfg["bare_min_logprob"]
                              or len(audio) < 16000 * wcfg["bare_min_seconds"])):
                log.info("[%s] ignored uncertain wake", self.device_id)
                return
            await self.set_awake(True)
            if not re.search(r"\w", rest):
                return  # just "Hey Tana": wait for the actual request
        self.close_after_reply = False
        await self.send_json({"type": "stt", "text": text})
        await self.send_json({"type": "llm", "emotion": "thinking", "text": "🤔"})
        try:
            await self._reply(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] reply failed", self.device_id)
            await self.send_json({"type": "tts", "state": "start"})
            await self._speak(self.hub.cfg["assistant"]["unavailable"])
            await self.send_json({"type": "tts", "state": "stop"})
        finally:
            self.last_activity = time.monotonic()
        if self.close_after_reply:
            if self.ambient:
                await self.set_awake(False)
            else:
                await asyncio.sleep(0.5)
                await self.ws.close()

    # ---------------------------------------------------------------- the assistant (assistant.url)
    @property
    def chat_id(self) -> str:
        return "board-" + re.sub(r"[^\w.-]", "-", self.device_id)

    def _assistant_tools(self) -> list[dict]:
        """The tools only this board (and the home it is in) can run, offered to the assistant."""
        tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                  "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                 for t in LOCAL_TOOLS + home.TOOLS]
        tools += [{"name": name, "description": t.get("description", ""),
                   "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}}
                  for name, t in self.mcp_tools.items()]
        return tools

    def _assistant_instructions(self) -> str:
        now = datetime.now().astimezone()
        room = self.hub.device_names.get(self.device_id)
        return self.hub.cfg["assistant"]["instructions"] + (
            f" Current date and time: {now.strftime('%A %d %B %Y, %H:%M %Z')}."
            + (f" This device is in: {room}." if room else ""))

    async def _reply_assistant(self, user_text: str):
        """One turn through the assistant: its reply is spoken as it streams in; it may call this board's tools."""
        self.play_clock = 0.0
        self.frames_sent = 0
        state = {"started": False, "emotion": False, "full": ""}
        splitter = SentenceSplitter()

        async def say(sentence: str):
            if not state["started"]:
                await self.send_json({"type": "tts", "state": "start"})
                state["started"] = True
            await self._speak(sentence)

        async def on_text(piece: str):
            state["full"] += piece
            if not state["emotion"] and state["full"].strip():
                emotion = first_emotion(state["full"])
                if emotion or len(state["full"].strip()) > 3:
                    await self.send_json({"type": "llm", "emotion": emotion or "neutral", "text": ""})
                    state["emotion"] = True
            for sentence in splitter.push(piece):
                await say(sentence)

        async def run_tool(name: str, args: dict) -> str:
            return await self._run_tool(name, json.dumps(args))

        try:
            await self.hub.assistant.turn(self.chat_id, user_text, self._assistant_tools(),
                                          self._assistant_instructions(), on_text, run_tool)
        except Unavailable as e:
            metrics.inc("tana_boards_turns_total", result="assistant unavailable")
            log.warning("[%s] assistant unavailable: %s", self.device_id, e)
            for sentence in splitter.flush():
                await say(sentence)
            await say(self.hub.cfg["assistant"]["unavailable"])
        else:
            metrics.inc("tana_boards_turns_total", result="answered")
            for sentence in splitter.flush():
                await say(sentence)
        if state["started"]:
            await self._drain()
            await self.send_json({"type": "tts", "state": "stop"})

    async def _announce_finished_work(self):
        """Work this board asked for that finishes later is announced here, as soon as it is done."""
        acfg = self.hub.cfg["assistant"]
        since = None
        while not self.ws.closed:
            await asyncio.sleep(acfg["poll_seconds"])
            try:
                feed = await self.hub.assistant.events(self.chat_id, since or 0)
            except Unavailable:
                continue
            if since is None:                      # connecting: only work that finishes from now on
                since = feed.get("last") or 0
                continue
            since = feed.get("last") or since
            for e in feed.get("events", []):
                if not e.get("result") and "-> failed" not in e.get("message", ""):
                    continue
                while self.turn and not self.turn.done():
                    await asyncio.sleep(1)
                ok = "-> done" in e["message"].split(":")[0]
                try:
                    summary = await self.hub.assistant.condense(e.get("result") or "", "concise") if e.get("result") else ""
                except Unavailable:
                    summary = ""
                title = e.get("title") or "Your task"
                text = (f"{title} is done. {summary} Want the full report?" if ok
                        else f"{title} failed. {summary}").strip()
                log.info("[%s] announcing: %s", self.device_id, text[:200])
                metrics.inc("tana_boards_announcements_total")
                await self.set_awake(True)          # the board's wake sound, then the news
                self.play_clock = 0.0
                self.frames_sent = 0
                await self.send_json({"type": "tts", "state": "start"})
                sp = SentenceSplitter()
                for sentence in sp.push(text) + sp.flush():
                    await self._speak(sentence)
                await self._drain()
                await self.send_json({"type": "tts", "state": "stop"})
                self.last_activity = time.monotonic()

    async def _reply(self, user_text: str):
        if self.hub.assistant:
            return await self._reply_assistant(user_text)
        await self.send_json({"type": "tts", "state": "start"})
        self.play_clock = 0.0
        self.frames_sent = 0
        await self._speak(self.hub.cfg["assistant"]["unavailable"])
        await self._drain()
        await self.send_json({"type": "tts", "state": "stop"})

    async def _speak(self, sentence: str):
        text = clean_for_speech(sentence)
        if not text:
            return
        loop = asyncio.get_running_loop()
        ts = time.monotonic()
        audio, rate = await loop.run_in_executor(None, self.hub.tts.synthesize, text)
        metrics.observe("tana_boards_speech_seconds", time.monotonic() - ts)
        if getattr(self, "turn_started", None):
            metrics.observe("tana_boards_first_audio_seconds", time.monotonic() - self.turn_started)
            self.turn_started = None
        if rate != OUT_RATE:
            audio = soxr.resample(audio, rate, OUT_RATE)
        frames = self.opus_out.encode(audio)
        await self.send_json({"type": "tts", "state": "sentence_start", "text": text})
        frame_s = FRAME_MS / 1000
        now = time.monotonic()
        if self.play_clock < now:  # playback ran dry (or first sentence): restart the clock
            self.play_clock = now
            self.frames_sent = 0
        for frame in frames:
            # stay PREBUFFER_FRAMES ahead of real time so an abort takes effect quickly
            ahead = self.play_clock - time.monotonic()
            if self.frames_sent >= PREBUFFER_FRAMES and ahead > PREBUFFER_FRAMES * frame_s:
                await asyncio.sleep(ahead - PREBUFFER_FRAMES * frame_s)
            if self.ws.closed:
                return
            await self.ws.send_bytes(frame)
            self.play_clock += frame_s
            self.frames_sent += 1

    async def _drain(self):
        remaining = self.play_clock - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    # ---------------------------------------------------------------- tools
    async def _run_tool(self, name: str, arguments: str) -> str:
        metrics.inc("tana_boards_tool_calls_total", tool=name)
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            args = {}
        log.info("[%s] tool %s %s", self.device_id, name, args)
        if name in home.TOOL_NAMES:
            try:
                return await home.run(self.hub, name, args, here=self.device_id)
            except Exception as e:
                return f"Couldn't do that: {e}"
        if name == "end_conversation":
            self.close_after_reply = True
            return "The conversation will end after your reply."
        tool = self.mcp_tools.get(name)
        if tool is None:
            return f"Unknown tool {name}"
        try:
            result = await self._mcp_request("tools/call", {"name": tool["name"], "arguments": args}, timeout=10)
        except Exception as e:
            return f"Tool failed: {e}"
        content = result.get("content") or []
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict)) or json.dumps(result)

    def _on_mcp(self, payload: dict):
        fut = self.mcp_pending.pop(payload.get("id"), None)
        if fut and not fut.done():
            if "error" in payload:
                fut.set_exception(RuntimeError(json.dumps(payload["error"])))
            else:
                fut.set_result(payload.get("result") or {})

    async def _mcp_request(self, method: str, params: dict, timeout: float = 10) -> dict:
        req_id = self.mcp_next_id
        self.mcp_next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.mcp_pending[req_id] = fut
        await self.send_json(
            {"type": "mcp", "payload": {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}}
        )
        return await asyncio.wait_for(fut, timeout)

    async def _mcp_discover(self):
        try:
            init = await self._mcp_request("initialize", {"capabilities": {}})
            self.board_name = (init.get("serverInfo") or {}).get("name", "")
            cursor = ""
            while True:
                res = await self._mcp_request("tools/list", {"cursor": cursor})
                for t in res.get("tools", []):
                    self.mcp_tools[_tool_name(t["name"])] = t
                cursor = res.get("nextCursor") or ""
                if not cursor:
                    break
            log.info("[%s] %s tools: %s", self.device_id, self.board_name, ", ".join(t["name"] for t in self.mcp_tools.values()))
        except Exception as e:
            log.warning("[%s] MCP discovery failed: %s", self.device_id, e)
