"""tana-boards configuration: defaults merged with an optional YAML file (plugins/boards/config.yaml)."""
from __future__ import annotations

import copy
import os
import socket
from pathlib import Path

import yaml

DEFAULTS: dict = {
    # boards.db (settings learned by presence), occupancy profiles, sensing recordings, sonar captures
    "data_dir": str(Path(__file__).resolve().parents[1] / "data"),
    "server": {
        "host": "0.0.0.0",
        "port": 8003,
        # Address the devices use to reach this machine. Auto-detected when empty.
        "public_host": "",
        # Shared secret sent by devices as "Authorization: Bearer <token>". Empty disables auth.
        "token": "",
        # Close the voice channel after this many seconds without speech.
        "idle_timeout": 30,
        # Ambient devices (Tana firmware) stay connected; after a reply they remain awake
        # for this many seconds of silence before going back to waiting for "Hey Tana".
        "follow_up_seconds": 10,
        "timezone_offset_minutes": None,  # None = use this machine's local offset
    },
    # Ultrasonic sonar (19-21 kHz) is audible to cats and some people: off unless enabled here
    "sonar": {"enabled": False},
    "firmware": {
        # Directory holding tana-<version>.bin images offered to devices over OTA.
        "dir": str(Path(__file__).resolve().parents[1] / "firmware" / "releases"),
        # Firmware under test: offered only to the boards listed in test_devices
        "staging_dir": str(Path(__file__).resolve().parents[1] / "firmware" / "staging"),
        "test_devices": [],
    },
    # Boards that stream raw CSI to the RuView listener (udp/5005) whenever they connect
    "ruview": {"stream_devices": {}, "host": "", "port": 5005},  # {"<mac>": node_id}; host "" = this machine
    "presence": {
        "auto_lights": True,
        "hold_minutes": 30,     # a room stays "occupied" this long after the last movement/voice
        # Lights only switch on by themselves while it's dark at this location (sunset to sunrise,
        # computed daily, daylight saving included). dark_offset_minutes shifts the start.
        "dark_only": True,
        "dark_offset_minutes": 0,
        # where the boards are, for sunset and sunrise: set it in config.yaml (the default is Greenwich, UK)
        "location": {"name": "Greenwich", "lat": 51.477, "lon": 0.0, "tz": "Europe/London"},
        # Seated/still detection from the signal shape: off until it's proven reliable
        "use_stillness": False,
        # Turning a room on needs a burst (3 s median) this far above its empty level:
        "enter_rise": 0.008, "enter_noise": 6.0,   # at least +0.008, and 6x the empty noise
        # Keeping it on needs only the 30 s average this far above empty (seated people):
        "stay_rise": 0.002, "stay_noise": 2.5,
        # …or the signal shape differing from the empty-room snapshot by this many "empty
        # variations" (z-score), which catches people sitting perfectly still:
        "shape_stay": 3.0,
        "only_between": None,   # e.g. ["17:00", "07:00"] to auto-switch lights only in the evening
        "near_rssi": -70,       # a board only claims a light it hears at least this strongly (dBm)
    },
    # board MAC -> {"name": "Living room", "lights": ["LivingRoom"], "auto": true}
    "rooms": {},
    # board MAC -> the name the assistant is told ("This device is in: ...")
    "devices": {},
    # Occupancy detector (Wi-Fi movement + signal strength + voices + sound); its tuning defaults are in
    # occupancy.py. control_lights: boards whose rooms' lights follow it (on when occupied after dark).
    "occupancy": {},
    # Seated/still presence from the signal shape (experimental; report-only); tuning in stillness.py
    "stillness": {},
    # Lights on a fixed daily timetable: [{"light": "Porch", "on_at": "sunset", "off_at": "23:00"}]
    "light_schedules": [],
    "asr": {
        "model": "small.en",
        "device": "cpu",
        "compute_type": "int8",
        "download_root": str(Path.home() / ".cache" / "tana" / "whisper"),
        "language": "en",
        "hotwords": "Tana",
    },
    "wake": {
        # Words (lowercase, from Whisper's transcript) that count as the assistant's name.
        # Any word ending in "tana" also matches ("Oktana").
        "names": ["tana", "tanna", "tanah", "tahna", "tarna"],
        "search_words": 4,  # the name must appear within the first N words
        # Confidence gates (Whisper's own estimates) against waking on noise or TV
        "max_no_speech": 0.6, "min_logprob": -1.2,
        # stricter for just "Tana" on its own
        "bare_max_no_speech": 0.3, "bare_min_logprob": -1.1, "bare_min_seconds": 0.5,
    },
    "vad": {
        "threshold": 0.5,
        "silence_ms": 700,  # trailing silence that ends an utterance
        "min_speech_ms": 250,
        "max_speech_s": 20,  # force an utterance to end after this long
    },
    # The boards' AI: an assistant speaking the chat-turn API (see assistant.py). Each board is one chat
    # ("board-<id>"); its own and the home's tools are offered to it. Boards keep working without the
    # assistant; only their AI stops ("unavailable" is spoken).
    "assistant": {
        "backend": "rameness",             # what answers the boards: a swappable backend (tana/assistant.py)
        "url": "http://127.0.0.1:7788",    # the backend's address (rameness: a manager's chat-turn API)
        "timeout": 300,                    # seconds a reply may stay silent before it counts as failed
        "poll_seconds": 3,                 # how often a connected board checks for work that finished
        "unavailable": "My AI isn't available right now.",
        "instructions": (
            "Your replies are spoken aloud by a small round voice assistant device in the user's home, so use "
            "plain spoken sentences: no markdown, lists, tables or code. Keep replies to one to three sentences; "
            "when the full answer would be longer, give the short version and offer more detail or the full "
            "answer. Begin every reply with exactly one emoji that matches your mood, chosen from: "
            "😶 🙂 😆 😂 😔 😠 😭 😍 😳 😯 😱 🤔 😉 😎 😌 🤤 😘 😏 😴 😜 🙄. Use the device's tools when the user "
            "asks to change something on the device or in the home. When the user says goodbye or clearly wants "
            "to stop talking, reply briefly and call end_conversation."
        ),
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


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def load(path: str | None) -> dict:
    cfg = DEFAULTS
    if path and os.path.exists(path):
        with open(path) as f:
            cfg = _merge(DEFAULTS, yaml.safe_load(f) or {})
    cfg = copy.deepcopy(cfg)
    if not cfg["server"]["public_host"]:
        cfg["server"]["public_host"] = lan_ip()
    return cfg
