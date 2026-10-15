"""Room presence from the boards' Wi-Fi motion scores and heard speech, driving the lights.

Each board reports a motion score once a second. A room's "empty" level is learned as a low
percentile of its own recent history (nights and empty hours dominate), so no manual
calibration is needed. Motion well above that level, or speech the board hears, marks the
room active; it stays occupied for `hold_minutes` after the last activity.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import logging
import statistics
import time
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lights import Lights

log = logging.getLogger("tana.presence")


class Room:
    """One board's view of its room.

    Two thresholds, both relative to the room's own learned empty level and noise:
      - enter: a short burst well above empty (someone walking in) switches the room on;
      - stay: the 30-second average sitting a little above empty (someone seated, fidgeting,
        breathing) keeps it on. Staying on is deliberately easier than turning on.
    """

    def __init__(self, device_id: str, cfg: dict):
        self.device_id = device_id
        self.name = cfg.get("name") or device_id[-5:]
        self.lights: list[str] = list(cfg.get("lights") or [])
        self.auto = bool(cfg.get("auto", True))
        self.recent: deque[float] = deque(maxlen=30)       # last 30 s, one value per second
        self.history: deque[float] = deque(maxlen=4 * 3600)  # 4 hours of per-second values
        self.calibration: tuple[float, float] | None = cfg.get("calibration")  # (floor, noise) if measured
        self.profiles: deque[list[float]] = deque(maxlen=10)  # last 10 s of signal shape
        self.empty_shape: dict | None = None  # {"mean": [...], "std": [...]} from calibration
        self.calibrating_until = 0.0
        self._cal_samples: list[tuple[float, list[float]]] = []
        self.last_active = 0.0
        self.last_reason = ""
        self.occupied = False
        self.suppress_auto_on = False  # set when someone turns the lights off by hand
        self.last_report = 0.0

    def add_motion(self, motion: float, now: float, raw: dict | None = None):
        self.last_report = now
        self.last_raw = raw or {}
        self.recent.append(motion)
        self.history.append(motion)

    def empty_level(self) -> tuple[float, float] | None:
        """(floor, noise) of this room when nobody is there: a measured calibration if we have
        one, else learned from the quietest part of the last hours."""
        if self.calibration:
            return tuple(self.calibration)  # type: ignore[return-value]
        if len(self.history) < 120:
            return None
        h = sorted(self.history)
        floor = h[int(len(h) * 0.10)]
        noise = max(h[int(len(h) * 0.30)] - h[int(len(h) * 0.05)], 1e-4)
        return floor, noise

    def add_profile(self, profile: list[float], motion: float, now: float):
        if len(profile) < 4 or not any(profile):
            return
        self.profiles.append(profile)
        if getattr(self, "calibrating_from", 0) <= now < self.calibrating_until:
            self._cal_samples.append((motion, profile))

    def shape_change(self) -> float | None:
        """How different the last ~10 s of signal shape is from the empty-room snapshot, in units
        of the empty room's own variation (z-score averaged over the channel slices)."""
        if not self.empty_shape or len(self.profiles) < 5:
            return None
        mean, std = self.empty_shape["mean"], self.empty_shape["std"]
        n = min(len(mean), len(self.profiles[0]))
        cur = [statistics.fmean(p[i] for p in self.profiles) for i in range(n)]
        return statistics.fmean(abs(cur[i] - mean[i]) / std[i] for i in range(n))

    def burst(self) -> float | None:
        return statistics.median(list(self.recent)[-3:]) if self.recent else None

    def steady(self) -> float | None:
        return statistics.fmean(self.recent) if len(self.recent) >= 20 else None

    def thresholds(self, cfg: dict) -> tuple[float, float] | None:
        e = self.empty_level()
        if e is None:
            return None
        floor, noise = e
        enter = floor + max(cfg["enter_rise"], cfg["enter_noise"] * noise)
        stay = floor + max(cfg["stay_rise"], cfg["stay_noise"] * noise)
        return enter, stay

    def snapshot(self, cfg: dict, lights: "Lights") -> dict:
        thr = self.thresholds(cfg)
        e = self.empty_level()
        return {
            "id": self.device_id, "name": self.name, "occupied": self.occupied, "auto": self.auto,
            "motion": self.burst(), "steady": self.steady(), "floor": e and e[0], "noise": e and e[1],
            "threshold": thr and thr[0], "stay_threshold": thr and thr[1],
            "calibrated": bool(self.calibration), "shape_calibrated": bool(self.empty_shape),
            "shape_change": self.shape_change(),
            "calibrating": max(0, round(self.calibrating_until - time.time())),
            "last_active": self.last_active or None, "reason": self.last_reason,
            "sensing": time.time() - self.last_report < 5,
            "raw": getattr(self, "last_raw", {}),
            "lights": [{"name": n, **(lights.state.get(n) or {"on": None})} for n in self.lights],
        }


