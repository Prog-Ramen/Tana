"""Run Claude Code headlessly for the web app and translate its stream into chat events.

Each web message is one `claude -p` run. A session started from the web is resumed directly;
any other session (e.g. one still open in a terminal) is forked first so two writers never
share a transcript. Runs with full permissions, as configured by the owner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

from .claude_code import _tool_note
from .store import Store

log = logging.getLogger("tana.cc")


class ClaudeCodeRunner:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        self.procs: dict[str, asyncio.subprocess.Process] = {}  # run key -> running process
        self.lock = asyncio.Lock()  # one run at a time: they share the machine

    def owned_by_web(self, session_id: str) -> bool:
        return self.store.get(f"cc.web.{session_id}") == "1"

    async def run(self, session_id: str | None, text: str, run_key: str,
                  cwd: str | None = None) -> AsyncIterator[dict]:
        """Yields {"t": "session"|"delta"|"step"|"error", ...} events."""
        cmd = [self.cfg["claude_bin"], "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--dangerously-skip-permissions"]
        if session_id:
            cmd += ["--resume", session_id]
            if not self.owned_by_web(session_id):
                cmd.append("--fork-session")
        async with self.lock:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd or self.cfg["cwd"], stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "tana-web"}, limit=16 * 1024 * 1024)
            self.procs[run_key] = proc
            proc.stdin.write(text.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            try:
                async for ev in self._events(proc, cwd or self.cfg["cwd"]):
                    yield ev
                await proc.wait()
                if proc.returncode not in (0, None) and proc.returncode > 0:
                    err = (await proc.stderr.read()).decode(errors="ignore").strip()
                    if err:
                        yield {"t": "error", "text": err[-500:]}
            finally:
                self.procs.pop(run_key, None)
                if proc.returncode is None:
                    proc.kill()

    async def _events(self, proc, proc_cwd: str) -> AsyncIterator[dict]:
        announced = False
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if not announced and ev.get("session_id"):
                sid = ev["session_id"]
                self.store.set(f"cc.web.{sid}", "1")
                self.store.set(f"cc.cwd.{sid}", proc_cwd)
                announced = True
                yield {"t": "session", "id": sid}
            if kind == "stream_event":
                inner = ev.get("event") or {}
                delta = inner.get("delta") or {}
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    yield {"t": "delta", "text": delta.get("text", "")}
                elif inner.get("type") == "message_stop":
                    yield {"t": "break"}
            elif kind == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use":
                        note = _tool_note(b)
                        if note:
                            yield {"t": "step", "text": note}
            elif kind == "result" and ev.get("is_error"):
                yield {"t": "error", "text": str(ev.get("result") or ev.get("subtype"))}

    async def stop(self, run_key: str) -> bool:
        proc = self.procs.get(run_key)
        if proc and proc.returncode is None:
            proc.terminate()
            return True
        return False
