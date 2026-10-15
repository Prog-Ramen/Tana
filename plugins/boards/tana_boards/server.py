"""tana-boards: the server for voice boards (xiaozhi protocol): firmware updates (OTA), the voice WebSocket,
presence, occupancy and lights. Each board is a chat with the assistant (assistant.url); boards keep working
without it, only their AI stops."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import numpy as np
from aiohttp import web

from . import config as config_mod
from tana.voice.audio import VadModel
from tana.voice.speech import Asr, Tts

from tana import metrics
from tana.assistant import make_backend
from .lights import Lights
from .presence import Presence
from .stillness import Stillness
from .occupancy import Occupancy
from .session import Session
from .store import Store

log = logging.getLogger("tana.boards")


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


class Hub:
    """Engines and state shared by every device."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        t = time.monotonic()
        self.vad = VadModel()
        self.asr = Asr(cfg["asr"])
        self.tts = Tts(cfg["tts"])
        self.assistant = make_backend(cfg.get("assistant") or {})   # what answers the boards (swappable)
        self.data = Path(cfg["data_dir"])
        self.data.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(self.data / "boards.db"))
        # the first inference of each model is slow; pay that cost now, not on the first question
        self.tts.synthesize("Hello.")
        self.asr.transcribe(np.zeros(16000, dtype=np.float32))
        log.info("engines loaded in %.1fs", time.monotonic() - t)
        self.device_names: dict[str, str] = cfg.get("devices", {}) or {}
        self.devices: dict[str, dict] = {}  # device_id -> last OTA check-in info
        self.lights = Lights()
        self.sonar_waiters: dict[str, asyncio.Future] = {}
        self.sonar_parts: dict[str, dict] = {}
        self.ruview = None  # RuView-algorithm listener for raw CSI (udp/5005)
        self.presence = Presence(cfg["presence"], cfg.get("rooms") or {}, self.lights, self.store)
        # Seated/still presence from signal-shape steps; shadow mode (logs only, never switches lights)
        self.stillness = Stillness(cfg.get("stillness"), self.data)
        # Occupancy from Wi-Fi movement + signal strength + voice + sound level; report-only
        self.occupancy = Occupancy(cfg.get("occupancy"), cfg.get("rooms") or {}, self.data)
        self.sessions: dict[str, Session] = {}  # device_id -> live voice session

    async def occupancy_lights(self):
        """Lights of the rooms in occupancy.control_lights follow the occupancy detector: on when the
        room is occupied after dark (presence schedule), off as soon as it is empty. If someone turns
        them off by hand during a stay, they stay off until the room has emptied."""
        handled: dict[str, str] = {}  # device -> state already acted on
        was_allowed = self.presence.in_schedule()
        while True:
            await asyncio.sleep(1)
            allowed = self.presence.in_schedule()
            if was_allowed and not allowed:
                # end of the allowed hours (e.g. 11 PM): lights off, and none switch on until tomorrow
                for dev in (self.cfg.get("occupancy") or {}).get("control_lights") or []:
                    for n in ((self.cfg.get("rooms") or {}).get(dev) or {}).get("lights") or []:
                        if (self.lights.state.get(n) or {}).get("on") is not False:
                            try:
                                await self.lights.power(n, False, by="auto")
                                log.info("light %s off: end of automatic-light hours", n)
                            except Exception as e:
                                log.warning("lights-off at end of hours %s: %s", n, e)
            was_allowed = allowed
            for dev in (self.cfg.get("occupancy") or {}).get("control_lights") or []:
                room = self.occupancy.rooms.get(dev)
                names = ((self.cfg.get("rooms") or {}).get(dev) or {}).get("lights") or []
                if not room or not names or room.reported not in ("occupied", "empty"):
                    continue
                if handled.get(dev) == room.reported:
                    continue
                try:
                    if room.reported == "empty":
                        for n in names:
                            if (self.lights.state.get(n) or {}).get("on") is not False:
                                await self.lights.power(n, False, by="auto")
                        handled[dev] = "empty"
                    elif self.presence.in_schedule():
                        manual_off = any((st := self.lights.state.get(n) or {}).get("on") is False
                                         and st.get("by") != "auto" and st.get("changed", 0) > room.since
                                         for n in names)
                        if not manual_off:
                            for n in names:
                                await self.lights.power(n, True, by="auto")
                        handled[dev] = "occupied"
                except Exception as e:
                    log.warning("occupancy lights %s: %s", dev, e)

    def schedule_times(self, entry: dict, day) -> list[tuple[float, bool]]:
        """(time, on) events of one light_schedules entry on a given local date."""
        from datetime import datetime, time as dtime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.presence.cfg["location"]["tz"])
        sunrise, sunset = self.presence.sun_times(day)
        def at(spec):
            if spec == "sunset":
                return sunset.timestamp()
            if spec == "sunrise":
                return sunrise.timestamp()
            h, m = (int(x) for x in str(spec).split(":"))
            return datetime.combine(day, dtime(h, m), tz).timestamp()
        return [(at(entry["on_at"]), True), (at(entry["off_at"]), False)] if entry.get("on_at") and entry.get("off_at") else []

    async def light_schedules(self):
        """Lights on fixed daily times (light_schedules in config.yaml, e.g. the fireplace: on at
        sunset, off at 23:00). Acts only at those moments, so switching by hand in between sticks."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        last = time.time()
        while True:
            await asyncio.sleep(20)
            now = time.time()
            today = datetime.now(ZoneInfo(self.presence.cfg["location"]["tz"])).date()
            for entry in self.cfg.get("light_schedules") or []:
                for day in (today - timedelta(days=1), today):
                    for t, on in self.schedule_times(entry, day):
                        if last < t <= now:
                            try:
                                await self.lights.power(entry["light"], on, by="schedule")
                                log.info("schedule: light %s %s", entry["light"], "on" if on else "off")
                            except Exception as e:
                                log.warning("schedule: light %s: %s", entry["light"], e)
            last = now

    def fallback_kit(self, dev: str) -> dict | None:
        """What a board needs to run its room light by itself while this server is unreachable: the learned
        fingerprints for a light that follows occupancy, and/or the next on/off times of a timetable light."""
        room = self.presence.rooms.get(dev)               # live: rooms and lights can be changed by voice
        names = list(room.lights) if room else []
        light = next((n for n in names if n in self.lights.devices), None)
        if not light:
            return None
        ip = self.lights.devices[light]["ip"]
        follows = dev in ((self.cfg.get("occupancy") or {}).get("control_lights") or [])
        empty = self.occupancy.fingerprint(dev, "empty") if follows else None
        schedule = self.timetable(light)
        if not empty and not schedule:
            return None
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(self.presence.cfg["location"]["tz"])).date()
        dark = []
        for d in range(-1, 15):   # sunset -> next sunrise, for two weeks
            day = today + timedelta(days=d)
            dark.append([int(self.presence.sun_times(day)[1].timestamp()),
                         int(self.presence.sun_times(day + timedelta(days=1))[0].timestamp())])
        loc = self.presence.cfg["location"]
        window = self.presence.cfg.get("only_between")
        tzoff = int(datetime.now(ZoneInfo(loc["tz"])).utcoffset().total_seconds() // 60)
        kit = {"light_ip": ip, "dark": dark[:3], "lat": loc["lat"], "lon": loc["lon"],
               "dark_offset_minutes": self.presence.cfg.get("dark_offset_minutes", 0),
               "tz_offset_minutes": tzoff,
               "shape_enter": self.occupancy.cfg["shape_enter"], "exit_margin": self.occupancy.cfg["fast_margin"]}
        if empty:
            kit["empty"] = empty
        if schedule:
            kit["schedule"] = schedule
        if window:
            mins = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:5])
            kit["allowed_from"], kit["allowed_until"] = mins(window[0]), mins(window[1])
        occupied = self.occupancy.fingerprint(dev, "occupied") if empty else None
        if occupied:
            kit["occupied"] = occupied
        return kit

    def timetable(self, light: str, days: int = 14) -> list[list[int]]:
        """[on, off] times (epoch seconds) of a light's daily timetable (light_schedules), the next `days`."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = time.time()
        today = datetime.now(ZoneInfo(self.presence.cfg["location"]["tz"])).date()
        out = []
        for entry in self.cfg.get("light_schedules") or []:
            if entry.get("light") != light:
                continue
            for d in range(-1, days):
                times = self.schedule_times(entry, today + timedelta(days=d))
                if len(times) == 2 and times[1][0] > now:
                    out.append([int(times[0][0]), int(times[1][0])])
        return sorted(out)[:16]

    async def fallback_kits(self):
        """Every minute, give each board with lights in its room its fallback kit (it is also the heartbeat:
        a board that hears nothing for 3 minutes runs its room light itself)."""
        while True:
            await asyncio.sleep(60)
            for dev, room in list(self.presence.rooms.items()):
                if not room.lights:
                    continue
                session = self.sessions.get(dev)
                if not session:
                    continue
                try:
                    kit = self.fallback_kit(dev)
                    if kit:
                        await session.send_json({"type": "tana", "fallback": kit})
                except Exception as e:
                    log.warning("fallback kit for %s: %s", dev, e)

    def firmware_dirs(self, device_id: str | None = None) -> list[Path]:
        fw = self.cfg["firmware"]
        dirs = [Path(fw["dir"])]
        if device_id is None or device_id in fw.get("test_devices", []):
            dirs.append(Path(fw["staging_dir"]))
        return dirs

    def latest_firmware(self, device_id: str | None = "") -> tuple[str, Path] | None:
        best = None
        for p in (f for d in self.firmware_dirs(device_id) for f in d.glob("tana-*.bin")):
            ver = p.stem.removeprefix("tana-")
            if best is None or _version_key(ver) > _version_key(best[0]):
                best = (ver, p)
        return best


