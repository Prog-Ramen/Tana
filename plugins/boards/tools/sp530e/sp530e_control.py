
import argparse
import json
import socket
import time
from pathlib import Path

PORT = 8587
MAGIC = b"SPTECH\x00"


def get_device_ip(ip=None):
    """Load the IP address saved by the discovery script."""
    if ip:
        return ip

    config = json.loads(
        Path("sp530e_devices.json").read_text(
            encoding="utf-8"
        )
    )

    devices = config["devices"]

    if len(devices) != 1:
        raise ValueError(
            "Specify --ip when multiple devices are found."
        )

    return devices[0]["ip"]


class SP530E:
    def __init__(self, ip):
        self.ip = ip
        self.sock = None

    def __enter__(self):
        self.sock = socket.create_connection(
            (self.ip, PORT),
            timeout=5
        )
        self.sock.settimeout(5)
        return self

    def __exit__(self, *args):
        if self.sock:
            self.sock.close()

    def send(self, command, payload=b""):
        """Encode and send an SPTech network command."""

        packet = (
            MAGIC
            + bytes([command, 0, 0, 0])
            + len(payload).to_bytes(2, "big")
            + payload
        )

        self.sock.sendall(packet)

        print(f"Sent: {packet.hex(' ')}")

        # Give the controller time to process the command.
        time.sleep(0.3)

    def receive_exact(self, length):
        data = bytearray()

        while len(data) < length:
            chunk = self.sock.recv(length - len(data))

            if not chunk:
                raise ConnectionError(
                    "Controller closed the connection."
                )

            data.extend(chunk)

        return bytes(data)

    def status(self):
        """Query the controller's current state."""

        self.send(0x02)

        header = self.receive_exact(13)

        if not header.startswith(MAGIC):
            raise ValueError(
                f"Unexpected response: {header.hex()}"
            )

        length = int.from_bytes(
            header[11:13], "big"
        )

        if length > 8192:
            raise ValueError("Unexpected response length")

        payload = self.receive_exact(length)

        print("Connected to SP530E")
        print("Status response:", payload.hex(" "))

        return payload

    def on(self):
        self.send(0x50, b"\x01")

    def off(self):
        self.send(0x50, b"\x00")

    def color(self, red, green, blue, brightness=128):
        values = (red, green, blue, brightness)

        if not all(0 <= v <= 255 for v in values):
            raise ValueError("RGB and brightness must be 0-255")

        # Turn on
        self.on()

        # Select static RGB colour mode and solid effect
        self.send(0x53, bytes([0x01, 0x01]))

        # Set RGB values and brightness
        self.send(
            0x52,
            bytes([red, green, blue, brightness])
        )

    def brightness(self, level, white=False):
        if not 0 <= level <= 255:
            raise ValueError("Brightness must be 0-255")

        # 0 = colour channel; 1 = white channel
        channel = 0x01 if white else 0x00

        self.send(0x51, bytes([channel, level]))


def main():
    parser = argparse.ArgumentParser(
        description="Control SP530E using original firmware"
    )

    parser.add_argument(
        "--ip",
        help="SP530E IP address; otherwise use discovery JSON"
    )

    sub = parser.add_subparsers(
        dest="command", required=True
    )

    sub.add_parser("status")
    sub.add_parser("on")
    sub.add_parser("off")

    color_parser = sub.add_parser("color")

    color_parser.add_argument("red", type=int)
    color_parser.add_argument("green", type=int)
    color_parser.add_argument("blue", type=int)

    color_parser.add_argument(
        "--brightness",
        type=int,
        default=128
    )

    brightness_parser = sub.add_parser("brightness")

    brightness_parser.add_argument(
        "level", type=int
    )

    brightness_parser.add_argument(
        "--white",
        action="store_true",
        help="Adjust white-channel brightness"
    )

    args = parser.parse_args()

    ip = get_device_ip(args.ip)

    print(f"Connecting to SP530E at {ip}:{PORT}")

    with SP530E(ip) as led:

        if args.command == "status":
            led.status()

        elif args.command == "on":
            led.on()

        elif args.command == "off":
            led.off()

        elif args.command == "color":
            led.color(
                args.red,
                args.green,
                args.blue,
                args.brightness
            )

        elif args.command == "brightness":
            led.brightness(
                args.level,
                args.white
            )

    print("Command completed.")


if __name__ == "__main__":
    main()
