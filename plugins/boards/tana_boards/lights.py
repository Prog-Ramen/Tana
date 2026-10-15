"""SP530E LED controllers over Wi-Fi (BanlanX "SPTECH" protocol, TCP 8587; UDP 6454 discovery).

Protocol from sp530e/sp530e_control.py and sp530e_discovery.py:
  command: b"SPTECH\\0" + [cmd, 0, 0, 0] + len(payload) as 2 bytes big-endian + payload
  0x02 status, 0x50 power [0|1], 0x51 brightness [channel(0 colour, 1 white), level],
  0x52 colour [r, g, b, brightness], 0x53 mode [1, 1] = static colour.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time

log = logging.getLogger("tana.lights")

PORT = 8587
DISCOVERY_PORT = 6454
MAGIC = b"SPTECH\x00"
DISCOVERY_PACKET = bytes.fromhex("53704e65740000200000000002e0")
SP530E_CODE = 0x4E


def _packet(command: int, payload: bytes = b"") -> bytes:
    return MAGIC + bytes([command, 0, 0, 0]) + len(payload).to_bytes(2, "big") + payload


def discover(duration: float = 3.0) -> dict[str, dict]:
    """Broadcast for SP530E controllers; returns {name: {"ip", "mac"}}."""
    found: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        deadline = time.monotonic() + duration
        next_probe = 0.0
        while (now := time.monotonic()) < deadline:
            if now >= next_probe:
                sock.sendto(DISCOVERY_PACKET, ("255.255.255.255", DISCOVERY_PORT))
                next_probe = now + 1
            sock.settimeout(max(0.05, min(0.5, deadline - now)))
            try:
                data, (ip, _) = sock.recvfrom(1024)
            except socket.timeout:
                continue
            if len(data) < 26 or not data.startswith(b"SpNet") or data[7] != 0x21 or data[17] != SP530E_CODE:
                continue
            n = data[25]
            name = data[26:26 + n].decode("utf-8", "replace").strip("\x00") or ip
            found[name] = {"ip": ip, "mac": ":".join(f"{b:02X}" for b in data[19:25])}
    return found


class Lights:
    """Named SP530E controllers. Remembers what Tana last set, since that's what the rules need."""

    def __init__(self):
        self.devices: dict[str, dict] = {}  # name -> {"ip", "mac"}
        self.state: dict[str, dict] = {}    # name -> {"on": bool, "changed": ts, "by": "auto"|"voice"|"app"}
        self.locks: dict[str, asyncio.Lock] = {}
        self.last_discovery = 0.0

    def wifi_macs(self) -> dict[str, str]:
        """Light name -> Wi-Fi MAC (from this machine's ARP table; differs from the BLE MAC)."""
        arp = {}
        try:
            for line in open("/proc/net/arp").read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    arp[parts[0]] = parts[3].lower()
        except OSError:
            pass
        return {n: arp[d["ip"]] for n, d in self.devices.items() if d["ip"] in arp}

    async def refresh(self, force: bool = False):
        if not force and time.monotonic() - self.last_discovery < 600 and self.devices:
            return
        found = await asyncio.get_running_loop().run_in_executor(None, discover)
        if found:
            self.devices.update(found)
        self.last_discovery = time.monotonic()
        log.info("lights: %s", ", ".join(f"{n} ({d['ip']})" for n, d in self.devices.items()) or "none found")

    def resolve(self, name: str) -> str | None:
        """Match a spoken or configured name ("living room" -> "LivingRoom")."""
        key = name.replace(" ", "").lower()
        for n in self.devices:
            if n.replace(" ", "").lower() == key:
                return n
        for n in self.devices:
            if key in n.replace(" ", "").lower():
                return n
        return None

    async def _send(self, name: str, packets: list[bytes]):
        dev = self.devices.get(name)
        if dev is None:
            await self.refresh(force=True)
            dev = self.devices.get(name)
            if dev is None:
                raise LookupError(f"no light called {name}")
        lock = self.locks.setdefault(name, asyncio.Lock())
        async with lock:  # the controller takes one connection at a time
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(dev["ip"], PORT), 4)
            except (OSError, asyncio.TimeoutError):
                await self.refresh(force=True)  # DHCP may have moved it
                dev = self.devices[name]
                reader, writer = await asyncio.wait_for(asyncio.open_connection(dev["ip"], PORT), 4)
            try:
                for p in packets:
                    writer.write(p)
                    await writer.drain()
                    await asyncio.sleep(0.3)  # the controller needs time between commands
            finally:
                writer.close()

    def _mark(self, name: str, on: bool, by: str):
        self.state[name] = {"on": on, "changed": time.time(), "by": by}

    async def power(self, name: str, on: bool, by: str = "app"):
        await self._send(name, [_packet(0x50, b"\x01" if on else b"\x00")])
        self._mark(name, on, by)
        from tana import metrics
        metrics.inc("tana_boards_light_switches_total", by=by)
        log.info("light %s %s (%s)", name, "on" if on else "off", by)

    async def brightness(self, name: str, level: int, by: str = "app"):
        level = max(0, min(255, int(level)))
        await self._send(name, [_packet(0x50, b"\x01"), _packet(0x51, bytes([0, level]))])
        self._mark(name, True, by)

    async def color(self, name: str, r: int, g: int, b: int, level: int = 200, by: str = "app"):
        vals = bytes(max(0, min(255, int(v))) for v in (r, g, b, level))
        await self._send(name, [_packet(0x50, b"\x01"), _packet(0x53, bytes([1, 1])), _packet(0x52, vals)])
        self._mark(name, True, by)
