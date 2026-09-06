from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from .transport import Client, Connection, ProtocolError
from .agent import ReferenceAgent
from .harness import CodexStrategist
from .runner import FileAgent, Runner
from .custom import IsolatedAgent
from .host import Host, Worker


def main(argv=None):
    parser = argparse.ArgumentParser(description="Crown & Covenant local agent runner")
    sub = parser.add_subparsers(dest="command", required=True)
    host = sub.add_parser(
        "host", help="Pair this PC once and supply three private Luna opponents"
    )
    host.add_argument("--server", default="https://crown-and-covenant-flame.vercel.app")
    host.add_argument("--state-dir", default=".covenant")
    host.add_argument("--label", default="My PC")
    host.add_argument(
        "--pair-again",
        action="store_true",
        help="Recover browser pairing using the same Google owner account",
    )
    host.add_argument(
        "--fast",
        action="store_true",
        help="Resolve early when everyone is ready (testing)",
    )
    host.add_argument("--max-seconds", type=float)
    host.add_argument("--model-timeout", type=float, default=90)
    run = sub.add_parser("run", help="Control a kingdom from this computer")
    run.add_argument(
        "--connection",
        required=True,
        action="append",
        help="Private connection.json downloaded from the website",
    )
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--agent", help="Import a custom agent as module:ClassName")
    mode.add_argument(
        "--files", help="Enable the file interface inside this kingdom’s private folder"
    )
    mode.add_argument(
        "--codex",
        action="store_true",
        help="Use the local Codex login for a strategic model",
    )
    run.add_argument("--model", default="gpt-5.6-luna")
    run.add_argument("--poll", type=float, default=3)
    run.add_argument(
        "--fast",
        action="store_true",
        help="Allow model agents to resolve early when all ready",
    )
    run.add_argument("--max-turns", type=int)
    run.add_argument("--max-seconds", type=float)
    run.add_argument("--state-dir", default=".covenant")
    run.add_argument(
        "--timeout",
        type=float,
        default=90,
        help="Reasoning watchdog timeout in seconds",
    )
    state = sub.add_parser("state", help="Print your current observation as JSON")
    state.add_argument("--connection", required=True)
    join = sub.add_parser(
        "join", help="Show the signed-in website flow for joining a game"
    )
    join.add_argument("--server", required=True)
    join.add_argument("--game", required=True)
    join.add_argument("--name", required=True)
    join.add_argument("--output", default="connection.json")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    agent = None
    runner = None
    try:
        if args.command == "host":
            Host(
                args.server,
                args.state_dir,
                args.label,
                args.fast,
                model_timeout=args.model_timeout,
                pair_again=args.pair_again,
            ).run(args.max_seconds)
            return 0
        if args.command == "join":
            print(
                f"Open {args.server.rstrip('/')}/?join={args.game.upper()}, sign in with Google, and download your seat's connection.json. Then run covenant run --connection connection.json."
            )
            return 0
        if args.command == "run":
            if args.codex and args.model != "gpt-5.6-luna":
                raise ValueError(
                    "This release supports gpt-5.6-luna through the local Codex login."
                )
            import time

            workers = []
            options = {
                "agent": args.agent,
                "files": bool(args.files),
                "codex": args.codex,
                "poll": args.poll,
                "fast": args.fast,
                "max_turns": args.max_turns,
                "max_seconds": args.max_seconds,
                "model_timeout": args.timeout,
            }
            sys.path.insert(0, str(Path.cwd()))
            try:
                for filename in args.connection:
                    c = Connection.load(filename)
                    worker = Worker(c, str(Path(args.state_dir).resolve()), options)
                    worker.start()
                    workers.append(worker)
                    print(
                        f"{c.gameId}/{c.playerId}: independent worker. Game files: {worker.root}",
                        flush=True,
                    )
                    if args.files:
                        print(f"File interface: {worker.root / 'files'}", flush=True)
                started = time.monotonic()
                while True:
                    for w in workers:
                        if (w.process.is_alive() or w.process.exitcode) and (
                            args.max_seconds is None
                            or time.monotonic() - started < args.max_seconds
                        ):
                            w.maintain()
                    if not any(
                        w.process.is_alive()
                        or (
                            w.process.exitcode
                            and (
                                args.max_seconds is None
                                or time.monotonic() - started < args.max_seconds
                            )
                        )
                        for w in workers
                    ):
                        break
                    if (
                        args.max_seconds is not None
                        and time.monotonic() - started >= args.max_seconds
                    ):
                        break
                    time.sleep(0.5)
                return 1 if any(w.process.exitcode for w in workers) else 0
            finally:
                for w in workers:
                    w.close()
        connection = Connection.load(args.connection)
        client = Client(connection)
        if args.command == "state":
            print(json.dumps(client.state(), indent=2))
            return 0
    except KeyboardInterrupt:
        logging.info("Stopped. Your kingdom remains available to reconnect.")
        return 0
    except (OSError, ValueError, ProtocolError) as error:
        logging.error("%s", error)
        return 1
    finally:
        if runner:
            runner.close()
        if isinstance(agent, IsolatedAgent):
            agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