def _base_url(cfg: dict) -> str:
    return f"{cfg['server']['public_host']}:{cfg['server']['port']}"


async def ota(request: web.Request) -> web.Response:
    """Device check-in. Stock xiaozhi firmware and Tana firmware both call this on boot."""
    hub: Hub = request.app["hub"]
    cfg = hub.cfg
    device_id = request.headers.get("Device-Id", "unknown")
    info = {}
    if request.method == "POST" and request.can_read_body:
        try:
            info = await request.json()
        except Exception:
            info = {}
    current = (info.get("application") or {}).get("version", "?")
    metrics.inc("tana_boards_checkins_total", version=current)
    hub.devices[device_id] = {
        "version": current,
        "board": (info.get("board") or {}).get("type") or request.headers.get("User-Agent", ""),
        "ip": request.remote,
        "seen": time.time(),
    }
    reasons = {1: "power on", 3: "software restart", 4: "CRASH (panic)", 5: "watchdog (interrupt)",
               6: "watchdog (task)", 7: "watchdog (other)", 8: "woke from sleep", 9: "brownout (power dip)",
               2: "reset button", 14: "power glitch", 15: "CPU lockup"}
    rr = info.get("reset_reason")
    log.info("check-in %s from %s running %s (last restart: %s)", device_id, request.remote, current,
             reasons.get(rr, rr if rr is not None else "not reported"))
    # a check-in means the board just booted: its Wi-Fi signal shape has shifted, so what the
    # occupancy detector learned for that room no longer applies
    hub.occupancy.board_rebooted(device_id)

    tz = cfg["server"]["timezone_offset_minutes"]
    if tz is None:
        tz = int(time.localtime().tm_gmtoff // 60)
    base = _base_url(cfg)
    body: dict = {
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": tz},
        "websocket": {"url": f"ws://{base}/tana/v1/", "token": cfg["server"]["token"] or "tana"},
    }
    fw = hub.latest_firmware(device_id)
    if fw:
        body["firmware"] = {"version": fw[0], "url": f"http://{base}/firmware/{fw[1].name}"}
        # Only Tana firmware understands the "assets" section; it installs matching fonts/emoji
        assets = fw[1].with_name(f"assets-{fw[0]}.bin")
        if assets.is_file():
            body["assets"] = {"version": fw[0], "url": f"http://{base}/firmware/{assets.name}"}
    return web.json_response(body)


async def firmware(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    name = request.match_info["name"]
    if "/" in name or ".." in name:
        raise web.HTTPNotFound()
    for d in hub.firmware_dirs(None):
        path = d / name
        if path.is_file():
            log.info("serving firmware %s to %s", name, request.remote)
            return web.FileResponse(path)
    raise web.HTTPNotFound()


async def voice(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    token = hub.cfg["server"]["token"]
    if token and request.headers.get("Authorization", "") != f"Bearer {token}":
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
    await ws.prepare(request)
    device_id = request.headers.get("Device-Id", request.query.get("device-id", "unknown"))
    client_id = request.headers.get("Client-Id", "")
    log.info("[%s] connected from %s", device_id, request.remote)
    try:
        session = Session(hub, ws, device_id, client_id)
        hub.sessions[device_id] = session
        await session.run()
    finally:
        if hub.sessions.get(device_id) is session:
            del hub.sessions[device_id]
        log.info("[%s] disconnected", device_id)
    return ws


async def status(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    fw = hub.latest_firmware()
    return web.json_response(
        {"name": "tana", "latest_firmware": fw[0] if fw else None, "devices": hub.devices}
    )


async def call_tool(request: web.Request) -> web.Response:
    """POST /api/devices/{id}/tools/{name} with JSON arguments: run an MCP tool on a board.
    GET  /api/devices/{id}/status is a shortcut for self.get_device_status."""
    hub: Hub = request.app["hub"]
    session = hub.sessions.get(request.match_info["id"].lower())
    if session is None:
        raise web.HTTPNotFound(text="device not connected")
    name = request.match_info.get("name", "self.get_device_status")
    args = await request.json() if request.can_read_body else {}
    try:
        result = await session._mcp_request("tools/call", {"name": name, "arguments": args}, timeout=10)
    except Exception as e:
        raise web.HTTPBadGateway(text=str(e))
    texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
    out = []
    for t in texts:
        try:
            out.append(json.loads(t))
        except ValueError:
            out.append(t)
    return web.json_response(out[0] if len(out) == 1 else out)


async def sense_record(request: web.Request) -> web.Response:
    """LAN admin: {"labels": {"<device>": "empty", ...}} starts/relabels recording; {"labels": null} stops."""
    hub: Hub = request.app["hub"]
    body = await request.json()
    path = hub.presence.record(body.get("labels"))
    return web.json_response({"recording": path})


async def stillness(request: web.Request) -> web.Response:
    """LAN admin: GET = each room's detector state; POST {"device", "state": "empty"|"occupied", "note"} =
    ground truth from a person (an "empty" mark gives the detector its empty-room reference)."""
    hub: Hub = request.app["hub"]
    if request.method == "POST":
        body = await request.json()
        if body.get("state") not in ("empty", "occupied") and not body.get("replay"):
            return web.json_response({"error": "state must be empty or occupied"}, status=400)
        if body.get("replay"):
            # warm the detector from a recording (jsonl from /api/sense/record), with marks [[t, state], ...]
            marks = sorted(tuple(m) for m in body.get("marks") or [])
            room = hub.stillness.room(body["device"])
            with open(body["replay"]) as f:
                for line in f:
                    rec = json.loads(line)
                    if rec["device"] != body["device"]:
                        continue
                    while marks and marks[0][0] <= rec["t"]:
                        mt, st = marks.pop(0)
                        room.mark(st, mt)
                    room.feed(rec["t"], rec["r"])
            return web.json_response(hub.stillness.snapshot())
        hub.stillness.mark(body["device"], body["state"], body.get("note", ""))
    return web.json_response(hub.stillness.snapshot())


async def occupancy(request: web.Request) -> web.Response:
    """LAN admin: GET = each room's occupancy; POST {"device", "state": "empty"|"occupied"} = ground truth."""
    hub: Hub = request.app["hub"]
    if request.method == "POST":
        body = await request.json()
        if body.get("state") not in ("empty", "occupied"):
            return web.json_response({"error": "state must be empty or occupied"}, status=400)
        hub.occupancy.mark(body["device"], body["state"])
    return web.json_response(hub.occupancy.snapshot())


async def run_sonar(hub: "Hub", device: str, params: dict) -> dict:
    """Run one sonar capture on a board; returns {"rate", "period", "channels": np.ndarray(ch, n)}."""
    from . import sonar as sonar_mod
    if not hub.cfg["sonar"]["enabled"]:
        raise PermissionError("sonar is disabled (sonar.enabled in config.yaml)")
    session = hub.sessions.get(device)
    if session is None:
        raise LookupError("device not connected")
    hub.sonar_parts[device] = {}
    fut = asyncio.get_running_loop().create_future()
    hub.sonar_waiters[device] = fut
    await session.send_json({"type": "tana", "sonar": params})
    parts = await asyncio.wait_for(fut, params["repeats"] * params["period_ms"] / 1000 + 15)
    if any("error" in p for p in parts):
        raise RuntimeError(parts[0].get("error"))
    chans = np.array([sonar_mod.decode(p) for p in parts])
    return {"rate": parts[0]["rate"], "period": parts[0]["period"], "channels": chans}


async def sonar(request: web.Request) -> web.Response:
    """LAN admin: run the board's ultrasonic sonar and analyse the echoes (first channel).
    Body: {"f0":19000,"f1":21000,"chirp_ms":5,"period_ms":40,"repeats":40,"amplitude":16000,
           "all_channels": false, "with_noise": true}"""
    from . import sonar as sonar_mod
    hub: Hub = request.app["hub"]
    device = request.match_info["id"].lower()
    params = {"f0": 19000, "f1": 21000, "chirp_ms": 5, "period_ms": 40, "repeats": 40, "amplitude": 16000}
    body = await request.json() if request.can_read_body else {}
    params.update({k: int(v) for k, v in body.items() if k in params})
    params["all_channels"] = bool(body.get("all_channels"))
    try:
        noise = None
        if body.get("with_noise", True):
            noise = (await run_sonar(hub, device, {**params, "amplitude": 0}))["channels"][0]
            await asyncio.sleep(0.3)
        res = await run_sonar(hub, device, params)
    except PermissionError as e:
        raise web.HTTPForbidden(text=str(e))
    except LookupError:
        raise web.HTTPNotFound(text="device not connected")
    except (RuntimeError, asyncio.TimeoutError) as e:
        raise web.HTTPConflict(text=f"sonar failed: {e}")
    chans = res["channels"]
    out = sonar_mod.analyse(chans[0], res["rate"], params, noise)
    out.update({"rate": res["rate"], "params": params, "channels": len(chans),
                "rms_per_channel": [round(float(np.sqrt(np.mean(c ** 2))), 1) for c in chans]})
    np.save(hub.data / f"sonar-{device[-5:].replace(':', '')}-{int(time.time())}.npy", chans)
    return web.json_response(out)


async def send_to_device(request: web.Request) -> web.Response:
    """LAN admin: send a raw JSON message to a board, e.g. {"type":"tana","csi_stream":{...}}."""
    hub: Hub = request.app["hub"]
    session = hub.sessions.get(request.match_info["id"].lower())
    if session is None:
        raise web.HTTPNotFound(text="device not connected")
    await session.send_json(await request.json())
    return web.json_response({"ok": True})


async def ruview_status(request: web.Request) -> web.Response:
    """LAN admin: RuView-algorithm view of each node streaming raw CSI; ?record=<label> or ?stop=1."""
    hub: Hub = request.app["hub"]
    rv = hub.ruview
    if rv is None:
        raise web.HTTPServiceUnavailable(text="listener not running")
    if "record" in request.query:
        return web.json_response({"recording": rv.record(request.query["record"])})
    if "stop" in request.query:
        return web.json_response({"recording": rv.record(None)})
    return web.json_response([n.snapshot() for n in rv.nodes.values()])


async def probe_targets(request: web.Request) -> web.Response:
    """LAN admin: every sensing board pings/overhears the given targets and reports their RSSI.
    Body: {"targets": [{"ip": "...", "mac": "aa:bb:..", "name": "..."}], "seconds": 4}"""
    hub: Hub = request.app["hub"]
    body = await request.json()
    targets = body["targets"][:8]
    seconds = int(body.get("seconds", 4))
    boards = {d: s for d, s in hub.sessions.items() if getattr(s, "ambient", False)}
    hub.presence.probe_results = {}
    for d, s in boards.items():
        # a board can't overhear itself; leave it out of its own target list
        own = [t for t in targets if t["mac"].lower() != d.lower()]
        await s.send_json({"type": "tana", "probe": {"seconds": seconds,
                                                     "targets": [{"ip": t["ip"], "mac": t["mac"].lower()} for t in own]}})
    deadline = time.time() + seconds + 6
    while time.time() < deadline and len(hub.presence.probe_results) < len(boards):
        await asyncio.sleep(0.5)
    names = {t["mac"].lower(): t.get("name", t["ip"]) for t in targets}
    out = {d: {names.get(m, m): r for m, r in res.items()} for d, res in hub.presence.probe_results.items()}
    return web.json_response({"t": time.time(), "results": out, "no_reply": sorted(set(boards) - set(out))})


async def reboot_device(request: web.Request) -> web.Response:
    """LAN admin: restart a board (it checks for firmware on boot)."""
    hub: Hub = request.app["hub"]
    session = hub.sessions.get(request.match_info["id"].lower())
    if session is None:
        raise web.HTTPNotFound(text="device not connected")
    await session.send_json({"type": "system", "command": "reboot"})
    return web.json_response({"ok": True})


def build_app(cfg: dict) -> web.Application:
    app = web.Application()
    app["hub"] = Hub(cfg)
    app.router.add_route("*", "/tana/ota/", ota)
    app.router.add_route("*", "/xiaozhi/ota/", ota)  # path used by stock firmware
    app.router.add_get("/firmware/{name}", firmware)
    app.router.add_get("/tana/v1/", voice)
    app.router.add_get("/", status)
    app.router.add_get("/api/devices/{id}/status", call_tool)
    app.router.add_post("/api/devices/{id}/tools/{name}", call_tool)
    app.router.add_post("/api/devices/{id}/reboot", reboot_device)
    app.router.add_post("/api/devices/{id}/send", send_to_device)
    app.router.add_get("/api/ruview", ruview_status)
    app.router.add_post("/api/probe", probe_targets)
    app.router.add_post("/api/sense/record", sense_record)
    app.router.add_get("/api/stillness", stillness)
    app.router.add_get("/api/occupancy", occupancy)
    app.router.add_post("/api/occupancy", occupancy)
    app.router.add_post("/api/stillness", stillness)
    app.router.add_post("/api/devices/{id}/sonar", sonar)
    app.router.add_get("/api/rooms", rooms)
    app.router.add_get("/api/boards", boards)
    app.router.add_get("/metrics", metrics_page)
    app.router.add_post("/api/boards/{id}", set_board)

    async def cleanup(app):
        if app["hub"].assistant:
            await app["hub"].assistant.close()

    app.on_cleanup.append(cleanup)
    return app


async def rooms(request: web.Request) -> web.Response:
    """Rooms and their lights, for other local apps (the web app's boards screen)."""
    hub: Hub = request.app["hub"]
    await hub.lights.refresh()
    p = hub.presence
    assigned = {n for r in p.rooms.values() for n in r.lights}
    return web.json_response({
        "rooms": [r.snapshot(p.cfg, hub.lights) for r in p.rooms.values()],
        "lights": [{"name": n, "ip": d["ip"], "on": (hub.lights.state.get(n) or {}).get("on"),
                    "room": next((r.name for r in p.rooms.values() if n in r.lights), None)}
                   for n, d in hub.lights.devices.items()],
        "unassigned": sorted(set(hub.lights.devices) - assigned),
        "auto_lights": p.cfg["auto_lights"],
        "located": p.last_locate,
    })


async def boards(request: web.Request) -> web.Response:
    """Every board: name, connection, firmware and live status (volume, brightness...), for other local apps."""
    hub: Hub = request.app["hub"]

    async def status(did: str):
        s = hub.sessions.get(did)
        if s is None:
            return None
        try:
            res = await s._mcp_request("tools/call", {"name": "self.get_device_status", "arguments": {}}, 3)
            return json.loads(res["content"][0]["text"])
        except Exception:
            return None

    ids = sorted(set(hub.devices) | set(hub.sessions) | set(hub.device_names))
    stats = await asyncio.gather(*(status(d) for d in ids))
    return web.json_response([
        {"id": d, "name": hub.device_names.get(d) or (hub.presence.rooms.get(d).name if hub.presence.rooms.get(d) else ""),
         "connected": d in hub.sessions, "awake": getattr(hub.sessions.get(d), "awake", False), "status": st,
         "version": hub.devices.get(d, {}).get("version"), "chat": "board-" + re.sub(r"[^\w.-]", "-", d)}
        for d, st in zip(ids, stats)
    ])


async def set_board(request: web.Request) -> web.Response:
    """POST {"volume": 0-100, "brightness": 0-100} to a connected board."""
    hub: Hub = request.app["hub"]
    s = hub.sessions.get(request.match_info["id"])
    if s is None:
        raise web.HTTPNotFound(text="board not connected")
    body = await request.json()
    calls = []
    if "volume" in body:
        calls.append(("self.audio_speaker.set_volume", {"volume": max(0, min(100, int(body["volume"])))}))
    if "brightness" in body:
        calls.append(("self.screen.set_brightness", {"brightness": max(0, min(100, int(body["brightness"])))}))
    for name, args in calls:
        await s._mcp_request("tools/call", {"name": name, "arguments": args}, 5)
    return web.json_response({"ok": True})


async def metrics_page(request: web.Request) -> web.Response:
    """GET /metrics: this process's counters plus the boards, rooms and lights now."""
    hub: Hub = request.app["hub"]
    rooms = hub.presence.rooms.values()
    text = metrics.render([
        ("tana_boards_up", "1 while tana-boards runs, with its assistant backend.",
         [({"backend": hub.assistant.name if hub.assistant else "none"}, 1)]),
        ("tana_boards_connected", "Boards connected now.", [({}, len(hub.sessions))]),
        ("tana_boards_awake", "Boards awake (listening for a follow-up) now.",
         [({}, sum(1 for s in hub.sessions.values() if getattr(s, "awake", False) and getattr(s, "ambient", False)))]),
        ("tana_boards_room_occupied", "Whether a room is occupied, by room.",
         [({"room": r.name}, int(bool(r.occupied))) for r in rooms]),
        ("tana_boards_light_on", "Whether a light is on (1), off (0), by light; unknown lights are left out.",
         [({"light": n}, int(st["on"])) for n, st in hub.lights.state.items() if isinstance(st.get("on"), bool)]),
    ])
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


async def serve(cfg: dict):
    device_app = build_app(cfg)
    hub = device_app["hub"]
    runners = []
    for app, host, port in ((device_app, cfg["server"]["host"], cfg["server"]["port"]),):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    from . import ruview
    hub.ruview = await ruview.start(cfg["ruview"]["port"], hub.data / "recordings")
    asyncio.create_task(hub.presence.tick())
    # keep a sensing recording running across restarts and reboots (small: ~25 MB/day), for tuning
    hub.presence.record({"*": "auto"})
    asyncio.create_task(hub.occupancy_lights())
    asyncio.create_task(hub.fallback_kits())
    asyncio.create_task(hub.light_schedules())
    asyncio.create_task(hub.lights.refresh(force=True))
    log.info("boards' AI: %s", f"assistant at {cfg['assistant']['url']}" if hub.assistant else "none configured (assistant.url)")
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="tana-boards: the server for Tana's voice boards")
    parser.add_argument("-c", "--config", default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
    )
    cfg = config_mod.load(args.config)
    log.info("devices should use OTA URL http://%s:%s/tana/ota/", cfg["server"]["public_host"], cfg["server"]["port"])
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
