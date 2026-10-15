"""Clients for the local services Tana's web app shows: a Rameness manager (its HTTP API) and tana-boards.

The web app never reads their files or databases: everything goes through these APIs, so each service can be
restarted, moved or replaced on its own.
"""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("tana.clients")


class ServiceDown(Exception):
    """The service could not be reached or failed the request."""


class _Http:
    def __init__(self, url: str, timeout: float = 15):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.http: aiohttp.ClientSession | None = None

    def _session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self.http

    async def close(self):
        if self.http and not self.http.closed:
            await self.http.close()

    async def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        try:
            async with self._session().request(method, self.url + path, json=body, params=params) as r:
                data = await r.json(content_type=None) if r.content_type in ("application/json", "") else await r.text()
                if r.status >= 400:
                    msg = data.get("error") if isinstance(data, dict) else str(data)
                    raise ServiceDown(f"{r.status}: {str(msg)[:300]}")
                return data
        except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError) as e:
            raise ServiceDown(f"{type(e).__name__}: {e}") from None


class Rameness(_Http):
    """A Rameness manager (rameness fleet up --home): its chats, agents and the decisions waiting on you."""

    async def chats(self) -> list[dict]:
        return (await self.call("GET", "/api/chats"))["chats"]

    async def chat_events(self, chat: str, since: int = 0) -> dict:
        return await self.call("GET", f"/api/chats/{chat}/events", params={"since": since})

    async def state(self) -> dict:
        """The team: agent tree, open escalations ("needs you"), recent decisions and events."""
        return await self.call("GET", "/api/state")

    async def agent(self, aid: str) -> dict:
        return await self.call("GET", f"/api/agents/{aid}")

    async def answer(self, eid: str, answer: str) -> dict:
        return await self.call("POST", f"/api/escalations/{eid}", {"answer": answer})

    async def prompt(self, aid: str, text: str) -> dict:
        return await self.call("POST", f"/api/agents/{aid}/prompt", {"text": text})

    async def retire(self, aid: str) -> dict:
        return await self.call("POST", f"/api/agents/{aid}/retire", {})


class Boards(_Http):
    """tana-boards' local API: the boards, their settings, rooms and lights."""

    async def boards(self) -> list[dict]:
        return await self.call("GET", "/api/boards")

    async def set_board(self, did: str, values: dict) -> dict:
        return await self.call("POST", f"/api/boards/{did}", values)

    async def rooms(self) -> dict:
        return await self.call("GET", "/api/rooms")
