"""Opus framing and streaming voice-activity detection."""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import onnxruntime

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    import opuslib

IN_RATE = 16000  # device microphone
OUT_RATE = 24000  # what we send back (Kokoro's native rate)
FRAME_MS = 60


class OpusIn:
    def __init__(self, rate: int = IN_RATE, frame_ms: int = FRAME_MS):
        self.dec = opuslib.Decoder(rate, 1)
        self.samples = rate * frame_ms // 1000

    def decode(self, packet: bytes) -> np.ndarray:
        pcm = self.dec.decode(packet, self.samples)
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class OpusOut:
    def __init__(self, rate: int = OUT_RATE, frame_ms: int = FRAME_MS):
        self.enc = opuslib.Encoder(rate, 1, opuslib.APPLICATION_AUDIO)
        self.samples = rate * frame_ms // 1000

    def encode(self, audio: np.ndarray) -> list[bytes]:
        """Encode float32 mono audio into a list of Opus packets, zero-padding the tail."""
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        pad = (-len(pcm)) % self.samples
        if pad:
            pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
        return [
            self.enc.encode(pcm[i : i + self.samples].tobytes(), self.samples)
            for i in range(0, len(pcm), self.samples)
        ]


def _silero_path() -> str:
    import faster_whisper

    return str(Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx")


class VadModel:
    """Shared Silero session; per-stream state lives in VadStream."""

    def __init__(self):
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(
            _silero_path(), providers=["CPUExecutionProvider"], sess_options=opts
        )


class VadStream:
    CHUNK = 512  # Silero window at 16 kHz (32 ms)
    CONTEXT = 64

    def __init__(self, model: VadModel, threshold: float, silence_ms: int, min_speech_ms: int,
                 max_speech_s: float = 20):
        self.model = model
        self.threshold = threshold
        self.silence_chunks = max(1, silence_ms * IN_RATE // 1000 // self.CHUNK)
        self.min_speech_chunks = max(1, min_speech_ms * IN_RATE // 1000 // self.CHUNK)
        self.max_chunks = int(max_speech_s * IN_RATE / self.CHUNK)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(self.CONTEXT, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.audio: list[np.ndarray] = []
        self.pre_roll: list[np.ndarray] = []
        self.speech_chunks = 0
        self.silent_run = 0
        self.in_speech = False

    @property
    def heard_speech(self) -> bool:
        return self.speech_chunks >= self.min_speech_chunks

    def _prob(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self.context, chunk])[None, :]
        out, self.h, self.c = self.model.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = chunk[-self.CONTEXT :]
        return float(np.asarray(out).reshape(-1)[0])

    def feed(self, pcm: np.ndarray) -> bool:
        """Feed audio; returns True once an utterance has ended."""
        self.pending = np.concatenate([self.pending, pcm])
        ended = False
        while len(self.pending) >= self.CHUNK:
            chunk, self.pending = self.pending[: self.CHUNK], self.pending[self.CHUNK :]
            speech = self._prob(chunk) >= self.threshold
            if self.in_speech:
                self.audio.append(chunk)
                if speech:
                    self.speech_chunks += 1
                    self.silent_run = 0
                else:
                    self.silent_run += 1
                    if self.silent_run >= self.silence_chunks and self.heard_speech:
                        ended = True
                if len(self.audio) >= self.max_chunks:
                    ended = True
            elif speech:
                self.in_speech = True
                self.speech_chunks += 1
                self.audio.extend(self.pre_roll)
                self.audio.append(chunk)
            else:
                # keep ~300 ms before speech onset so first syllables aren't clipped
                self.pre_roll = (self.pre_roll + [chunk])[-10:]
        return ended

    def utterance(self) -> np.ndarray:
        return np.concatenate(self.audio) if self.audio else np.zeros(0, dtype=np.float32)
