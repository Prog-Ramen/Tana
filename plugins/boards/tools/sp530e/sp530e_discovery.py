
import argparse
import json
import socket
import time
from pathlib import Path

# Discovery packet and UDP port used by UniLED
DISCOVERY_PACKET = bytes.fromhex(
    "53704e65740000200000000002e0"
)
DISCOVERY_PORT = 6454

# SP530E model code
SP530E_CODE = 0x4E


def discover(broadcast="255.255.255.255",
             local_ip="0.0.0.0",
             duration=10):

    devices = {}

    with socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    ) as sock:

        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_BROADCAST,
            1
        )

        sock.bind((local_ip, DISCOVERY_PORT))

        deadline = time.monotonic() + duration
        next_probe = 0

        print("Searching for SP530E controllers...")

        while time.monotonic() < deadline:

            now = time.monotonic()

            if now >= next_probe:
                sock.sendto(
                    DISCOVERY_PACKET,
                    (broadcast, DISCOVERY_PORT)
                )
                next_probe = now + 1

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            sock.settimeout(min(0.5, remaining))

            try:
                data, address = sock.recvfrom(1024)
            except socket.timeout:
                continue

            # Validate the discovery response
            if len(data) < 26:
                continue

            if not data.startswith(b"SpNet"):
                continue

            # Discovery response type
            if data[7] != 0x21:
                continue

            # Model identification
            model_code = data[17]

            if model_code != SP530E_CODE:
                continue

            ip = address[0]

            mac = ":".join(
                f"{byte:02X}"
                for byte in data[19:25]
            )

            name_length = data[25]

            if len(data) < 26 + name_length:
                continue

            name = data[
                26:26 + name_length
            ].decode(
                "utf-8", errors="replace"
            ).strip("\x00")

            if not name:
                name = "SP530E"

            device = {
                "name": name,
                "model": "SP530E",
                "ip": ip,
                "mac": mac,
                "discovery_port": DISCOVERY_PORT,
            }

            if ip not in devices:
                devices[ip] = device

                print(
                    f"Found: {name} | "
                    f"IP: {ip} | MAC: {mac}"
                )

    return list(devices.values())


def main():
    parser = argparse.ArgumentParser(
        description="Discover SP530E LED controllers"
    )

    parser.add_argument(
        "--broadcast",
        default="255.255.255.255",
        help="Network broadcast address"
    )

    parser.add_argument(
        "--local-ip",
        default="0.0.0.0",
        help="Local network interface IP"
    )

    args = parser.parse_args()

    devices = discover(
        broadcast=args.broadcast,
        local_ip=args.local_ip
    )

    if not devices:
        print("\nNo SP530E controllers found.")
        print("Check Wi-Fi and local network access.")
        return

    config = Path("sp530e_devices.json")

    config.write_text(
        json.dumps(
            {"devices": devices},
            indent=2
        ),
        encoding="utf-8"
    )

    print(f"\nFound {len(devices)} controller(s).")
    print(f"Configuration saved to {config.resolve()}")


if __name__ == "__main__":
    main()
