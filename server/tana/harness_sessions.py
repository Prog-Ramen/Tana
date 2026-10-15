"""Sessions of other agent harnesses on this machine, shown read-only in the web app: Codex, pi and dsh.

(Claude Code's sessions are mirrored separately by claude_code.py, which also lets you continue them.) Each source
reads the harness's own session files; a harness that is not installed or has no sessions is not shown.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path


def _text(content) -> str:
    """The text of a message's content: a string, or blocks with "text"."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            out.append(b["text"])
    return "\n".join(out)


class Source:
    name = ""
    label = ""

    def files(self) -> list[str]:
        raise NotImplementedError

    def records(self, path: str):
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def message(self, rec: dict) -> dict | None:
        """{"role", "content"} for a conversation message record; None for everything else."""
        raise NotImplementedError

    def session_id(self, path: str) -> str:
        return Path(path).stem

    def cwd(self, rec: dict) -> str | None:
        return None

    def sessions(self) -> list[dict]:
        out = []
        for path in self.files():
            title, cwd = "", ""
            for rec in self.records(path):
                cwd = cwd or self.cwd(rec) or ""
                m = self.message(rec)
                if m and m["role"] == "user" and m["content"].strip():
                    title = m["content"].strip().splitlines()[0][:100]
                    break
            out.append({"harness": self.name, "id": self.session_id(path), "title": title or "(no messages)",
                        "project": Path(cwd).name if cwd else "", "updated": os.path.getmtime(path)})
        return sorted(out, key=lambda s: -s["updated"])

    def read(self, sid: str) -> list[dict]:
        path = next((p for p in self.files() if self.session_id(p) == sid), None)
        if path is None:
            raise KeyError(sid)
        return [m for rec in self.records(path) if (m := self.message(rec)) and m["content"].strip()]


class Codex(Source):
    name, label = "codex", "Codex"

    def files(self):
        return glob.glob(os.path.expanduser("~/.codex/sessions/**/rollout-*.jsonl"), recursive=True)

    def session_id(self, path):
        return Path(path).stem.removeprefix("rollout-")

    def cwd(self, rec):
        return (rec.get("payload") or {}).get("cwd") if rec.get("type") == "session_meta" else None

    def message(self, rec):
        p = rec.get("payload") or {}
        if rec.get("type") != "response_item" or p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
            return None
        text = _text(p.get("content"))
        if p["role"] == "user" and text.lstrip().startswith("<"):     # injected environment / instructions
            return None
        return {"role": p["role"], "content": text}


class Pi(Source):
    name, label = "pi", "pi"

    def files(self):
        return glob.glob(os.path.expanduser("~/.pi/agent/sessions/**/*.jsonl"), recursive=True)

    def cwd(self, rec):
        return rec.get("cwd") if rec.get("type") == "session" else None

    def message(self, rec):
        m = rec.get("message") or {}
        if rec.get("type") != "message" or m.get("role") not in ("user", "assistant"):
            return None
        return {"role": m["role"], "content": _text(m.get("content"))}


class Dsh(Source):
    name, label = "dsh", "dsh"

    def files(self):
        return glob.glob(os.path.expanduser("~/.dsh/sessions/*/session-*/session.v3.jsonl.zstd"))

    def session_id(self, path):
        return Path(path).parent.name

    def records(self, path):
        try:
            raw = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        for line in raw.decode(errors="replace").splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue

    def cwd(self, rec):
        return rec.get("cwd") if rec.get("type") == "session" else None

    def message(self, rec):
        d = rec.get("data") or {}
        if rec.get("type") == "user/message":
            if (d.get("source") or {}).get("kind") != "user":                # runtime context the harness injects
                return None
            return {"role": "user", "content": _text(d.get("content"))}
        if rec.get("type") == "assistant/message":
            return {"role": "assistant", "content": _text(d.get("content") or (d.get("message") or {}).get("content"))}
        return None


SOURCES = {s.name: s for s in (Codex(), Pi(), Dsh())}


def available() -> dict[str, list[dict]]:
    """Each harness that has sessions here, with its sessions (newest first)."""
    out = {}
    for name, src in SOURCES.items():
        try:
            sessions = src.sessions()
        except OSError:
            continue
        if sessions:
            out[name] = sessions
    return out
