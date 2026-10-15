"""Speech recognition (Whisper), speech synthesis (Kokoro), spoken sentences and the wake phrase."""
from __future__ import annotations

import logging
import re
import threading

import numpy as np

log = logging.getLogger("tana.voice")


class Asr:
    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        self.model = WhisperModel(
            cfg["model"],
            device=cfg["device"],
            compute_type=cfg["compute_type"],
            download_root=cfg["download_root"],
        )
        self.lock = threading.Lock()

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcribe_scored(audio)[0]

    def transcribe_scored(self, audio: np.ndarray, hint: bool = True) -> tuple[str, float, float]:
        """Returns (text, no_speech_prob, avg_logprob). hint=False drops the name hint: a hint
        makes Whisper hallucinate the hinted word on short noises, so wake checks run without it."""
        with self.lock:
            segments, _ = self.model.transcribe(
                audio,
                language=self.cfg["language"],
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                hotwords=(self.cfg.get("hotwords") or None) if hint else None,
            )
            segments = list(segments)
        text = " ".join(s.text.strip() for s in segments).strip()
        if not segments:
            return text, 1.0, -10.0
        return (text, max(s.no_speech_prob for s in segments),
                sum(s.avg_logprob for s in segments) / len(segments))


class Tts:
    def __init__(self, cfg: dict):
        from kokoro_onnx import Kokoro

        self.cfg = cfg
        self.kokoro = Kokoro(cfg["model"], cfg["voices"])
        self.lock = threading.Lock()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        with self.lock:
            audio, rate = self.kokoro.create(
                text, voice=self.cfg["voice"], speed=self.cfg["speed"], lang=self.cfg["lang"]
            )
        return audio.astype(np.float32), rate


EMOJI_EMOTIONS = {
    "😶": "neutral", "🙂": "happy", "😆": "laughing", "😂": "funny", "😔": "sad",
    "😠": "angry", "😭": "crying", "😍": "loving", "😳": "embarrassed", "😯": "surprised",
    "😱": "shocked", "🤔": "thinking", "😉": "winking", "😎": "cool", "😌": "relaxed",
    "🤤": "delicious", "😘": "kissy", "😏": "confident", "😴": "sleepy", "😜": "silly",
    "🙄": "confused",
}
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff☀-➿⬀-⯿️‍]+", flags=re.UNICODE
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_SENTENCE_END = re.compile(r"(?<=[.!?;:。！？])\s+|\n+")


def clean_for_speech(text: str) -> str:
    text = _THINK_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[*_`#>|~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def first_emotion(text: str) -> str | None:
    for ch in text.strip()[:4]:
        if ch in EMOJI_EMOTIONS:
            return EMOJI_EMOTIONS[ch]
    return None


class SentenceSplitter:
    """Accumulates streamed text and yields speakable sentences as soon as they complete."""

    def __init__(self, soft_limit: int = 160):
        self.buf = ""
        self.soft_limit = soft_limit
        self.in_think = False

    def push(self, delta: str) -> list[str]:
        self.buf += delta
        # drop any <think> blocks a reasoning model may still emit
        if "<think>" in self.buf and "</think>" not in self.buf:
            return []
        self.buf = _THINK_RE.sub("", self.buf)
        out = []
        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m:
                break
            sentence, self.buf = self.buf[: m.start()], self.buf[m.end():]
            out.append(sentence)
        if len(self.buf) > self.soft_limit and "," in self.buf:
            cut = self.buf.rfind(",") + 1
            out.append(self.buf[:cut])
            self.buf = self.buf[cut:]
        return [s for s in out if clean_for_speech(s)]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf, ""
        return [rest] if clean_for_speech(rest) else []


def strip_wake_phrase(text: str, names: list[str], search_words: int) -> str | None:
    """If `text` addresses the assistant by name near its start, return the rest of it
    (possibly empty); otherwise None. "Hey Tana, what time is it?" -> "what time is it?"""
    words = text.split()
    for i, word in enumerate(words[:search_words]):
        w = re.sub(r"[^a-z]", "", word.lower())
        if w in names or (len(w) > 4 and w.endswith("tana")):
            rest = " ".join(words[i + 1:])
            return re.sub(r"^[\s,.!?:;-]+", "", rest)
    return None
