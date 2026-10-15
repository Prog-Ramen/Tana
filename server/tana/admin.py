"""tana-admin: maintenance commands for the web app's login."""
import argparse
from pathlib import Path

from . import config as config_mod
from .store import Store


def main():
    ap = argparse.ArgumentParser(description="Tana web app administration")
    ap.add_argument("-c", "--config", default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset-auth", help="forget the password and authenticator; re-run /setup on the LAN")
    sub.add_parser("logout-all", help="sign out every browser (e.g. after losing a device)")
    args = ap.parse_args()
    store = Store(config_mod.load(args.config)["web"]["db"])
    if args.cmd == "reset-auth":
        for key in ("auth.password", "auth.totp", "auth.totp_last"):
            store.set(key, None)
        store.delete_all_sessions()
        print("Login reset. Open http://<this machine>:8003/setup from your home network.")
    elif args.cmd == "logout-all":
        store.delete_all_sessions()
        print("All browsers signed out.")


if __name__ == "__main__":
    main()
