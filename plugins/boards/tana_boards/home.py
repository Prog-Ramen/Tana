"""Home-control tools the LLM can call (lights, room presence). Shared by boards and the web app."""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import Hub

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_light",
            "description": "Turn a light on or off, optionally setting brightness (0-100) or a colour. "
                           "Use the light's name, e.g. 'Fireplace' or 'LivingRoom', or 'all'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "light": {"type": "string"},
                    "on": {"type": "boolean"},
                    "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
                    "color": {"type": "string", "description": "colour name or hex like #ff8800"},
                },
                "required": ["light", "on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "home_status",
            "description": "Describe the home: each room (its board), its lights, whether someone seems to be "
                           "there and why, whether it's calibrated, and the automatic-lights settings.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_room",
            "description": "Change a room's settings. room: a room name, or 'this room' for the room this "
                           "board is in. You can rename it, put a light in it or take one out, and turn "
                           "automatic lights on or off for it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {"type": "string"},
                    "rename_to": {"type": "string"},
                    "add_light": {"type": "string"},
                    "remove_light": {"type": "string"},
                    "auto_lights": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calibrate_room",
            "description": "Teach a room what it looks like empty, so Tana can tell when someone is sitting "
                           "still there. Tell the user to leave the room now: recording starts in 20 seconds "
                           "and lasts 2 minutes.",
            "parameters": {"type": "object", "properties": {"room": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_nearest_lights",
            "description": "Have every board measure each light's signal and put each light in the room "
                           "whose board hears it best (lights placed by hand stay where they are).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_automation",
            "description": "Change how automatic lights behave everywhere: hold_minutes = how long lights "
                           "stay on after a room looks empty; hours = when lights may switch on by "
                           "themselves: 'after dark' (sunset to sunrise), fixed hours like '18:00-07:00', "
                           "or 'always'; enabled = automatic lights at all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hold_minutes": {"type": "integer", "minimum": 1, "maximum": 240},
                    "hours": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
            },
        },
    },
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "white": (255, 255, 255),
    "warm white": (255, 170, 80), "warm": (255, 170, 80), "orange": (255, 120, 0), "yellow": (255, 210, 0),
    "purple": (150, 0, 255), "pink": (255, 60, 150), "cyan": (0, 255, 255), "teal": (0, 160, 140),
}


def _rgb(color: str) -> tuple[int, int, int] | None:
    c = color.strip().lower()
    if c in COLORS:
        return COLORS[c]
    c = c.lstrip("#")
    if len(c) == 6:
        try:
            return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return None
    return None


