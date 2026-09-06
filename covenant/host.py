"""Account-paired supervisor. Each kingdom is an independent operating-system process."""

from __future__ import annotations
import hashlib
import importlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from .transport import Connection, Client, ProtocolError, atomic_json
from .durable import agent_directory
from .harness import CodexStrategist
from .runner import Runner, FileAgent
from .agent import ReferenceAgent

log = logging.getLogger("covenant")


def run_worker(connection, base, options, stop):
    root = agent_directory(base, connection)
    os.chdir(root)
    logging.basicConfig(
        filename=root / "worker.log",
        level=logging.INFO,
        force=True,
        format="%(asctime)s %(message)s",
    )
    agent = None
    runner = None
    try:
        if options.get("agent"):
            module, name = options["agent"].split(":", 1)
            agent = getattr(importlib.import_module(module), name)()
        elif options.get("files"):
            agent = FileAgent()
        elif options.get("codex", True):
            agent = CodexStrategist(timeout=options.get("model_timeout", 90))
        else:
            agent = ReferenceAgent()
        if not getattr(agent, "model_agent", False):
            agent.timeout = options.get("model_timeout", 90)
        runner = Runner(
            Client(connection),
            agent,
            options.get("poll", 2),
            options.get("fast", False),
            base,
        )

        def stop_when_requested():
            try:
                stop.recv_bytes()
            except (EOFError, OSError):
                pass
            runner.stop.set()
            runner.wake_network.set()
            if hasattr(agent, "cancel"):
                agent.cancel()

        threading.Thread(target=stop_when_requested, daemon=True).start()
        runner.run(options.get("max_turns"), options.get("max_seconds"))
    except Exception as e:
        log.exception("Kingdom worker stopped")
        atomic_json(
            root / "status.json",
            {
                "status": "offline",
                "error": {
                    "kind": getattr(e, "kind", "worker"),
                    "message": str(e)[:400],
                },
                "at": time.time(),
            },
        )
        raise
    finally:
        if runner:
            runner.close()
            # Python cannot safely interrupt an arbitrary callback thread. End only this worker.
            if runner.future and not runner.future.done():
                os._exit(1)


class Worker:
    def __init__(self, connection, base, options):
        self.connection, self.base, self.options = connection, base, options
        self.root = agent_directory(base, connection)
        self.failures = 0
        self.next_start = 0
        self.process = None
        self.stop = None
        self.started = 0

    def start(self):
        context = multiprocessing.get_context("spawn")
        receiver, self.stop = context.Pipe(duplex=False)
        self.process = context.Process(
            target=run_worker,
            args=(self.connection, self.base, self.options, receiver),
            name=f"covenant-{self.connection.gameId}-{self.connection.playerId}",
        )
        self.process.start()
        receiver.close()
        self.started = time.time()

    def status(self):
        try:
            state = json.loads((self.root / "status.json").read_text())
        except (OSError, ValueError):
            return (
                "connected" if self.process and self.process.is_alive() else "offline"
            )
        if (
            self.process
            and self.process.is_alive()
            and state.get("at", 0) > time.time() - 45
        ):
            return state.get("status", "connected")
        return "retrying"

    def error(self):
        try:
            error = json.loads((self.root / "status.json").read_text()).get("error")
            if error:
                return {
                    "kind": error.get("kind", "worker")[:40],
                    "message": error.get("message", "")[:400],
                }
        except (OSError, ValueError):
            pass
        return None

    def maintain(self):
        if not self.process:
            self.start()
            return
        stale = False
        try:
            stale = (
                time.time()
                - max(
                    self.started,
                    json.loads((self.root / "status.json").read_text()).get("at", 0),
                )
                > 120
            )
        except (OSError, ValueError):
            stale = time.time() - self.started > 120
        if stale:
            self.close()
        if self.process and self.process.is_alive():
            return
        if time.monotonic() < self.next_start:
            return
        self.failures += 1
        self.next_start = time.monotonic() + min(60, 2 ** min(self.failures, 6))
        self.start()
        log.warning(
            "%s/%s: restarted only this kingdom worker.",
            self.connection.gameId,
            self.connection.playerId,
        )

    def close(self):
        if not self.process:
            return
        # A killed process can leave multiprocessing.Event's semaphore locked.
        # A one-way stop pipe has no shared lock and cannot stall the supervisor.
        if self.process.is_alive():
            try:
                self.stop.send_bytes(b"stop")
            except (BrokenPipeError, EOFError, OSError):
                pass
        self.stop.close()
        self.process.join(timeout=3)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=3)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=3)


