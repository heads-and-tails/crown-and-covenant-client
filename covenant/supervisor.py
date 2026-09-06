"""Provider-independent account connection and one worker process per assigned seat."""

import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from .catalog import discover, pin
from .transport import Client, Connection, ProtocolError, atomic_json

log = logging.getLogger("covenant")


class Instance:
    def __init__(self, root, job, definition, server, fast=False):
        self.root = Path(root)
        self.private = self.root / "runtime"
        self.private.mkdir(parents=True, exist_ok=True)
        self.job = job
        self.process = None
        self.starts = 0
        self.next_start = 0
        self.started = 0
        self.fast = fast
        self.definition = pin(definition, self.private)
        Connection(server, job["gameId"], job["playerId"], job["token"]).save(
            self.private / "connection.json"
        )
        atomic_json(
            self.private / "assignment.json",
            {k: v for k, v in job.items() if k != "token"},
        )
        self.output = None

    def report(self):
        try:
            r = json.loads((self.private / "status.json").read_text())
        except (OSError, ValueError):
            r = {"status": "waiting", "error": None, "at": self.started}
        if not self.process or self.process.poll() is not None:
            r["status"] = "retrying" if self.starts < 8 else "offline"
        elif time.time() - r.get("at", 0) > 45:
            r["status"] = "retrying"
        return {
            "instanceId": self.job["instanceId"],
            "status": r["status"],
            "error": r.get("error"),
        }

    def maintain(self):
        if self.process and self.process.poll() is None:
            try:
                status = json.loads((self.private / "status.json").read_text())
                stale = time.time() - max(self.started, status.get("at", 0)) > 120
                hung = (
                    status.get("callbackStarted")
                    and time.time() - status["callbackStarted"] > 120
                )
                if not stale and not hung:
                    return
            except (OSError, ValueError):
                if time.time() - self.started < 240:
                    return
            self.close()
        if self.starts >= 8 or time.monotonic() < self.next_start:
            return
        # A crashed worker may leave children or a Docker execution behind.
        self.cleanup_execution()
        self.starts += 1
        self.next_start = time.monotonic() + min(60, 2**self.starts)
        if self.output:
            self.output.close()
        self.output = (self.private / "worker.log").open("a")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "covenant.worker",
                str(self.root),
                *(["--fast"] if self.fast else []),
            ],
            stdin=subprocess.PIPE,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
        self.started = time.time()

    def cleanup_execution(self):
        if self.process and self.process.poll() is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if shutil.which("docker"):
            try:
                subprocess.run(
                    [
                        "docker",
                        "rm",
                        "-f",
                        "covenant-script-" + self.job["instanceId"][:32],
                    ],
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("stop\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
        self.cleanup_execution()
        if self.process and self.process.stdin:
            self.process.stdin.close()
        if self.output:
            self.output.close()
            self.output = None


class Supervisor:
    def __init__(self, server, root=".", label="My PC", fast=False, pair_again=False):
        self.server = server.rstrip("/")
        self.root = Path(root).resolve()
        self.label = label
        self.fast = fast
        self.pair_again = pair_again
        self.origin = hashlib.sha256(self.server.encode()).hexdigest()[:16]
        self.private = self.root / ".covenant" / "clients" / self.origin
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.credentials = json.loads((self.private / "client.json").read_text())
        except (OSError, ValueError):
            self.credentials = None
        self.client = Client(
            Connection(
                server,
                "00000000",
                "p1",
                (
                    self.credentials["token"]
                    if self.credentials
                    else "unpaired-client-registration"
                ),
            ),
            timeout=8,
            retries=1,
        )
        self.instance = uuid.uuid4().hex
        self.workers = {}
        self.stop = threading.Event()
        self.lease = 0
        self.failed = {}

    def register(self):
        if not self.credentials:
            self.credentials = self.client._request("/clients", {"label": self.label})
            self.client.connection.token = self.credentials["token"]
            atomic_json(self.private / "client.json", self.credentials)
        if self.pair_again:
            self.credentials.update(
                self.client._request(f"/clients/{self.credentials['id']}/pairing", {})
            )
            atomic_json(self.private / "client.json", self.credentials)
        if self.credentials.get("pairCode"):
            self.print_pair()

    def print_pair(self):
        print(
            f"Open {self.server}, sign in, and choose Connect your agents.\nConnection code: {self.credentials['pairCode']}\nAgent folders: {self.root/'agents'}\nKeep this client running; choose agents in the website lobby.",
            flush=True,
        )

    def step(self):
        definitions = discover(self.root / "agents")
        by_version = {(d.public["id"], d.public["version"]): d for d in definitions}
        work = self.client._request(
            f"/clients/{self.credentials['id']}/work",
            {
                "instance": self.instance,
                "catalog": [d.public for d in definitions],
                "statuses": [w.report() for w in self.workers.values()]
                + list(self.failed.values()),
            },
        )
        self.lease = time.monotonic() + max(
            0, min(30, (work["leaseUntil"] - work["serverTime"]) / 1000)
        )
        if work["paired"] and self.credentials.get("pairCode"):
            self.credentials.pop("pairCode", None)
            atomic_json(self.private / "client.json", self.credentials)
            print(
                "Connected. Your agent folders are available in the website lobby.",
                flush=True,
            )
        if (
            not work["paired"]
            and self.credentials.get("expiresAt", 0) < time.time() * 1000
        ):
            self.credentials.update(
                self.client._request(f"/clients/{self.credentials['id']}/pairing", {})
            )
            atomic_json(self.private / "client.json", self.credentials)
            self.print_pair()
        jobs = {j["instanceId"]: j for j in work["jobs"]}
        self.failed = {}
        for id in list(self.workers):
            if id not in jobs:
                self.workers.pop(id).close()
        for id, job in jobs.items():
            if id not in self.workers:
                root = self.root / "instances" / self.origin / job["gameId"] / id
                definition = by_version.get(
                    (job["definitionId"], job["definitionVersion"])
                )
                if not definition:
                    pinned = root / "runtime" / "definition"
                    if pinned.exists():
                        from .catalog import Definition
                        import tomllib

                        definition = Definition(
                            pinned,
                            tomllib.loads((pinned / "agent.toml").read_text()),
                            json.loads((root / "runtime" / "pinned.json").read_text()),
                        )
                    else:
                        log.error(
                            "Assigned definition is missing or changed: %s. Reassign the seat.",
                            job["definitionId"],
                        )
                        self.failed[id] = {
                            "instanceId": id,
                            "status": "offline",
                            "error": {
                                "kind": "definition",
                                "message": "Assigned definition changed or is missing. Restore it or reassign this lobby seat.",
                            },
                        }
                        continue
                self.workers[id] = Instance(
                    root, job, definition, self.server, self.fast
                )
            self.workers[id].maintain()
        return work

    def run(self, max_seconds=None):
        self.register()
        start = time.monotonic()
        try:
            while not self.stop.is_set() and (
                max_seconds is None or time.monotonic() - start < max_seconds
            ):
                try:
                    self.step()
                except ProtocolError as e:
                    log.warning("Client reconnecting: %s", e)
                    if e.code == "CLIENT_LEASE" or e.status in (401, 403, 404):
                        raise
                    if time.monotonic() >= self.lease:
                        for w in self.workers.values():
                            w.close()
                self.stop.wait(3)
        finally:
            for w in self.workers.values():
                w.close()
