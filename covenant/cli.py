"""Connect once, choose agents on the website."""

import argparse
import logging
from pathlib import Path
from .catalog import initialize, discover
from .supervisor import Supervisor

DEFAULT_SERVER = "https://crown-and-covenant-flame.vercel.app"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Crown & Covenant agent client")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init", help="Create example agent folders without overwriting your code"
    )
    init.add_argument("--directory", default=".")
    listing = sub.add_parser("agents", help="Inspect discovered agent definitions")
    listing.add_argument("--directory", default=".")
    connect = sub.add_parser(
        "connect",
        aliases=["host"],
        help="Connect this client; choose agents in website lobbies",
    )
    connect.add_argument("--server", default=DEFAULT_SERVER)
    connect.add_argument("--directory", default=".")
    connect.add_argument("--label", default="My PC")
    connect.add_argument("--pair-again", action="store_true")
    connect.add_argument(
        "--fast",
        action="store_true",
        help="Allow examples to mark ready early for testing",
    )
    connect.add_argument("--max-seconds", type=float)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "init":
        print(
            f"Agent definitions: {initialize(args.directory)}\nEdit their code/configuration, then run covenant connect."
        )
        return 0
    if args.command == "agents":
        for d in discover(Path(args.directory) / "agents"):
            print(
                f"{d.public['id']}: {d.public['name']} · {d.public.get('error','Ready')} · {d.public['version'][:12]}"
            )
        return 0
    try:
        Supervisor(
            args.server, args.directory, args.label, args.fast, args.pair_again
        ).run(args.max_seconds)
    except KeyboardInterrupt:
        print("Client stopped. Instance workspaces are preserved.")
        return 0
    except Exception as e:
        print(str(e))
        return 1
    return 0
