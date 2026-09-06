"""Networking and durable delivery independent of user callback execution."""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from .agent import Agent
from .context import Context
from .state import Record
from .durable import Journal
from .transport import ProtocolError, atomic_json

log = logging.getLogger("covenant")


class Runtime:
    def __init__(
        self,
        client,
        agent,
        directory,
        instance_id=None,
        config=None,
        poll=2,
        command_timeout=12,
    ):
        if not isinstance(agent, Agent):
            raise TypeError("Agent must implement all five covenant.Agent callbacks.")
        self.client, self.agent = client, agent
        self.root = Path(directory).resolve()
        self.workspace, self.private = self.root / "workspace", self.root / "runtime"
        for p in (self.workspace, self.private):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Repository metadata is never mounted into model-script containers.
        git_dir = self.private / "script-git"
        if not git_dir.exists():
            if not shutil.which("git"):
                raise RuntimeError(
                    "Install Git to initialize this agent instance workspace."
                )
            subprocess.run(
                [
                    "git",
                    "--git-dir=" + str(git_dir),
                    "--work-tree=" + str(self.workspace),
                    "init",
                    "-q",
                ],
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                },
                check=True,
                capture_output=True,
            )
        pointer = self.workspace / ".git"
        if pointer.is_symlink():
            pointer.unlink()
        pointer.write_text("gitdir: " + str(git_dir) + "\n")
        self.resumed = (self.private / "journal.sqlite").exists()
        self.journal = Journal(self.private)
        with self.journal.lock, self.journal.db:
            self.journal.db.execute(
                "CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,at INTEGER NOT NULL,message TEXT NOT NULL)"
            )
        self.instance_id = instance_id or self.root.name
        self.config, self.poll, self.command_timeout = (
            config or {},
            poll,
            command_timeout,
        )
        self.lock, self.stop, self.wake = (
            threading.RLock(),
            threading.Event(),
            threading.Event(),
        )
        self.observation = self.journal.get("observation", {})
        self.snapshot = None
        self.cursor = self.journal.get("cursor", 0)
        self.status, self.error = "waiting", None
        self.stats = self.journal.get(
            "stats",
            {
                "callbacks": 0,
                "callback_failures": 0,
                "network_failures": 0,
                "commands": 0,
            },
        )
        self.callback_started = None
        self.finished = None
        self.closed = False
        self.ctx = Context(self)

    def _accept(self, obs):
        with self.lock:
            if obs.get("revision", 0) < self.observation.get("revision", 0):
                return
            if "tiles" not in obs and self.observation:
                obs = {**obs, "tiles": self.observation["tiles"]}
            self.observation = copy.deepcopy(obs)
            self.snapshot = None
        with self.journal.lock, self.journal.db:
            for m in obs.get("messages", []):
                self.journal.db.execute(
                    "INSERT OR IGNORE INTO conversations VALUES(?,?,?)",
                    (m["id"], m.get("at", 0), json.dumps(m)),
                )
        # Terrain remains in the transport cache; persist a recovery snapshot only when changed.
        self.journal.set("observation", obs)

    def conversation(self, player, after=None):
        with self.journal.lock:
            rows = [
                json.loads(r[0])
                for r in self.journal.db.execute(
                    "SELECT message FROM conversations ORDER BY at,id"
                )
            ]
        rows = [m for m in rows if player in (m["from"], m["to"])]
        if after is not None:
            if isinstance(after, (int, float)):
                rows = [m for m in rows if m.get("at", 0) > after]
            else:
                index = next((i for i, m in enumerate(rows) if m["id"] == after), -1)
                rows = rows[index + 1 :]
        return rows

    def persist(self):
        self.journal.set("stats", self.stats)
        atomic_json(
            self.private / "status.json",
            {
                "status": self.status,
                "error": self.error,
                "at": time.time(),
                "callbackStarted": self.callback_started,
                "stats": self.stats,
                "finished": self.finished,
                "instanceId": self.instance_id,
            },
        )

    def flush(self):
        for key, command in self.journal.outgoing():
            try:
                response = self.client.command(
                    command["type"], command.get("data"), key
                )
                if response.get("observation"):
                    self._accept(response["observation"])
                receipt = {
                    "id": key,
                    "ok": True,
                    "status": "confirmed",
                    "result": response.get("result"),
                }
            except ProtocolError as e:
                if (
                    not e.status
                    or e.status >= 500
                    or e.status == 429
                    or e.code in ("CONFLICT", "CATCHING_UP")
                ):
                    raise
                receipt = {
                    "id": key,
                    "ok": False,
                    "status": "rejected",
                    "error": str(e),
                    "code": e.code,
                }
            self.journal.complete(key, receipt)
            self.journal.log("receipt", {"command": command, **receipt})
            self.stats["commands"] += 1

    def network_step(self):
        self.flush()
        update = self.client.updates(self.cursor)
        obs = update["observation"]
        if not self.observation or obs.get("revision") != self.observation.get(
            "revision"
        ):
            self._accept(obs)
        events = list(update.get("events", []))
        if update.get("reset"):
            events += [
                {"kind": "message", "cursor": update["cursor"], "data": m}
                for m in obs.get("messages", [])
                if m["to"] == obs["you"]
            ]
            events.append(
                {
                    "kind": "reset",
                    "cursor": update["cursor"],
                    "data": {
                        "reason": "Live feed expired; current state and retained messages restored."
                    },
                }
            )
        self.journal.receive(events, update["cursor"])
        self.cursor = update["cursor"]
        if self.status in ("waiting", "retrying", "offline"):
            self.status = "waiting" if obs["status"] == "lobby" else "connected"
            self.error = None
        self.flush()
        self.persist()
        return update

    def _network(self):
        while not self.stop.is_set():
            try:
                self.network_step()
            except ProtocolError as e:
                self.status = "offline" if e.status in (401, 403, 404) else "retrying"
                self.error = {
                    "kind": "authentication" if e.status in (401, 403) else "network",
                    "message": str(e),
                    "code": e.code,
                }
                self.stats["network_failures"] += 1
                self.persist()
                if e.status in (401, 403, 404):
                    self.finished = "authentication_failed"
                    return
            except Exception as e:
                self.error = {"kind": "network", "message": str(e)[:400]}
                self.status = "retrying"
                self.persist()
                log.exception("Network worker failed")
            self.wake.wait(self.poll)
            self.wake.clear()

    def _invoke(self, name, event_id, *args):
        self.callback_started = time.time()
        self.persist()
        try:
            result = getattr(self.agent, name)(Context(self, event_id), *args)
            if result is not None:
                raise TypeError("Callbacks return None; issue commands through ctx.")
            self.stats["callbacks"] += 1
        finally:
            self.callback_started = None

    def run(self, max_seconds=None):
        started, last_turn, started_hook = time.monotonic(), None, False
        network = threading.Thread(
            target=self._network, name="game-network", daemon=True
        )
        network.start()
        reason = "stopped"
        last_connection = None
        try:
            while not self.stop.is_set() and (
                max_seconds is None or time.monotonic() - started < max_seconds
            ):
                if self.finished:
                    reason = self.finished
                    break
                if not self.observation:
                    self.stop.wait(0.05)
                    continue
                if not started_hook:
                    self._invoke("on_start", "start:" + uuid.uuid4().hex)
                    started_hook = True
                state = self.ctx.get_state()
                current_connection = (
                    self.status == "retrying",
                    self.error.get("kind") if self.error else None,
                )
                if current_connection != last_connection:
                    self._invoke(
                        "on_event",
                        "connection:" + uuid.uuid4().hex,
                        Record(
                            {
                                "kind": "connection",
                                "status": self.status,
                                "error": self.error,
                            }
                        ),
                    )
                    last_connection = current_connection
                for e in self.journal.pending(64):
                    eid = e["inboxId"]
                    self.journal.mark([eid], "presented")
                    try:
                        if e["kind"] == "message":
                            if e["data"]["to"] == state.you:
                                self._invoke("on_message", eid, Record(e["data"]))
                        elif e["kind"] != "turn":
                            self._invoke("on_event", eid, Record(e))
                        with self.journal.lock, self.journal.db:
                            self.journal.db.execute(
                                "UPDATE inbox SET state='handled' WHERE id=? AND state='presented'",
                                (eid,),
                            )
                    except Exception:
                        self.journal.retry_presented([eid])
                        raise
                state = self.ctx.get_state()
                if state.status == "finished" or state.get_player().eliminated:
                    reason = state.get("termination") or (
                        "eliminated" if state.get_player().eliminated else "finished"
                    )
                    self.finished = reason
                    break
                if state.status == "active" and state.turn != last_turn:
                    self._invoke("on_turn", f"turn:{state.turn}", state.turn)
                    last_turn = state.turn
                self.stop.wait(0.05)
        except Exception as e:
            self.stats["callback_failures"] += 1
            self.error = {"kind": "callback", "message": str(e)[:400]}
            self.status = "retrying"
            reason = "callback_failed"
            self.journal.log("callback_failure", {"error": str(e)})
            raise
        finally:
            if started_hook:
                try:
                    self._invoke("on_stop", "stop:" + uuid.uuid4().hex, reason)
                except Exception:
                    log.exception("Agent cleanup failed")
            self.stop.set()
            self.wake.set()
            network.join(timeout=45)
            self.status = "offline"
            self.persist()
        return self.observation

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.wake.set()
        self.journal.close()
