"""Room occupancy from everything a board senses: Wi-Fi movement, Wi-Fi signal strength, voice and
sound level. Report-only: decisions are logged (data/occupancy/events.log), lights are not touched.

What the tests on 2026-10-04 showed, and how each signal is used:
  - movement bursts (Wi-Fi), voice (VAD) and sounds above the room's background are certain signs
    that someone is there right now -> occupied immediately;
  - someone sitting still shifts the board's signal strength by 0.6-3 dB from the room's recent
    empty level (direction depends on the room) -> keeps the room occupied while nothing moves;
  - leaving looks like: a burst, then the signal settles back to the empty level with no sound
    -> empty within about a minute;
  - the empty level drifts over hours, so it is tracked while the room is empty and quiet;
    a board reboot shifts everything, so references are dropped after a data gap.
Fallbacks: no movement/voice/sound for `quiet_minutes` and no seated signal -> empty;
nothing at all for `max_still_minutes` -> empty.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger("tana.occupancy")

DEFAULTS = {
    "burst_factor": 2.0, "burst_min": 0.11,   # Wi-Fi movement burst: peak vs recent quiet peak level
    "sound_rise_db": 10.0,                    # a sound this far above the room's background counts as activity
    "settle_seconds": 20,                     # signal strength is judged over 20 s without bursts
    "rssi_step_db": 0.7,                      # seated: settled level this far from the empty level
    "rssi_back_db": 0.35,                     # left: settled level back within this of the empty level
    "leave_after_seconds": 90,                # ...for this long after the last activity
    "strong_bursts": 3, "strong_window": 15,  # entering needs 3 burst-seconds within 15 s...
    # (single spikes and small blips happen in empty rooms, so neither can switch a room on)
    "quiet_minutes": 10,                      # no activity and no seated signal -> empty
    "max_still_minutes": 90,                  # no activity at all -> empty regardless
    "empty_tau_minutes": 15,                  # how fast the empty level follows drift while empty
    "gap_seconds": 45,                        # data gap (reboot): drop references
    "report_hold_seconds": 30,                # a new state must hold this long before it is announced
    # Rooms where the effect of people is known from tests ("up": living room, where an empty room
    # has higher signal strength, less jitter, less flicker, less movement and a different signal
    # shape). There, the room learns what "occupied" looks like during each stay (30 s windows,
    # after 10 min) and calls it empty when at least 2 of those 5 features move the empty way
    # (z > 1.5) in 4 of the last 6 windows, with no movement, voice or sound for 90 s.
    "empty_direction": {},
    # Continuous presence: per room, learned "occupied" and "empty" profiles (30 s windows of the 5
    # features). Each window adds (distance to occupied - distance to empty), clipped, to a leaky
    # score; movement/voice/sound pull it firmly to occupied. Crossing +/-threshold switches the room.
    # Movement/sound in an empty room is only a precursor: the room's signal shape must also move
    # away from the learned empty shape (by shape_enter, in units of its own spread) within 2 min.
    "shape_ref_windows": 6, "shape_enter": 4.0,   # user asked for higher (10/5 2:55 AM): walking outside the room reached 2.3-6.0x
    # Fast exit (both profiles learned): within 60 s after a movement burst, if the last 5 s of
    # signal (shape + strength) is closer to the empty profile than the occupied one by fast_margin
    # for 3 s in a row, with no voice/sound for 10 s -> empty at once. For 2 min afterwards, the
    # signal looking like the occupied profile again (3 s) switches the room straight back on.
    "fast_exit": True, "fast_margin": 1.0, "fast_seconds": 3, "fast_recheck_seconds": 120,
    "cont_min_windows": 10, "cont_decay": 0.8, "cont_threshold": 4.0, "cont_clip": 3.0,
    "occ_shape_strong": 99.0,
    "occ_learn_windows": 20, "occ_votes": 2, "occ_z": 1.5, "occ_persist": (4, 6),
}

# How each feature moves when a room with direction "up" empties: +1 rises, -1 falls
FEATURE_DIR = {"rssi": +1, "jitter": -1, "flicker": -1, "move": -1}


class RoomOccupancy:
    def __init__(self, device_id: str, name: str, cfg: dict):
        self.device_id, self.name, self.cfg = device_id, name, cfg
        self.state = "unknown"
        self.reason = ""
        self.since = 0.0
        self.reported = "unknown"            # last announced state (after report_hold_seconds)
        self.peaks: deque[float] = deque(maxlen=600)
        self.burst_times: deque[float] = deque(maxlen=10)
        self.last_any_burst = 0.0
        self.rssi_win: deque[float] = deque(maxlen=cfg["settle_seconds"])
        self.direction = (cfg.get("empty_direction") or {}).get(device_id)
        self.win: list[dict] = []                            # current 30 s window of reports
        self.win_start = 0.0
        self.occ_windows: deque[dict] = deque(maxlen=240)    # this stay's windows (up to 2 h)
        self.recent_votes: deque[int] = deque(maxlen=cfg["occ_persist"][1])
        self.last_votes: int | None = None
        self.profiles = {"occupied": deque(maxlen=240), "empty": deque(maxlen=240)}
        self.cont_score = 0.0
        self.cont_ready = False
        self.marked_empty_at = 0.0
        self.pending_entry = 0.0
        self.last_profiles: deque[list[float]] = deque(maxlen=5)   # last 5 s, for fast entry checks
        self.candidate: dict | None = None     # shape-change curve of the current entry candidate
        self.fast_hits = 0
        self.exit_hits = 0
        self.fast_exited_at = 0.0
        self.last_voice_sound = 0.0
        self.last_rssi: deque[float] = deque(maxlen=5)
        self.sound_hist: deque[float] = deque(maxlen=1800)   # 30 min of per-second sound levels (dB)
        self.last_t = 0.0
        self.last_activity = 0.0
        self.last_activity_kind = ""
        self.last_strong = 0.0          # strong movement or voice: blips and household sounds don't count
        self.empty_rssi: float | None = None
        self.trusted_empty_rssi: float | None = None        # from a person's "empty" or a held empty
        self.level: float | None = None      # settled signal strength
        self.seated = False
        self.events: list[dict] = []
        self.candidates: list[dict] = []        # finished entry candidates, written to candidates.log
        self.sound_level: float | None = None

    # ------------------------------------------------------------------ helpers
    def _set(self, state: str, reason: str, t: float):
        if state == "occupied" and self.state != "occupied":
            self.occ_windows.clear()
            self.recent_votes.clear()
        if state != self.state:
            self.state, self.reason, self.since = state, reason, t

    def _activity(self, kind: str, t: float):
        if kind in ("movement", "sound") and self.state != "occupied" \
                and len(self.profiles["empty"]) >= self.cfg["shape_ref_windows"]:
            # Movement alone can be a moving toy or a pet: it only makes the room a candidate.
            # Voice/sound, or signal strength + shape looking like a person (body) in the room,
            # must confirm it within 2 minutes (see _continuous).
            self.pending_entry = t
            return
        self.last_activity, self.last_activity_kind = t, kind
        self.cont_score = min(self.cont_score, -self.cfg["cont_threshold"])
        self.pending_entry = 0.0
        if self.state != "occupied":
            if self.state == "empty" and self.reported != "empty":
                # the "empty" never held: whatever reference it took was really someone sitting
                self.empty_rssi = self.trusted_empty_rssi
            self._set("occupied", kind, t)

    # ------------------------------------------------------------------ inputs
    def sense(self, t: float, report: dict):
        rssi = report.get("rssi")
        peak = float(report.get("max", report.get("motion", 0)) or 0)
        if self.last_t and t - self.last_t > self.cfg["gap_seconds"]:
            self.empty_rssi = None
            for p in self.profiles.values():
                p.clear()
            self.rssi_win.clear()
        self.last_t = t
        self.started = getattr(self, "started", 0.0) or t
        quiet = statistics.median(self.peaks) if len(self.peaks) >= 60 else None
        self.peaks.append(peak)
        if quiet is not None and peak > max(self.cfg["burst_min"], self.cfg["burst_factor"] * quiet):
            self.rssi_win.clear()
            self.burst_times.append(t)
            self.last_any_burst = t
            recent = sum(1 for b in self.burst_times if t - b <= self.cfg["strong_window"])
            # entering needs sustained movement: a single spike, however big, is a glitch (1:12 AM 10/5)
            strong = recent >= self.cfg["strong_bursts"]
            if strong:
                self.last_strong = t
            departing = self.cfg.get("ignore_blips_when_departing") and bool(self.recent_votes) \
                and self.recent_votes[-1] >= self.cfg["occ_votes"]
            if strong or (self.state == "occupied" and not departing):
                self._activity("movement", t)
        elif rssi is not None:
            self.rssi_win.append(float(rssi))
        if len(self.rssi_win) == self.rssi_win.maxlen:
            self.level = statistics.median(self.rssi_win)
        prof = report.get("profile") or []
        if prof:
            self.last_profiles.append([float(x) for x in prof])
        if report.get("rssi") is not None:
            self.last_rssi.append(float(report["rssi"]))
        self._fast_entry(t)
        self._fast_exit(t)
        self._window(t, report)
        self._decide(t)

    def _recent_body(self) -> dict | None:
        if len(self.last_profiles) < 5 or len(self.last_rssi) < 5:
            return None
        n = min(len(p) for p in self.last_profiles)
        return {"shape": [statistics.fmean(p[k] for p in self.last_profiles) for k in range(n)],
                "rssi": statistics.median(self.last_rssi)}

    def _ready(self) -> bool:
        n = self.cfg["cont_min_windows"]
        return len(self.profiles["occupied"]) >= n and len(self.profiles["empty"]) >= n

    def _fast_exit(self, t: float):
        """Seconds-fast exits and their undo, using the two learned profiles (signal shape + strength)."""
        c = self.cfg
        if not c.get("fast_exit") or not self._ready():
            return
        f = self._recent_body()
        if f is None:
            return
        po, pe = self.profiles["occupied"], self.profiles["empty"]
        margin = self._distance(f, po, ("rssi",)) - self._distance(f, pe, ("rssi",))   # > 0: like empty
        if self.state == "occupied":
            recent_burst = self.burst_times and t - self.burst_times[-1] <= 60
            quiet = t - self.last_voice_sound >= 10
            self.exit_hits = self.exit_hits + 1 if (recent_burst and quiet and margin >= c["fast_margin"]) else 0
            if self.exit_hits >= c["fast_seconds"]:
                self.exit_hits = 0
                self.fast_exited_at = t
                self.cont_score = c["cont_threshold"]
                self._set("empty", f"left: the signal looks like the empty room ({margin:+.1f})", t)
        elif self.state == "empty" and self.fast_exited_at and t - self.fast_exited_at <= c["fast_recheck_seconds"]:
            # undo a fast exit that was wrong (moved seats, sat still): looks occupied again
            self.exit_hits = self.exit_hits + 1 if margin <= -c["fast_margin"] else 0
            if self.exit_hits >= c["fast_seconds"]:
                self.exit_hits, self.fast_exited_at = 0, 0.0
                self.last_activity, self.last_activity_kind = t, "still there"
                self.last_strong = t
                self.cont_score = -c["cont_threshold"]
                self._set("occupied", f"still there: the signal looks occupied again ({margin:+.1f})", t)

    def _close_candidate(self, outcome: str):
        c, self.candidate = self.candidate, None
        if c and c["z"]:
            z = c["z"]
            self.candidates.append({"start": c["start"], "outcome": outcome, "at3": z[min(2, len(z) - 1)],
                                    "peak10": max(z[:10]), "peak30": max(z[:30]), "curve": z[:60]})

    def _fast_entry(self, t: float):
        """Confirm a pending entry within seconds: the last 5 s of signal shape vs the empty room's
        shape, 3 seconds in a row above shape_enter."""
        pe = self.profiles["empty"]
        if not self.pending_entry or self.state == "occupied" or len(pe) < self.cfg["shape_ref_windows"] \
                or len(self.last_profiles) < 5:
            self.fast_hits = 0
            return
        if t - self.pending_entry > 120:
            self.pending_entry, self.fast_hits = 0.0, 0
            self._close_candidate("ignored")
            return
        n = min(len(p) for p in self.last_profiles)
        cur = {"shape": [statistics.fmean(p[k] for p in self.last_profiles) for k in range(n)]}
        z = self._distance(cur, pe, ())
        if self.candidate is None or self.candidate["start"] != self.pending_entry:
            if self.candidate is not None:
                self._close_candidate("superseded")
            self.candidate = {"start": self.pending_entry, "z": []}
            self.fast_hits = 0          # each candidate needs its own 3 seconds
        self.candidate["z"].append(round(z, 2))
        self.fast_hits = self.fast_hits + 1 if z > self.cfg["shape_enter"] else 0
        if self.fast_hits >= 3:
            self.pending_entry, self.fast_hits = 0.0, 0
            self._close_candidate("occupied")
            self.last_activity, self.last_activity_kind = t, "movement"
            self.last_strong = t
            self.cont_score = -self.cfg["cont_threshold"]
            self._set("occupied", f"movement, and the signal shape changed ({z:.1f}x)", t)

    def sound_floor(self) -> float | None:
        """The room's background loudness (10th percentile of the last 30 min); needs 10 min first."""
        if len(self.sound_hist) < 600:
            return None
        h = sorted(self.sound_hist)
        return h[len(h) // 10]

    def sound(self, t: float, level_db: float, speaking: bool, louder_elsewhere: bool = False):
        self.sound_level = level_db
        floor = self.sound_floor()
        self.sound_hist.append(level_db)
        if louder_elsewhere:
            return  # the same sound, clearly louder in another room: it's happening there
        if speaking:
            self.last_voice_sound = t
            self.last_strong = t
            self._activity("voice", t)
        elif floor is not None and level_db > floor + self.cfg["sound_rise_db"]:
            # a sound alone (appliance, pet, noise through the wall) can keep an occupied room
            # occupied, but only makes an empty room occupied together with Wi-Fi movement
            self.last_voice_sound = t
            # a sound only counts with movement in this room nearby in time: on its own it is as
            # likely a door, footsteps or an appliance elsewhere in the house (kept an empty room
            # "occupied" for 7 h on 10/5)
            if t - self.last_any_burst <= (600 if self.state == "occupied" else 30):
                self._activity("sound", t)

    def _window(self, t: float, report: dict):
        """Every 30 s: summarise the window; while occupied, learn it or score it against this stay."""
        prof = report.get("profile") or []
        if report.get("rssi") is not None and prof:
            self.win.append(report)
        if not self.win_start:
            self.win_start = t
        if t - self.win_start < 30:
            return
        w, self.win, self.win_start = self.win, [], t
        if len(w) < 20:
            return
        n = min(len(r["profile"]) for r in w)
        P = [[float(x) for x in r["profile"][:n]] for r in w]
        f = {"t": t, "rssi": statistics.median(r["rssi"] for r in w),
             "jitter": statistics.fmean(float(r.get("rssi_std") or 0) for r in w),
             "flicker": statistics.fmean(abs(P[i][k] - P[i - 1][k]) for i in range(1, len(P)) for k in range(n)),
             "move": statistics.fmean(float(r.get("motion") or 0) for r in w),
             "shape": [statistics.fmean(p[k] for p in P) for k in range(n)]}
        self._continuous(t, f)
        if self.state != "occupied" or not self.direction:
            return
        hist = list(self.occ_windows)
        if len(hist) >= self.cfg["occ_learn_windows"]:
            votes = 0
            for k, d in FEATURE_DIR.items():
                vals = [h[k] for h in hist]
                sd = statistics.pstdev(vals) or 1e-6
                if d * (f[k] - statistics.median(vals)) / sd > self.cfg["occ_z"]:
                    votes += 1
            mean = [statistics.fmean(h["shape"][k] for h in hist) for k in range(n)]
            spread = statistics.fmean(abs(h["shape"][k] - mean[k]) for h in hist for k in range(n)) or 1e-6
            shape_z = statistics.fmean(abs(f["shape"][k] - mean[k]) for k in range(n)) / spread - 1
            if shape_z > self.cfg["occ_z"]:
                votes += 1
            if shape_z > self.cfg["occ_shape_strong"]:
                votes += 1   # a very large change in where the energy sits counts double
            self.recent_votes.append(votes)
            self.last_votes = votes
            if votes <= 1:
                self.occ_windows.append(f)   # clearly occupied: keep learning this stay (never learn a
                                             # window that is drifting toward empty)
        else:
            self.occ_windows.append(f)

    def save_profiles(self, path: Path):
        path.write_text(json.dumps({k: list(v) for k, v in self.profiles.items()}))

    def load_profiles(self, path: Path):
        try:
            data = json.loads(path.read_text())
            for k in self.profiles:
                self.profiles[k].extend(data.get(k) or [])
        except (OSError, ValueError):
            pass

    @staticmethod
    def _distance(f: dict, prof, keys=None) -> float:
        if not prof:
            return 0.0
        """How unlike a learned profile this window is: per feature, |value - median| in units of the
        profile's own spread (capped), averaged over signal, jitter, flicker, movement and shape."""
        hist = list(prof)
        d = []
        for k in (keys if keys is not None else FEATURE_DIR):
            vals = [h[k] for h in hist]
            med = statistics.median(vals)
            spread = statistics.median(abs(v - med) for v in vals) * 1.4826 or statistics.pstdev(vals) or 1e-6
            d.append(min(abs(f[k] - med) / spread, 6.0))
        n = min(len(f["shape"]), min(len(h["shape"]) for h in hist))
        mean = [statistics.fmean(h["shape"][i] for h in hist) for i in range(n)]
        spread = statistics.fmean(abs(h["shape"][i] - mean[i]) for h in hist for i in range(n)) or 1e-6
        d.append(min(statistics.fmean(abs(f["shape"][i] - mean[i]) for i in range(n)) / spread, 6.0))
        return statistics.fmean(d)

    def _continuous(self, t: float, f: dict):
        """Ongoing presence: learn the room's two looks, then weigh every window against both."""
        c = self.cfg
        since = t - self.last_activity if self.last_activity else math.inf
        # learn only from windows we're sure about
        if self.state == "occupied" and since <= 120 and self.last_activity_kind != "sound":
            self.profiles["occupied"].append(f)   # learn from movement/voice, never sound alone
        elif self.state == "empty" and (t - self.marked_empty_at < 600 or t - self.last_strong >= 600) \
                and (self.cont_score >= 0 or not self.cont_ready):
            # only trustworthy empties: right after a person said so, or 10 min with no movement,
            # voice or sound at all (never straight after an automatic "empty", which may be wrong)
            self.profiles["empty"].append(f)
        po, pe = self.profiles["occupied"], self.profiles["empty"]
        self.cont_ready = len(po) >= c["cont_min_windows"] and len(pe) >= c["cont_min_windows"]
        if self.pending_entry and t - self.pending_entry > 120:
            self.pending_entry = 0.0                                 # unconfirmed movement: ignored
        if not self.cont_ready:
            if self.pending_entry and self.state != "occupied" and len(pe) >= c["shape_ref_windows"]:
                z = self._distance(f, pe, ())                        # shape only, vs the empty room
                if z > c["shape_enter"]:
                    self.pending_entry = 0.0
                    self.last_activity, self.last_activity_kind = t, "movement"
                    self.last_strong = t
                    self.cont_score = -c["cont_threshold"]
                    self._set("occupied", f"movement, and the signal shape changed ({z:.1f}x)", t)
            return
        T = c["cont_threshold"]
        if self.state == "occupied":
            e = self._distance(f, po) - self._distance(f, pe)          # > 0: looks more like empty
            e = max(-c["cont_clip"], min(c["cont_clip"], e))
            self.cont_score = c["cont_decay"] * self.cont_score + e
            if self.cont_score >= T and since >= c["leave_after_seconds"]:
                self.empty_rssi = self.level
                self._set("empty", "looks like the empty room (all features), no movement or sound", t)
            return
        # empty: only the body features (signal strength, shape) count; movement, jitter and flicker
        # also rise for a moving toy
        body = ("rssi",)
        e = self._distance(f, po, body) - self._distance(f, pe, body)
        e = max(-c["cont_clip"], min(c["cont_clip"], e))
        self.cont_score = c["cont_decay"] * self.cont_score + e
        if self.pending_entry and t - self.pending_entry > 120:
            self.pending_entry = 0.0                                 # unconfirmed movement: ignored
        if self.pending_entry and e <= -1.0:
            self.pending_entry = 0.0
            self.last_activity, self.last_activity_kind = t, "movement"
            self.last_strong = t
            self._set("occupied", "movement, and the signal looks like a person is there", t)
        elif self.cont_score <= -T:
            self._set("occupied", "the signal looks like a person is there", t)

    # ------------------------------------------------------------------ decision
    def _decide(self, t: float):
        c = self.cfg
        since_act = t - self.last_activity if self.last_activity else math.inf
        lvl, ref = self.level, self.empty_rssi
        self.seated = lvl is not None and ref is not None and abs(lvl - ref) >= c["rssi_step_db"]
        if self.state == "occupied" and self.cont_ready:
            if t - self.last_strong >= c["max_still_minutes"] * 60:
                self._set("empty", f"no strong movement or voice for {c['max_still_minutes']} min", t)
        elif self.state == "occupied":
            need, of = c["occ_persist"]
            if self.cfg.get("vote_exit") and self.direction == "up" and len(self.recent_votes) == of \
                    and sum(v >= c["occ_votes"] for v in self.recent_votes) >= need \
                    and since_act >= c["leave_after_seconds"]:
                self.empty_rssi = lvl
                self._set("empty", "signal strength, jitter, flicker, movement and shape look like an empty room", t)
            elif ref is not None and lvl is not None and since_act >= c["leave_after_seconds"] \
                    and abs(lvl - ref) <= c["rssi_back_db"]:
                self._set("empty", "signal back to the empty level, no movement or sound", t)
            elif since_act >= c["quiet_minutes"] * 60 and not self.seated and ref is not None:
                # (without an empty reference we can't see someone sitting still, so only the
                # long max_still rule below may empty the room)
                self._set("empty", f"no movement or sound for {c['quiet_minutes']} min", t)
            elif t - self.last_strong >= c["max_still_minutes"] * 60:
                self._set("empty", f"no strong movement or voice for {c['max_still_minutes']} min", t)
        elif self.state == "empty":
            if self.seated and since_act > 60:
                pass  # a level shift with no activity at all is drift, not a person: follow it below
            if lvl is not None and since_act > 120:
                if ref is None:
                    self.empty_rssi = lvl
                else:
                    a = 1 / (c["empty_tau_minutes"] * 60)
                    self.empty_rssi = ref + a * (lvl - ref)
        else:  # unknown: wait for activity, or call it empty after a quiet spell
            watched = t - getattr(self, "started", t)
            if min(t - self.last_strong, watched) >= c["quiet_minutes"] * 60 and lvl is not None:
                self.empty_rssi = lvl
                self._set("empty", f"no movement or sound for {c['quiet_minutes']} min", t)
        # announce only states that held for a while
        hold = 0 if (self.state == "occupied" or self.reason.startswith("left:")) else c["report_hold_seconds"]
        if self.state != self.reported and t - self.since >= hold:
            prev, self.reported = self.reported, self.state
            if self.state == "empty":
                self.trusted_empty_rssi = self.empty_rssi
            ev = {"t": self.since, "room": self.name, "device": self.device_id, "state": self.state,
                  "from": prev, "reason": self.reason}
            self.events.append(ev)
            log.info("%s: %s (%s)", self.name, self.state, self.reason)

    def mark(self, state: str, t: float):
        """A person says what's true; an empty mark also resets the empty level."""
        if state == "empty" and self.state == "occupied" and self.reason not in ("voice", "told by a person"):
            # the automatic entry was wrong (toy, pet...): forget what it taught the occupied profile
            po = self.profiles["occupied"]
            keep = [w for w in po if w.get("t", 0) < self.since]
            po.clear()
            po.extend(keep)
            self.cont_ready = self._ready()
        if state == "empty":
            self.empty_rssi = self.trusted_empty_rssi = self.level
            self.last_activity = 0.0
            self.marked_empty_at = t
            self.cont_score = self.cfg["cont_threshold"]
        else:
            self.last_activity, self.last_activity_kind = t, "told by a person"
            self.last_strong = t
        self._set(state, "told by a person", t)

    def snapshot(self) -> dict:
        return {"room": self.name, "state": self.state, "reason": self.reason, "since": self.since or None,
                "last_activity": self.last_activity or None, "last_activity_kind": self.last_activity_kind,
                "signal": self.level, "empty_signal": self.empty_rssi, "seated_signal": self.seated,
                "sound_db": self.sound_level, "empty_votes": self.last_votes,
                "occupied_windows_learned": len(self.occ_windows),
                "presence_score": round(-self.cont_score, 2) if self.cont_ready else None,
                "profiles": {k: len(v) for k, v in self.profiles.items()}}


class Occupancy:
    def __init__(self, cfg: dict | None, rooms_cfg: dict, data_dir: Path):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.rooms_cfg = rooms_cfg or {}
        self.rooms: dict[str, RoomOccupancy] = {}
        self.dir = Path(data_dir) / "occupancy"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sound_acc: dict[str, list] = {}
        self._last_rise: dict[str, tuple[float, float | None]] = {}
        self._saved: dict[str, float] = {}

    def room(self, device_id: str) -> RoomOccupancy:
        if device_id not in self.rooms:
            name = (self.rooms_cfg.get(device_id) or {}).get("name") or device_id[-5:]
            self.rooms[device_id] = RoomOccupancy(device_id, name, self.cfg)
            self.rooms[device_id].load_profiles(self._profile_path(device_id))
            self._saved[device_id] = time.time()
        return self.rooms[device_id]

    def _profile_path(self, device_id: str) -> Path:
        return self.dir / f"profiles-{device_id.replace(':', '')}.json"

    def _flush(self, r: RoomOccupancy, n: int):
        while r.candidates:
            c = r.candidates.pop(0)
            with open(self.dir / "candidates.log", "a") as f:
                f.write(json.dumps({"room": r.name, "device": r.device_id, **c}) + "\n")
        for ev in r.events[n:]:
            with open(self.dir / "events.log", "a") as f:
                f.write(json.dumps(ev) + "\n")

    def sense(self, device_id: str, report: dict, t: float | None = None):
        r = self.room(device_id); n = len(r.events)
        r.sense(t or time.time(), report)
        self._flush(r, n)
        if time.time() - self._saved.get(device_id, 0) > 300:   # keep learned profiles across restarts
            self._saved[device_id] = time.time()
            r.save_profiles(self._profile_path(device_id))

    def audio(self, device_id: str, pcm, speaking: bool):
        """Called per decoded audio frame (float32 mono, -1..1); reduced to one loudness value per
        second. Only that number is kept, never the audio."""
        now = time.time()
        acc = self._sound_acc.setdefault(device_id, [now, 0.0, 0, False])
        if len(pcm):
            acc[1] += float(np.dot(pcm, pcm)); acc[2] += len(pcm)
        acc[3] = acc[3] or speaking
        if now - acc[0] >= 1.0:
            if acc[2]:
                level = 10 * math.log10(acc[1] / acc[2] + 1e-12)
                r = self.room(device_id); k = len(r.events)
                floor = r.sound_floor()
                rise = None if floor is None else level - floor
                self._last_rise[device_id] = (now, rise)
                louder = rise is not None and any(
                    d != device_id and o is not None and abs(now - ot) <= 1.5 and o >= rise + 6
                    for d, (ot, o) in self._last_rise.items())
                r.sound(now, level, acc[3], louder)
                self._flush(r, k)
                with open(self.dir / "sound-levels.log", "a") as f:
                    f.write(f"{now:.0f} {device_id} {level:.1f} {int(acc[3])}\n")
            self._sound_acc[device_id] = [now, 0.0, 0, False]

    def fingerprint(self, device_id: str, which: str) -> dict | None:
        """A learned profile summarised for the board-side fallback, with exactly the statistics
        RoomOccupancy._distance uses (shape mean + mean abs spread, signal-strength median + robust spread)."""
        r = self.rooms.get(device_id)
        hist = list(r.profiles[which]) if r else []
        if len(hist) < self.cfg["shape_ref_windows"]:
            return None
        n = min(len(h["shape"]) for h in hist)
        mean = [statistics.fmean(h["shape"][i] for h in hist) for i in range(n)]
        spread = statistics.fmean(abs(h["shape"][i] - mean[i]) for h in hist for i in range(n)) or 1e-6
        vals = [h["rssi"] for h in hist]
        med = statistics.median(vals)
        rs = statistics.median(abs(v - med) for v in vals) * 1.4826 or statistics.pstdev(vals) or 1e-6
        return {"mean": [round(x, 5) for x in mean], "spread": round(spread, 6), "rssi": round(med, 2),
                "rssi_spread": round(rs, 4)}

    def board_rebooted(self, device_id: str):
        r = self.rooms.get(device_id)
        if r:
            for p in r.profiles.values():
                p.clear()
            r.empty_rssi = r.trusted_empty_rssi = None
            r.cont_ready = False
        try:
            self._profile_path(device_id).unlink()
        except OSError:
            pass
        log.info("%s rebooted: occupancy profiles and references cleared", device_id)

    def mark(self, device_id: str, state: str):
        self.room(device_id).mark(state, time.time())

    def snapshot(self) -> dict:
        return {d: r.snapshot() for d, r in self.rooms.items()}
