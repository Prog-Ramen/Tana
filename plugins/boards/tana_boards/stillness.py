"""Seated/still presence from step changes in a board's Wi-Fi signal shape (shadow mode).

What the data showed (living room, 2026-10-04):
  - Someone sitting still changes the 16-slice signal shape ("profile") away from the empty room's
    shape by 2.5-6 units of the empty room's own second-to-second variation; empty stays under ~1.
  - But the empty room's shape itself drifts: ~1 unit over 20 min-2 h, 3+ over 4 h, far more
    between night and afternoon. A stored fingerprint therefore goes stale.
So instead of comparing to a fixed fingerprint, this watches transitions: every movement burst is
followed by a settled period, and the settled shape is compared with the most recent empty shape.
  - empty, then settles far from the (continuously refreshed) empty shape  -> someone came in
  - occupied, then settles back near the shape from before they came in    -> they left
While empty, the reference follows the slow drift. Decisions are logged, never acted on.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("tana.stillness")

DEFAULTS = {
    "settle_seconds": 20,     # no burst for this long = settled
    "burst_factor": 2.0,      # a burst: peak movement this many times the recent quiet peak level
    "burst_min": 0.11,        # ...and at least this
    "enter": 2.0,             # settled shape this far from the empty shape (in noise units) = occupied
    "exit": 1.3,              # settled shape this close to the pre-entry empty shape = empty again
    "empty_tau": 600,         # seconds; how fast the empty reference follows drift while empty
    "gap_seconds": 45,        # a data gap this long (board reboot) voids the references -> "unknown"
    "sustain_seconds": 15,    # a decision needs this many consecutive settled seconds agreeing
    "stale_hours": 3,         # beyond this, an "exit" match against the frozen reference is unreliable
}


def _dist(a: list[float], b: list[float], scale: float) -> float:
    n = min(len(a), len(b))
    return sum(abs(a[i] - b[i]) for i in range(n)) / n / scale


class RoomStillness:
    def __init__(self, device_id: str, cfg: dict):
        self.device_id = device_id
        self.cfg = cfg
        self.state = "unknown"                  # "empty" | "occupied" | "unknown"
        self.reason = ""
        self.since = 0.0
        self.peaks: deque[float] = deque(maxlen=600)       # last 10 min of per-second peak movement
        self.window: deque[tuple[float, list[float]]] = deque(maxlen=int(cfg["settle_seconds"]))
        self.last_t = 0.0
        self.last_burst = 0.0
        self.checked_after_burst = True
        self.empty_ref: list[float] | None = None          # live empty shape (follows drift while empty)
        self.empty_scale = 0.0                              # second-to-second variation of the empty shape
        self.frozen_ref: list[float] | None = None         # empty shape just before the room was entered
        self.frozen_at = 0.0
        self.last_dist: float | None = None
        self.far = 0                                         # consecutive settled seconds far from empty
        self.near = 0                                        # consecutive settled seconds back near empty
        self.events: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _settled_shape(self) -> list[float]:
        rows = [p for _, p in self.window]
        return [statistics.fmean(r[i] for r in rows) for i in range(min(len(r) for r in rows))]

    def _window_scale(self) -> float:
        rows = [p for _, p in self.window]
        n = min(len(r) for r in rows)
        return max(statistics.fmean(statistics.pstdev(r[i] for r in rows) for i in range(n)), 1e-3)

    def _set(self, state: str, reason: str, t: float, dist: float | None = None):
        if state == self.state:
            return
        self.state, self.reason, self.since = state, reason, t
        ev = {"t": t, "state": state, "reason": reason, "dist": None if dist is None else round(dist, 2)}
        self.events.append(ev)
        log.info("[%s] stillness: %s (%s, shape distance %s)", self.device_id, state, reason, ev["dist"])

    # ------------------------------------------------------------------ input
    def mark(self, state: str, t: float | None = None):
        """Ground truth from a person ("empty"/"occupied"); an empty mark (re)starts the reference."""
        t = t or time.time()
        self._set(state, "marked", t)
        self.checked_after_burst = True
        if state == "empty" and len(self.window) >= 5:
            self.empty_ref, self.empty_scale = self._settled_shape(), self._window_scale()
        elif state == "empty":
            self.empty_ref = None  # taken at the next settled period

    def feed(self, t: float, report: dict):
        prof = [float(x) for x in (report.get("profile") or [])]
        if len(prof) < 4 or not any(prof):
            return
        peak = float(report.get("max", report.get("motion", 0)) or 0)
        gap = self.last_t and t - self.last_t > self.cfg["gap_seconds"]
        self.last_t = t
        quiet = statistics.median(self.peaks) if len(self.peaks) >= 60 else None
        self.peaks.append(peak)
        if gap:
            # a reboot shifts the signal shape far more than a person does: old references are void
            self.empty_ref = self.frozen_ref = None
            self.far = self.near = 0
            self._set("unknown", "data gap (board restarted?) - needs an empty mark", t)
        burst = gap or (quiet is not None and peak > max(self.cfg["burst_min"], self.cfg["burst_factor"] * quiet))
        if burst:
            self.last_burst = t
            self.window.clear()
            self.checked_after_burst = False
            return
        self.window.append((t, prof))
        if len(self.window) < self.window.maxlen:
            return
        shape = self._settled_shape()
        if self.state == "empty" and self.empty_ref is None:
            self.empty_ref, self.empty_scale = shape, self._window_scale()
            return
        sustain = self.cfg["sustain_seconds"]
        if self.state == "empty":
            dist = _dist(shape, self.empty_ref, self.empty_scale)
            self.last_dist = dist
            # someone came in: the settled shape stays far from the empty shape (re-checked every second)
            self.far = self.far + 1 if dist > self.cfg["enter"] else 0
            if self.far >= sustain:
                self.frozen_ref, self.frozen_at = list(self.empty_ref), t
                self.far = 0
                self._set("occupied", "settled away from the empty shape", t, dist)
            elif dist < self.cfg["exit"] and t - self.last_burst > 60:
                # quiet and close to the reference: follow slow drift (never adapt towards a person)
                a = 1 / self.cfg["empty_tau"]
                self.empty_ref = [(1 - a) * e + a * x for e, x in zip(self.empty_ref, shape)]
        elif self.state == "occupied" and self.frozen_ref is not None:
            dist = _dist(shape, self.frozen_ref, self.empty_scale)
            self.last_dist = dist
            # they left: the settled shape is back near the shape from before they came in
            self.near = self.near + 1 if dist < self.cfg["exit"] else 0
            if self.near >= sustain:
                self.empty_ref, self.near = shape, 0
                self._set("empty", "settled back to the shape from before", t, dist)

    def snapshot(self) -> dict:
        stale = self.frozen_at and self.state == "occupied" and time.time() - self.frozen_at > self.cfg["stale_hours"] * 3600
        return {"state": self.state, "reason": self.reason, "since": self.since or None,
                "shape_distance": None if self.last_dist is None else round(self.last_dist, 2),
                "has_empty_reference": self.empty_ref is not None, "reference_stale": bool(stale),
                "last_burst": self.last_burst or None, "events": self.events[-10:]}


class Stillness:
    """All rooms; logs every decision to <data>/stillness/events.log for scoring against marks."""

    def __init__(self, cfg: dict | None, data_dir: Path):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.rooms: dict[str, RoomStillness] = {}
        self.dir = Path(data_dir) / "stillness"
        self.dir.mkdir(parents=True, exist_ok=True)

    def room(self, device_id: str) -> RoomStillness:
        if device_id not in self.rooms:
            self.rooms[device_id] = RoomStillness(device_id, self.cfg)
        return self.rooms[device_id]

    def feed(self, device_id: str, t: float, report: dict):
        r = self.room(device_id)
        n = len(r.events)
        r.feed(t, report)
        for ev in r.events[n:]:
            self._log({"device": device_id, **ev})

    def mark(self, device_id: str, state: str, note: str = ""):
        r = self.room(device_id)
        r.mark(state)
        self._log({"device": device_id, "t": time.time(), "mark": state, "note": note})

    def _log(self, rec: dict):
        with open(self.dir / "events.log", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def snapshot(self) -> dict:
        return {d: r.snapshot() for d, r in self.rooms.items()}
