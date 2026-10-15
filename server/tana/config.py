"""Configuration: defaults merged with an optional YAML file."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

# Networks that may direct agents (run Claude Code, act on Rameness), in addition to the login: loopback and
# private ranges. Widen it in config.yaml only if you understand that the login is then the only barrier.
PRIVATE_NETWORKS = ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]

DEFAULTS: dict = {
    # What answers Tana's chats (and the Siri Shortcut): a swappable backend, see assistant.py.
    # backend: rameness (url: a Rameness manager) | claude-code | codex | pi | dsh
    "assistant": {"backend": "rameness", "url": "http://127.0.0.1:7788", "timeout": 300},
    # Rameness's own views (its chats, agents and the decisions waiting on you), through its HTTP API
    "rameness": {"url": "http://127.0.0.1:7788"},
    # tana-boards: the voice boards' server, for the boards screen (volume, brightness, rooms, lights)
    "boards": {"url": "http://127.0.0.1:8003"},
    "web": {
        # Put an HTTPS reverse proxy in front: it terminates TLS and forwards here. Connections from any other
        # address than trusted_proxies are refused, and X-Forwarded-For is trusted only from them.
        "host": "0.0.0.0",
        "port": 8004,
        "trusted_proxies": ["127.0.0.1", "::1"],
        "origin": "",  # e.g. https://tana.example.com; when set, other Origins are rejected
        "db": str(Path(__file__).resolve().parents[1] / "data" / "tana.db"),
        "session_days": 30,
        # First-time login setup and /metrics, on a port you never forward: reachable from your network only
        "local_port": 8005,
        # how web chats want replies (markdown is rendered; replies may also be read aloud)
        "chat_instructions": ("The user is chatting through Tana's web app, often from a phone, and may listen to "
                              "replies read aloud. Keep answers concise and conversational; avoid tables and long "
                              "lists unless asked."),
        # the Siri Shortcut: the reply is spoken by Siri
        "spoken_instructions": ("Your reply is spoken aloud by Siri: plain spoken sentences, no markdown, lists or "
                                "code, one to three sentences."),
        # Claude Code transcript folders to show (and continue) in the web app
        "claude_code_projects": ["~/.claude/projects/*"],
        # Claude Code from the web, with no per-action approval: protected by the login and allowed_networks
        "claude_code": {
            "enabled": True,
            "cwd": str(Path.home()),  # for new sessions; resumed ones run in their own folder
            "claude_bin": str(Path.home() / ".local" / "bin" / "claude"),
            "allowed_networks": PRIVATE_NETWORKS,
        },
        # Acting on Rameness (sending work, answering its questions, retiring agents)
        "rameness": {"recent_hours": 24, "allowed_networks": PRIVATE_NETWORKS},
    },
    "claude": {
        "model": "claude-opus-5-5",
        "max_tokens": 16000,
        "effort": "medium",  # low/medium keep spoken replies snappy; high/xhigh for harder questions
        "api_key_file": str(Path(__file__).resolve().parents[1] / "anthropic.key"),
    },
    # Speech for voice input and spoken replies in the web app (its own models, separate from the boards')
    "asr": {
        "model": "small.en",      # faster-whisper model; base.en is faster, medium.en more accurate
        "device": "cpu",
        "compute_type": "int8",
        "download_root": str(Path.home() / ".cache" / "tana" / "whisper"),
    },
    "tts": {
        "model": str(Path.home() / ".cache" / "tana" / "kokoro-v1.0.onnx"),
        "voices": str(Path.home() / ".cache" / "tana" / "voices-v1.0.bin"),
        "voice": "af_heart",
        "speed": 1.05,
        "lang": "en-us",
    },
}

def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None) -> dict:
    cfg = DEFAULTS
    if path and os.path.exists(path):
        with open(path) as f:
            cfg = _merge(DEFAULTS, yaml.safe_load(f) or {})
    return copy.deepcopy(cfg)
