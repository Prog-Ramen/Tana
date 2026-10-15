"""Claude (Anthropic's API) as a chat brain for the web app's Claude threads."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import anthropic

WEB_CONTEXT = (
    "The user is talking to you through Tana's private web app, often from their car, "
    "and may listen to your replies read aloud. Keep answers concise and conversational; "
    "avoid tables and long lists unless asked."
)


def _now() -> str:
    return datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M %Z")


class ClaudeBrain:
    name = "claude"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        key = os.environ.get("ANTHROPIC_API_KEY")
        key_file = Path(cfg["api_key_file"])
        if not key and key_file.is_file():
            key = key_file.read_text().strip()
        self.client = anthropic.AsyncAnthropic(api_key=key) if key else None

    @property
    def available(self) -> bool:
        return self.client is not None

    async def stream(self, history: list[dict]) -> AsyncIterator[str]:
        if self.client is None:
            yield "Claude isn't set up yet: add an Anthropic API key on the server."
            return
        system = (
            "You are Claude, made by Anthropic, reachable through the user's home assistant app "
            f"(the local assistant is called Tana). {WEB_CONTEXT}\nCurrent date and time: {_now()}."
        )
        async with self.client.beta.messages.stream(
            model=self.cfg["model"],
            max_tokens=self.cfg["max_tokens"],
            system=system,
            messages=history,
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg["effort"]},
            # re-run a safety-declined request on Anthropic's recommended fallback model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            yield "\n\n(Claude declined to answer this one.)"
        elif final.stop_reason == "max_tokens":
            yield " …(cut off)"
