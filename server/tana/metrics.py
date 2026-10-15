"""Prometheus metrics for Tana's web app and tana-boards (each serves GET /metrics on its local port).

Counters and histograms are kept in the process; current state (sessions, boards, rooms, lights) is passed in as
gauges when scraped. No dependency: the text exposition format is written here.
"""

from __future__ import annotations

import re
import threading

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)

HELP = {
    # the web app
    "tana_chat_messages_total": ("counter", "Chat messages answered, by who answered (tana's backend, claude)."),
    "tana_reply_first_text_seconds": ("histogram", "Time from a chat message to the first words of the reply."),
    "tana_reply_seconds": ("histogram", "Time from a chat message to the end of the reply."),
    "tana_assistant_errors_total": ("counter", "Replies the assistant backend could not give, by backend."),
    "tana_transcriptions_total": ("counter", "Voice recordings transcribed for the web app."),
    "tana_transcribe_seconds": ("histogram", "Time to transcribe a voice recording."),
    "tana_speech_seconds": ("histogram", "Time to synthesize one spoken sentence."),
    "tana_logins_total": ("counter", "Login attempts, by result."),
    "tana_shortcut_requests_total": ("counter", "Siri Shortcut requests, by result."),
    "tana_claude_code_runs_total": ("counter", "Claude Code runs started from the web app."),
    # tana-boards
    "tana_boards_turns_total": ("counter", "Things said to a board that reached its assistant, by result."),
    "tana_boards_first_audio_seconds": ("histogram", "Time from the end of speech to the board's first spoken audio."),
    "tana_boards_transcribe_seconds": ("histogram", "Time to transcribe what was said to a board."),
    "tana_boards_speech_seconds": ("histogram", "Time to synthesize one sentence a board speaks."),
    "tana_boards_wakes_total": ("counter", "Times a board woke up (wake phrase or button)."),
    "tana_boards_tool_calls_total": ("counter", "Board and home tools the assistant called, by tool."),
    "tana_boards_announcements_total": ("counter", "Finished work announced on a board."),
    "tana_boards_light_switches_total": ("counter", "Lights switched, by who switched them (auto, voice, schedule...)."),
    "tana_boards_checkins_total": ("counter", "Board check-ins (boots), by firmware version."),
}

_lock = threading.Lock()
_counters: dict[tuple, float] = {}
_hists: dict[tuple, list] = {}           # key -> [bucket counts..., sum, count]


def _key(name: str, labels: dict) -> tuple:
    return (name, tuple(sorted((k, str(v)) for k, v in labels.items() if v is not None)))


def inc(name: str, value: float = 1, **labels) -> None:
    with _lock:
        k = _key(name, labels)
        _counters[k] = _counters.get(k, 0) + value


def observe(name: str, seconds: float, **labels) -> None:
    with _lock:
        k = _key(name, labels)
        h = _hists.setdefault(k, [0] * len(BUCKETS) + [0.0, 0])
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                h[i] += 1
        h[-2] += seconds
        h[-1] += 1


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _labels(pairs) -> str:
    pairs = [(re.sub(r"[^a-zA-Z0-9_]", "_", k), v) for k, v in pairs]
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in pairs) + "}" if pairs else ""


def render(gauges: list[tuple[str, str, list[tuple[dict, float]]]] = ()) -> str:
    """The exposition text: this process's counters and histograms, plus ``gauges`` given as
    (name, help, [(labels, value), ...])."""
    out: list[str] = []
    with _lock:
        counters, hists = dict(_counters), {k: list(v) for k, v in _hists.items()}
    for name, (kind, text) in HELP.items():
        rows = [(k, v) for k, v in (counters.items() if kind == "counter" else hists.items()) if k[0] == name]
        out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
        for (_, pairs), v in sorted(rows):
            if kind == "counter":
                out.append(f"{name}{_labels(pairs)} {v}")
                continue
            for b, n in zip(BUCKETS, v):
                out.append(f"{name}_bucket{_labels(list(pairs) + [('le', b)])} {n}")
            out.append(f"{name}_bucket{_labels(list(pairs) + [('le', '+Inf')])} {v[-1]}")
            out.append(f"{name}_sum{_labels(pairs)} {round(v[-2], 6)}")
            out.append(f"{name}_count{_labels(pairs)} {v[-1]}")
    for name, text, samples in gauges:
        out += [f"# HELP {name} {text}", f"# TYPE {name} gauge"]
        out += [f"{name}{_labels(sorted(lab.items()))} {val}" for lab, val in samples]
    return "\n".join(out) + "\n"


def reset() -> None:
    """For tests."""
    with _lock:
        _counters.clear()
        _hists.clear()