class Presence:
    def __init__(self, cfg: dict, rooms_cfg: dict, lights: "Lights", store=None):
        self.cfg = cfg
        self.lights = lights
        self.store = store
        self.store_dir = Path(store.db.execute("PRAGMA database_list").fetchone()[2]).parent if store else Path(".")
        self.rooms: dict[str, Room] = {d: Room(d, c or {}) for d, c in (rooms_cfg or {}).items()}
        self.manual_lights = {d for d, c in (rooms_cfg or {}).items() if (c or {}).get("lights")}
        self.probe_results: dict[str, dict] = {}  # device -> {wifi mac: {"rssi", "n"}}
        self.last_locate: dict = json.loads(store.get("lights.located") or "{}") if store else {}
        for d, cal in (json.loads(store.get("presence.calibration") or "{}") if store else {}).items():
            if isinstance(cal, dict):
                self.room(d).calibration = (cal["floor"], cal["noise"])
                self.room(d).empty_shape = cal.get("shape")
        # Settings changed by talking to Tana; they override config.yaml and survive restarts
        self.light_overrides: dict[str, str | None] = {}  # light -> board it was put in by hand
        saved = json.loads(store.get("presence.settings") or "{}") if store else {}
        for d, r in (saved.get("rooms") or {}).items():
            room = self.room(d)
            room.name = r.get("name", room.name)
            room.auto = r.get("auto", room.auto)
        self.light_overrides = saved.get("light_overrides") or {}
        self.cfg.update(saved.get("options") or {})
        self._apply_located()

    def save_settings(self):
        if not self.store:
            return
        options = {k: self.cfg[k] for k in ("hold_minutes", "only_between", "auto_lights", "dark_only",
                                            "dark_offset_minutes") if k in self.cfg}
        self.store.set("presence.settings", json.dumps({
            "rooms": {d: {"name": r.name, "auto": r.auto} for d, r in self.rooms.items()},
            "light_overrides": self.light_overrides,
            "options": options,
        }))

    def resolve_room(self, name: str | None, here: str | None = None) -> "Room | None":
        """'this room'/'here' means the board being spoken to; otherwise match by name."""
        if not name or name.strip().lower() in ("this room", "here", "this", "current room", "in here"):
            return self.rooms.get(here) if here else None
        key = name.strip().lower().removeprefix("the ")
        for r in self.rooms.values():
            if r.name.lower() == key:
                return r
        for r in self.rooms.values():
            if key in r.name.lower() or r.name.lower() in key:
                return r
        return None

    def assign_light(self, light: str, room: "Room | None"):
        """Put a light in a room by hand (None = no room); beats the signal-strength guess."""
        self.light_overrides[light] = room.device_id if room else None
        self._apply_located()
        self.save_settings()

    def _apply_located(self):
        """Lights go to the room whose board hears them loudest, unless placed by hand."""
        assignment = dict(self.last_locate.get("assignment") or {})
        assignment.update(self.light_overrides)
        assignment = {l: d for l, d in assignment.items() if d}
        for l, d in self.light_overrides.items():
            if d is None:
                assignment.pop(l, None)
        for d, room in self.rooms.items():  # drop earlier automatic picks that no longer hold
            if d not in self.manual_lights:
                room.lights = [l for l in room.lights if assignment.get(l) == d]
        for light, device in assignment.items():
            for d, room in self.rooms.items():
                if d not in self.manual_lights and light in room.lights and d != device:
                    room.lights.remove(light)
            if device not in self.manual_lights:
                room = self.room(device)
                if light not in room.lights:
                    room.lights.append(light)

    async def locate(self, sessions: dict, seconds: int = 4) -> dict:
        """Ask every sensing board to measure each light's signal; assign lights to the loudest."""
        await self.lights.refresh(force=True)
        macs = self.lights.wifi_macs()
        targets = [{"ip": self.lights.devices[n]["ip"], "mac": m} for n, m in macs.items()]
        boards = {d: s for d, s in sessions.items() if getattr(s, "ambient", False)}
        if not targets or not boards:
            return {"error": "no lights or no boards connected", "lights": list(macs), "boards": list(boards)}
        self.probe_results = {}
        for s in boards.values():
            await s.send_json({"type": "tana", "probe": {"seconds": seconds, "targets": targets}})
        deadline = time.time() + seconds + 6
        while time.time() < deadline and len(self.probe_results) < len(boards):
            await asyncio.sleep(0.5)
        by_mac = {m: n for n, m in macs.items()}
        table: dict[str, dict[str, float]] = {}  # light -> device -> rssi
        for device, result in self.probe_results.items():
            for mac, r in result.items():
                if r.get("n", 0) > 0 and mac in by_mac:
                    table.setdefault(by_mac[mac], {})[device] = r["rssi"]
        # Each light goes to the board that hears it loudest, if that board hears it clearly;
        # a faint signal only means the light is somewhere else (maybe near a board not reporting).
        assignment = {}
        for light, rs in table.items():
            best = max(rs, key=rs.get)
            if rs[best] >= self.cfg["near_rssi"]:
                assignment[light] = best
        self.last_locate = {"at": time.time(), "rssi": table, "assignment": assignment,
                            "no_reply": sorted(set(boards) - set(self.probe_results))}
        if self.store:
            self.store.set("lights.located", json.dumps(self.last_locate))
        self._apply_located()
        log.info("nearest lights: %s", assignment)
        return self.last_locate

    def room(self, device_id: str) -> Room:
        if device_id not in self.rooms:
            self.rooms[device_id] = Room(device_id, {})
        return self.rooms[device_id]

    def sun_times(self, day=None) -> tuple[datetime, datetime]:
        """Today's (sunrise, sunset) at the configured location, in local time with DST."""
        loc = self.cfg["location"]
        tz = ZoneInfo(loc["tz"])
        day = day or datetime.now(tz).date()
        s = sun(Observer(latitude=loc["lat"], longitude=loc["lon"]), date=day, tzinfo=tz)
        return s["sunrise"], s["sunset"] + timedelta(minutes=self.cfg.get("dark_offset_minutes", 0))

    def is_dark(self) -> bool:
        now = datetime.now(ZoneInfo(self.cfg["location"]["tz"]))
        sunrise, sunset = self.sun_times(now.date())
        return now < sunrise or now >= sunset

    def in_schedule(self) -> bool:
        """Auto lights only when allowed: after dark (dark_only) and/or within fixed local hours
        (only_between, e.g. ["12:00", "23:00"] = until 11 PM); with both set, both must hold."""
        if self.cfg.get("dark_only") and not self.is_dark():
            return False
        window = self.cfg.get("only_between")
        if not window:
            return True
        now = datetime.now(ZoneInfo(self.cfg["location"]["tz"])).strftime("%H:%M")  # local, not server UTC
        start, end = window
        return start <= now < end if start <= end else (now >= start or now < end)

    # ---------------------------------------------------------------- experiment recording
    def record(self, labels: dict | None):
        """Start (labels = {device or "*": label}) or stop (None) saving every raw sensing report
        to data/recordings/*.jsonl for offline analysis."""
        if getattr(self, "_rec", None):
            self._rec.close()
            self._rec = None
        self.rec_labels = labels or {}
        if labels is not None:
            d = Path(self.store_dir) / "recordings"
            d.mkdir(parents=True, exist_ok=True)
            path = d / time.strftime("%Y%m%d-%H%M%S.jsonl")
            self._rec = open(path, "a")
            log.info("recording sensing to %s (%s)", path, labels)
            return str(path)
        return None

    def _record(self, device_id: str, report: dict):
        rec = getattr(self, "_rec", None)
        if rec:
            label = self.rec_labels.get(device_id, self.rec_labels.get("*", ""))
            rec.write(json.dumps({"t": time.time(), "device": device_id, "label": label, "r": report}) + "\n")
            rec.flush()

    # ---------------------------------------------------------------- events from boards
    def motion(self, device_id: str, report: dict):
        now = time.time()
        self._record(device_id, report)
        room = self.room(device_id)
        motion = float(report.get("motion", 0))
        room.add_motion(motion, now, report)
        room.add_profile(list(report.get("profile") or []), motion, now)
        if room.calibrating_until and now >= room.calibrating_until:
            self._finish_calibration(room)
        if room.calibrating_until:
            return  # nobody should be there; don't let the calibration window switch lights
        thr = room.thresholds(self.cfg)
        burst, steady, shape = room.burst(), room.steady(), room.shape_change()
        if thr is not None and burst is not None and burst > thr[0]:
            self._activity(room, "movement", now)
        elif self.cfg.get("use_stillness") and room.occupied:
            if shape is not None and shape > self.cfg["shape_stay"]:
                self._activity(room, "someone still there", now)
            elif thr is not None and steady is not None and steady > thr[1]:
                self._activity(room, "small movements", now)
        self._update(room, now)

    def start_calibration(self, device_id: str, seconds: int = 120, delay: int = 0):
        """Record the room while it's empty for `seconds`, starting after `delay` seconds (time to
        leave): its motion floor/noise and its signal shape."""
        room = self.room(device_id)
        room._cal_samples = []
        room.calibrating_from = time.time() + delay
        room.calibrating_until = room.calibrating_from + seconds
        room.occupied = False
        log.info("%s: recording empty room for %ss (starting in %ss)", room.name, seconds, delay)

    def _finish_calibration(self, room: Room):
        samples = room._cal_samples
        room.calibrating_until = 0.0
        room._cal_samples = []
        if len(samples) < 30:
            log.warning("%s: calibration got too little data (%d)", room.name, len(samples))
            return
        motions = sorted(m for m, _ in samples)
        floor = statistics.median(motions)
        noise = max(motions[int(len(motions) * 0.9)] - motions[int(len(motions) * 0.1)], 1e-4) / 2
        n = min(len(p) for _, p in samples)
        # Shape statistics over 10 s averages (what shape_change compares against)
        windows = [[statistics.fmean(p[i] for _, p in samples[j:j + 10]) for i in range(n)]
                   for j in range(0, len(samples) - 9, 5)]
        mean = [statistics.fmean(w[i] for w in windows) for i in range(n)]
        std = [max(statistics.pstdev(w[i] for w in windows), 0.002) for i in range(n)]
        room.calibration = (floor, noise)
        room.empty_shape = {"mean": mean, "std": std}
        if self.store:
            cal = json.loads(self.store.get("presence.calibration") or "{}")
            cal[room.device_id] = {"floor": floor, "noise": noise, "shape": room.empty_shape}
            self.store.set("presence.calibration", json.dumps(cal))
        log.info("%s calibrated: floor %.4f noise %.4f, shape from %d windows", room.name, floor, noise, len(windows))

    def speech(self, device_id: str):
        """Voices keep an occupied room's lights on; only movement switches a room on."""
        room = self.room(device_id)
        now = time.time()
        if room.occupied:
            self._activity(room, "voice", now)
            self._update(room, now)

    def _activity(self, room: Room, reason: str, now: float):
        room.last_active = now
        room.last_reason = reason

    def _update(self, room: Room, now: float):
        occupied = now - room.last_active < self.cfg["hold_minutes"] * 60
        if occupied == room.occupied:
            return
        room.occupied = occupied
        log.info("%s %s (%s)", room.name, "occupied" if occupied else "empty", room.last_reason)
        if not occupied:
            room.suppress_auto_on = False
            room.auto_on_done = False
        if room.auto and room.lights and self.cfg["auto_lights"]:
            asyncio.create_task(self._apply(room, occupied))

    async def tick(self):
        """Rooms go empty by the clock, not by a report, so check periodically."""
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for room in self.rooms.values():
                self._update(room, now)
                # Someone already in the room when it gets dark: switch on now, once per stay
                if (room.occupied and room.auto and room.lights and self.cfg["auto_lights"]
                        and not getattr(room, "auto_on_done", False) and not room.suppress_auto_on
                        and self.in_schedule()):
                    asyncio.create_task(self._apply(room, True))

    async def _apply(self, room: Room, occupied: bool):
        if occupied and (room.suppress_auto_on or not self.in_schedule()):
            return
        if occupied:
            room.auto_on_done = True
        for name in room.lights:
            try:
                state = self.lights.state.get(name) or {}
                if occupied or state.get("on") is not False:
                    await self.lights.power(name, occupied, by="auto")
            except Exception as e:
                log.warning("auto light %s failed: %s", name, e)

    def manual(self, light: str, on: bool):
        """Someone switched a light by voice or in the app: don't immediately undo it."""
        for room in self.rooms.values():
            if light in room.lights and room.occupied and not on:
                room.suppress_auto_on = True
