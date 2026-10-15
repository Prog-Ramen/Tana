"""Tana: the web app. Chats with Rameness (the assistant) and Claude, Claude Code sessions, Rameness's agents and
the decisions waiting on you, and the boards' controls, with voice input and spoken replies.

Tana talks to the other services only through their APIs: Rameness (rameness.url) and tana-boards (boards.url).
It loads its own speech models, so someone talking to a board never makes the web app's voice wait.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

import numpy as np
from aiohttp import web

from . import config as config_mod
from .assistant import make_backend
from .claude import ClaudeBrain
from .clients import Boards, Rameness
from .store import Store
from .voice.speech import Asr, Tts
from .webapp import build_local_app, build_web_app

log = logging.getLogger("tana")


class Hub:
    """What the web app's handlers share."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        t = time.monotonic()
        self.asr = Asr(cfg["asr"])
        self.tts = Tts(cfg["tts"])
        # the first inference of each model is slow; pay that cost now, not on the first question
        self.tts.synthesize("Hello.")
        self.asr.transcribe(np.zeros(16000, dtype=np.float32))
        log.info("speech models loaded in %.1fs", time.monotonic() - t)
        self.claude = ClaudeBrain(cfg["claude"])
        self.store = Store(cfg["web"]["db"])
        self.audio_cache: dict[str, tuple[bytes, float]] = {}
        self.pending_totp: str | None = None
        self.assistant = make_backend(cfg.get("assistant") or {})       # what answers Tana's chats
        url = (cfg.get("rameness") or {}).get("url")
        self.rameness = Rameness(url) if url else None                   # Rameness's own views
        burl = (cfg.get("boards") or {}).get("url")
        self.boards = Boards(burl) if burl else None
        self._reach: dict[str, tuple[bool, float]] = {}

    async def reachable(self, name: str) -> bool:
        """Whether a configured service answers (cached 30 s), so the UI shows only what is set up."""
        client = {"rameness": self.rameness, "boards": self.boards}.get(name)
        if client is None:
            return False
        ok, at = self._reach.get(name, (False, 0.0))
        if time.time() - at < 30:
            return ok
        try:
            await client.call("GET", "/api/chats" if name == "rameness" else "/")
            ok = True
        except Exception:
            ok = False
        self._reach[name] = (ok, time.time())
        return ok

    async def close(self):
        for c in (self.rameness, self.assistant, self.boards):
            if c is not None:
                await c.close()


async def serve(cfg: dict):
    hub = Hub(cfg)
    web_app = build_web_app(hub)

    async def cleanup(app):
        await hub.close()

    web_app.on_cleanup.append(cleanup)
    runners = []
    for app, host, port in ((web_app, cfg["web"]["host"], cfg["web"]["port"]),
                            (build_local_app(hub), "0.0.0.0", cfg["web"]["local_port"])):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    log.info("web app on %s:%s, setup and metrics on :%s (Tana's backend: %s, claude %s, boards %s)", cfg["web"]["host"],
             cfg["web"]["port"], cfg["web"]["local_port"], hub.assistant.name if hub.assistant else "none",
             "enabled" if hub.claude.available else "disabled: no API key", cfg["boards"]["url"])
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


def main():
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Tana: the web app")
    parser.add_argument("-c", "--config", default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s")
    try:
        asyncio.run(serve(config_mod.load(args.config)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