async def run(hub: "Hub", name: str, args: dict, here: str | None = None) -> str:
    """Run a home tool. `here` is the board the user is talking to (for "this room")."""
    lights, presence = hub.lights, hub.presence
    await lights.refresh()

    def find_room(arg) -> "tuple[object | None, str | None]":
        room = presence.resolve_room(arg, here)
        if room is None:
            names = ", ".join(r.name for r in presence.rooms.values())
            what = "which room you mean" if not arg or arg.lower() in ("this room", "here") else f"a room called {arg}"
            return None, f"I don't know {what}. Rooms: {names}."
        return room, None

    if name == "home_status":
        now = time.time()
        rooms = {}
        for r in presence.rooms.values():
            sensing = now - r.last_report < 5
            rooms[r.name] = {
                "this_room": r.device_id == here,
                "lights": r.lights,
                "someone_there": r.occupied if sensing else "unknown (board not sensing)",
                "why": r.last_reason if r.occupied else "",
                "calibrated_for_seated_people": bool(r.empty_shape),
                "automatic_lights": r.auto,
            }
        return json.dumps({
            "rooms": rooms,
            "lights_on": {n: (lights.state.get(n) or {}).get("on") for n in lights.devices},
            "lights_not_in_any_room": sorted(set(lights.devices) - {l for r in presence.rooms.values() for l in r.lights}),
            "automation": {
                "enabled": presence.cfg["auto_lights"], "hold_minutes": presence.cfg["hold_minutes"],
                "when": "-".join(presence.cfg["only_between"]) if presence.cfg.get("only_between")
                else ("after dark (sunset to sunrise in " + presence.cfg["location"]["name"] + ")"
                      if presence.cfg.get("dark_only") else "any time"),
                "sunset_today": presence.sun_times()[1].strftime("%H:%M"),
                "dark_now": presence.is_dark(),
                "switches_on_by": "movement (voices only keep lights on)",
            },
        })

    if name == "set_room":
        room, err = find_room(args.get("room"))
        if err:
            return err
        done = []
        if args.get("rename_to"):
            room.name = str(args["rename_to"]).strip()[:40]
            done.append(f"renamed to {room.name}")
        for key, into in (("add_light", room), ("remove_light", None)):
            if args.get(key):
                light = lights.resolve(str(args[key]))
                if light is None:
                    return f"No light called {args[key]}. Lights: {', '.join(lights.devices)}."
                presence.assign_light(light, into)
                done.append(f"{light} {'added' if into else 'removed'}")
        if args.get("auto_lights") is not None:
            room.auto = bool(args["auto_lights"])
            done.append(f"automatic lights {'on' if room.auto else 'off'}")
        presence.save_settings()
        return f"{room.name}: {', '.join(done) or 'nothing changed'}. Lights now: {', '.join(room.lights) or 'none'}."

    if name == "calibrate_room":
        room, err = find_room(args.get("room"))
        if err:
            return err
        if time.time() - room.last_report < 5:
            presence.start_calibration(room.device_id, seconds=120, delay=20)
            return (f"Calibrating {room.name}: recording starts in 20 seconds and takes 2 minutes. "
                    "Ask the user to leave the room now and stay out until it's done.")
        return f"{room.name}'s board isn't sending sensing data, so I can't calibrate it."

    if name == "find_nearest_lights":
        result = await presence.locate(hub.sessions)
        if result.get("error"):
            return result["error"]
        names = {r.device_id: r.name for r in presence.rooms.values()}
        placed = {light: names.get(d, d) for light, d in presence_assignment(presence).items()}
        return json.dumps({"lights_by_room": placed,
                           "signal_dbm": {l: {names.get(d, d): v for d, v in rs.items()} for l, rs in result["rssi"].items()}})

    if name == "set_automation":
        changed = []
        if args.get("hold_minutes"):
            presence.cfg["hold_minutes"] = int(args["hold_minutes"])
            changed.append(f"lights stay on {presence.cfg['hold_minutes']} minutes after a room empties")
        if args.get("hours"):
            h = str(args["hours"]).strip().lower()
            m = re.fullmatch(r"(\d{1,2}):?(\d{2})?\s*-\s*(\d{1,2}):?(\d{2})?", h)
            if h in ("always", "any time", "anytime", "all day"):
                presence.cfg["only_between"] = None
                presence.cfg["dark_only"] = False
                changed.append("automatic lights any time")
            elif any(w in h for w in ("dark", "sunset", "night", "dusk")):
                presence.cfg["only_between"] = None
                presence.cfg["dark_only"] = True
                changed.append(f"automatic lights after dark (sunset today {presence.sun_times()[1].strftime('%H:%M')})")
            elif m:
                a = f"{int(m[1]):02d}:{m[2] or '00'}"
                b = f"{int(m[3]):02d}:{m[4] or '00'}"
                presence.cfg["only_between"] = [a, b]
                changed.append(f"automatic lights only between {a} and {b}")
            else:
                return "Give hours like 18:00-07:00, or 'always'."
        if args.get("enabled") is not None:
            presence.cfg["auto_lights"] = bool(args["enabled"])
            changed.append(f"automatic lights {'on' if presence.cfg['auto_lights'] else 'off'}")
        presence.save_settings()
        return "; ".join(changed) or "nothing changed"

    if name == "set_light":
        targets = list(lights.devices) if str(args.get("light", "")).lower() in ("all", "every", "everything") \
            else [lights.resolve(str(args.get("light", "")))]
        if (not targets or targets[0] is None) and here:  # "turn the light on" on a board: its room's lights
            room = presence.rooms.get(here)
            if room and room.lights and str(args.get("light", "")).lower() in ("", "light", "lights", "the light", "the lights", "this room", "here"):
                targets = list(room.lights)
        if not targets or targets[0] is None:
            return f"No light called {args.get('light')}. Lights: {', '.join(lights.devices) or 'none found'}."
        on = bool(args.get("on", True))
        done = []
        for t in targets:
            if not on:
                await lights.power(t, False, by="voice")
            elif args.get("color") and (rgb := _rgb(str(args["color"]))):
                level = round(int(args.get("brightness", 80)) * 2.55)
                await lights.color(t, *rgb, level=level, by="voice")
            elif args.get("brightness") is not None:
                await lights.brightness(t, round(int(args["brightness"]) * 2.55), by="voice")
            else:
                await lights.power(t, True, by="voice")
            presence.manual(t, on)
            done.append(t)
        return f"{', '.join(done)} turned {'on' if on else 'off'}."
    return f"Unknown tool {name}"


def presence_assignment(presence) -> dict:
    return {l: r.device_id for r in presence.rooms.values() for l in r.lights}
