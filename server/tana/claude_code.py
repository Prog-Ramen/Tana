"""Mirror Claude Code session transcripts into the store as read-only threads.

Reads ~/.claude/projects/<project>/<session>.jsonl and keeps what a person would want to reread:
the user's messages (including ones sent mid-turn and answers to questions), Claude's replies,
and a one-line note per tool call. Thinking and raw tool output are left out, and anything that
looks like a credential is masked before it is stored.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .store import Store

log = logging.getLogger("tana.claude_code")

# Sessions started by a person: the interactive terminal, or this web app. Scripted runs
# (claude -p from benchmarks etc., entrypoint "sdk-cli") are marked automated and listed apart.
INTERACTIVE_ENTRYPOINTS = {"cli", "tana-web"}

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)(\s*[=:]\s*)(['\"]?)[^\s'\"]{12,}\3"),
]


def redact(text: str) -> str:
    text = SECRET_PATTERNS[0].sub("sk-ant-•••", text)
    text = SECRET_PATTERNS[1].sub("[private key removed]", text)
    return SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}•••", text)


def _ts(entry: dict) -> float | None:
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tool_note(block: dict) -> str | None:
    name, args = block.get("name", ""), block.get("input") or {}
    if name == "Bash":
        return f"ran: {args.get('description') or args.get('command', '')[:120]}"
    if name in ("Write", "Edit", "Read"):
        verb = {"Write": "wrote", "Edit": "edited", "Read": "read"}[name]
        return f"{verb} {args.get('file_path', '')}"
    if name == "WebFetch":
        return f"read web page {args.get('url', '')}"
    if name == "WebSearch":
        return f"searched the web: {args.get('query', '')}"
    if name == "AskUserQuestion":
        qs = args.get("questions") or []
        return "asked: " + " / ".join(q.get("question", "") for q in qs if isinstance(q, dict))
    if name in ("ToolSearch", "Skill"):
        return None
    return f"used {name}"


def _is_human_text(text: str) -> bool:
    return bool(text.strip()) and not text.lstrip().startswith(("<task-notification>", "<system-reminder>",
                                                                 "<command-", "<local-command"))


def parse(path: Path) -> tuple[str, list[tuple[str, str, float]], str | None, set[str]]:
    """Returns (title, [(role, content, timestamp)], working directory, entrypoints)."""
    title = ""
    cwd = None
    entrypoints: set[str] = set()
    out: list[tuple[str, str, float]] = []
    last_ts = path.stat().st_mtime
    ask_ids: set[str] = set()
    with path.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = entry.get("type")
            cwd = cwd or entry.get("cwd")
            if kind == "user" and entry.get("entrypoint"):
                entrypoints.add(entry["entrypoint"])
            ts = _ts(entry) or last_ts
            last_ts = ts
            if kind == "ai-title":
                title = entry.get("aiTitle") or title
                continue
            if kind == "attachment":
                att = entry.get("attachment") or {}
                if att.get("type") == "queued_command" and (att.get("origin") or {}).get("kind") == "human":
                    prompt = att.get("prompt")
                    if isinstance(prompt, str) and _is_human_text(prompt):
                        out.append(("user", prompt, ts))
                continue
            if entry.get("isSidechain"):
                continue
            msg = entry.get("message") or {}
            content = msg.get("content")
            if kind == "user":
                if isinstance(content, str):
                    # typed in a terminal (origin human) or sent from the web app (headless: sdk)
                    from_person = ((entry.get("origin") or {}).get("kind") == "human"
                                   or entry.get("promptSource") == "sdk")
                    if from_person and _is_human_text(content):
                        out.append(("user", content, ts))
                elif isinstance(content, list):
                    for b in content:
                        if b.get("type") == "tool_result" and b.get("tool_use_id") in ask_ids:
                            res = b.get("content")
                            text = res if isinstance(res, str) else " ".join(
                                x.get("text", "") for x in res or [] if isinstance(x, dict))
                            m = re.search(r'answered:? (.*?)(?:\. You can now|$)', text, re.S)
                            out.append(("user", (m.group(1) if m else text).strip(), ts))
            elif kind == "assistant" and isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        out.append(("assistant", b["text"], ts))
                    elif b.get("type") == "tool_use":
                        if b.get("name") == "AskUserQuestion":
                            ask_ids.add(b.get("id"))
                        note = _tool_note(b)
                        if note:
                            out.append(("tool", note, ts))
    return title, [(role, redact(text), ts) for role, text, ts in out], cwd, entrypoints


def _session_files(project_dirs: list[str]):
    for pattern in project_dirs:
        pattern = str(Path(pattern).expanduser())
        bases = Path("/").glob(pattern.lstrip("/")) if any(c in pattern for c in "*?[") else [Path(pattern)]
        for base in bases:
            if base.is_dir():
                yield from base.glob("*.jsonl")


def sync(store: Store, project_dirs: list[str]):
    """Re-import any transcript whose size changed since the last sync."""
    for path in _session_files(project_dirs):
        key = f"cc.size2.{path.stem}"  # bump the prefix to force a full re-import
        size = str(path.stat().st_size)
        if store.get(key) == size:
            continue
        try:
            title, messages, cwd, entrypoints = parse(path)
        except Exception:
            log.exception("could not read %s", path)
            continue
        store.set(key, size)
        store.set(f"cc.auto.{path.stem}", "0" if entrypoints & INTERACTIVE_ENTRYPOINTS else "1")
        store.replace_thread(f"cc-{path.stem}", "claude-code", "claude",
                             title or "Claude Code session", messages)
        if cwd:
            store.set(f"cc.cwd.{path.stem}", cwd)
        log.info("synced Claude Code session %s (%d messages)", path.stem, len(messages))
