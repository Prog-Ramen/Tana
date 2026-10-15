"""Python port of RuView's on-device CSI processing (firmware/esp32-csi-node/main/edge_processing.c,
MIT/Apache-2.0), fed by the boards' raw CSI stream (ADR-018 UDP frames, magic 0xC5110001).

Per node: temporal phase unwrap per subcarrier, Welford variance per subcarrier, top-K most
variable subcarriers (refreshed every 100 frames), the top one's phase drives:
  - motion energy = variance of its last 20 phase samples; presence = energy vs a threshold
    calibrated over the first 1200 frames (mean + 3 sigma), with hysteresis;
  - breathing = 0.1-0.5 Hz biquad band-pass + zero-crossing rate (6-40 BPM kept).
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger("tana.ruview")

MAGIC = 0xC5110001
TOP_K = 8
HISTORY = 256
CALIB_FRAMES = 1200
CALIB_SIGMA = 3.0
HYST_RATIO = 0.5
CLEAR_FRAMES = 5


class Biquad:
    def __init__(self, fs: float, lo: float, hi: float):
        w0 = 2 * math.pi * (lo + hi) / 2 / fs
        bw = 2 * math.pi * (hi - lo) / fs
        alpha = math.sin(w0) * math.sinh(math.log(2) / 2 * bw / math.sin(w0))
        a0 = 1 + alpha
        self.b0, self.b1, self.b2 = alpha / a0, 0.0, -alpha / a0
        self.a1, self.a2 = -2 * math.cos(w0) / a0, (1 - alpha) / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def __call__(self, x: float) -> float:
        y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1, self.y2, self.y1 = self.x1, x, self.y1, y
        return y


def zero_crossing_bpm(sig: list[float], fs: float) -> float:
    cross = [i for i in range(1, len(sig)) if sig[i - 1] <= 0 < sig[i]]
    if len(cross) < 2:
        return 0.0
    period = (cross[-1] - cross[0]) / (len(cross) - 1)
    return fs / period * 60 if period >= 1 else 0.0


class Node:
    def __init__(self, node_id: int):
        self.node_id = node_id
        self.frames = 0
        self.prev_phase: np.ndarray | None = None
        self.n = self.mean = self.m2 = None  # Welford per subcarrier
        self.top_k: list[int] = []
        self.history: deque[float] = deque(maxlen=HISTORY)
        self.br_hist: deque[float] = deque(maxlen=HISTORY)
        self.times: deque[float] = deque(maxlen=200)
        self.fs = 20.0
        self.fs_design = 0.0
        self.bq_br: Biquad | None = None
        self.breathing_bpm = 0.0
        self.motion = 0.0
        self.calib: list[float] = []
        self.threshold: float | None = None
        self.present = False
        self.below = 0
        self.rssi = 0

    def feed(self, iq: bytes, rssi: int, now: float):
        n_sub = len(iq) // 2
        if self.prev_phase is not None and len(self.prev_phase) != n_sub and self.n > 100:
            return  # an odd frame format (e.g. a legacy 64-subcarrier frame) among the usual ones
        a = np.frombuffer(iq, dtype=np.int8).astype(float)
        i_val, q_val = a[0::2], a[1::2]
        raw = np.arctan2(q_val, i_val)
        if self.prev_phase is None or len(self.prev_phase) != n_sub:
            phase = raw
            self.n = 0
            self.mean = np.zeros(n_sub)
            self.m2 = np.zeros(n_sub)
        else:
            d = raw - self.prev_phase
            d = (d + np.pi) % (2 * np.pi) - np.pi
            phase = self.prev_phase + d
        self.prev_phase = phase
        self.frames += 1
        self.rssi = rssi
        # Welford per subcarrier
        self.n += 1
        delta = phase - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (phase - self.mean)
        if self.frames % 100 == 1 or not self.top_k:
            var = self.m2 / max(1, self.n - 1)
            self.top_k = list(np.argsort(var)[::-1][:TOP_K])
        # measured sample rate; redesign the filter when it drifts >15 %
        self.times.append(now)
        if len(self.times) > 20:
            self.fs = (len(self.times) - 1) / (self.times[-1] - self.times[0])
        if self.bq_br is None or abs(self.fs - self.fs_design) > 0.15 * self.fs_design:
            self.bq_br = Biquad(self.fs, 0.1, 0.5)
            self.fs_design = self.fs
        primary = float(phase[self.top_k[0]])
        self.history.append(primary)
        self.br_hist.append(self.bq_br(primary))
        if len(self.history) >= 64:
            bpm = zero_crossing_bpm(list(self.br_hist), self.fs)
            if 6 <= bpm <= 40:
                self.breathing_bpm = bpm
        if len(self.history) >= 10:
            w = list(self.history)[-20:]
            m = sum(w) / len(w)
            self.motion = max(0.0, sum(v * v for v in w) / len(w) - m * m)
        if self.threshold is None:
            self.calib.append(self.motion)
            if len(self.calib) >= CALIB_FRAMES:
                mu = float(np.mean(self.calib))
                sd = float(np.std(self.calib)) or 0.001
                self.threshold = max(0.01, mu + CALIB_SIGMA * sd)
                log.info("ruview node %d calibrated: threshold %.4f", self.node_id, self.threshold)
        thr = self.threshold if self.threshold is not None else 0.05
        if self.motion > thr:
            self.present, self.below = True, 0
        elif self.motion < thr * HYST_RATIO:
            self.below += 1
            if self.below >= CLEAR_FRAMES:
                self.present = False

    def snapshot(self) -> dict:
        return {"node": self.node_id, "frames": self.frames, "rate_hz": round(self.fs, 1),
                "motion": round(self.motion, 5), "threshold": self.threshold and round(self.threshold, 5),
                "calibrating": self.threshold is None, "present": self.present,
                "breathing_bpm": round(self.breathing_bpm, 1), "rssi": self.rssi, "top_k": [int(k) for k in self.top_k]}


class RuViewListener(asyncio.DatagramProtocol):
    def __init__(self, record_dir: Path):
        self.nodes: dict[int, Node] = {}
        self.record_dir = record_dir
        self._rec = None
        self.label = ""

    def record(self, label: str | None):
        if self._rec:
            self._rec.close()
            self._rec = None
        if label is not None:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            path = self.record_dir / time.strftime("raw-%Y%m%d-%H%M%S.bin")
            self._rec = open(path, "ab")
            self.label = label
            return str(path)
        return None

    def datagram_received(self, data: bytes, addr):
        if len(data) < 20:
            return
        magic, node, ant, nsub, freq, seq, rssi, nf, ppdu, flags = struct.unpack("<IBBHIIbbBB", data[:20])
        if magic != MAGIC:
            return
        iq = data[20:20 + nsub * ant * 2]
        now = time.time()
        self.nodes.setdefault(node, Node(node)).feed(iq, rssi, now)
        if self._rec:
            lab = self.label.encode()[:255]
            # record: t (f64), label len (u8), label, frame len (u16), frame
            self._rec.write(struct.pack("<dB", now, len(lab)) + lab + struct.pack("<H", len(data)) + data)


async def start(port: int, record_dir: Path) -> RuViewListener:
    loop = asyncio.get_running_loop()
    _, proto = await loop.create_datagram_endpoint(lambda: RuViewListener(record_dir), local_addr=("0.0.0.0", port))
    log.info("RuView CSI listener on udp/%d", port)
    return proto