class Host:
    def __init__(
        self,
        server,
        state_directory=".covenant",
        label="My PC",
        fast=False,
        poll=2,
        model_timeout=90,
        pair_again=False,
    ):
        self.server, self.label, self.fast, self.poll = (
            server.rstrip("/"),
            label,
            fast,
            poll,
        )
        self.base = str(Path(state_directory).resolve())
        origin = hashlib.sha256(self.server.encode()).hexdigest()[:16]
        self.root = Path(self.base) / "hosts" / origin
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pair_again = pair_again
        self.instance = uuid.uuid4().hex
        self.model_timeout = model_timeout
        self.workers = {}
        self.stop = threading.Event()
        try:
            self.credentials = json.loads((self.root / "host.json").read_text())
        except (OSError, ValueError):
            self.credentials = None
        self.client = Client(
            Connection(
                self.server,
                "00000000",
                "p1",
                (
                    self.credentials["token"]
                    if self.credentials
                    else "unpaired-registration-key"
                ),
            ),
            retries=2,
        )

    def register(self):
        if not self.credentials:
            self.credentials = self.client._request(
                "/agent-hosts", {"label": self.label, "model": "gpt-5.6-luna"}
            )
            atomic_json(self.root / "host.json", self.credentials)
            self.client.connection.token = self.credentials["token"]
        if self.pair_again:
            self.credentials.update(
                self.client._request(
                    f"/agent-hosts/{self.credentials['id']}/pairing", {"repair": True}
                )
            )
            atomic_json(self.root / "host.json", self.credentials)
            self.pair_again = False
        if self.credentials.get("pairCode"):
            self.print_pair()

    def print_pair(self):
        print(
            f"\nOpen {self.server}, sign in with Google, and choose Pair once.\nPairing code: {self.credentials['pairCode']}\nThis account alone can create lobbies using this host. Keep it running while playing.\n",
            flush=True,
        )

    def step(self):
        statuses = [
            {"gameId": k[0], "playerId": k[1], "status": w.status(), "error": w.error()}
            for k, w in self.workers.items()
        ]
        work = self.client._request(
            f"/agent-hosts/{self.credentials['id']}/work",
            {"instance": self.instance, "statuses": statuses},
        )
        if work["paired"] and self.credentials.get("pairCode"):
            self.credentials.pop("pairCode", None)
            atomic_json(self.root / "host.json", self.credentials)
            print(
                "Account paired. Create a Play against Luna lobby on the website.",
                flush=True,
            )
        if not work["paired"] and (
            not self.credentials.get("pairCode")
            or self.credentials.get("expiresAt", 0) < time.time() * 1000
        ):
            self.credentials.update(
                self.client._request(
                    f"/agent-hosts/{self.credentials['id']}/pairing", {}
                )
            )
            atomic_json(self.root / "host.json", self.credentials)
            self.print_pair()
        jobs = {(j["gameId"], j["playerId"]): j for j in work["jobs"]}
        for key in list(self.workers):
            if key not in jobs:
                self.workers.pop(key).close()
        for key, job in jobs.items():
            if job["status"] != "active":
                continue
            if key not in self.workers:
                c = Connection(
                    self.server, job["gameId"], job["playerId"], job["token"]
                )
                self.workers[key] = Worker(
                    c,
                    self.base,
                    {
                        "codex": True,
                        "model_timeout": self.model_timeout,
                        "poll": self.poll,
                        "fast": self.fast,
                    },
                )
            self.workers[key].maintain()
        return work

    def run(self, max_seconds=None):
        result = subprocess.run(
            ["codex", "login", "status"], capture_output=True, timeout=15
        )
        if result.returncode:
            raise ValueError("Run codex login first, then covenant host.")
        CodexStrategist.check_compatibility(self.root)
        self.register()
        start = time.monotonic()
        try:
            while not self.stop.is_set() and (
                max_seconds is None or time.monotonic() - start < max_seconds
            ):
                delay = 3
                try:
                    work = self.step()
                    if not any(j["status"] == "active" for j in work["jobs"]):
                        delay = 10
                except ProtocolError as e:
                    if e.status in (401, 403, 404):
                        raise
                    log.warning("Host reconnecting: %s", e.code)
                self.stop.wait(delay)
        finally:
            for worker in self.workers.values():
                worker.close()
