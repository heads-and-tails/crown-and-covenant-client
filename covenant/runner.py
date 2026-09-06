"""A responsive transport loop beside a serial callback/model thread, per kingdom."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import inspect
import json
import logging
import threading
import time
from pathlib import Path
from .agent import Agent, ReferenceAgent
from .context import Context
from .controller import TacticalController, pos
from .durable import Journal, agent_directory
from .goals import validate_goal
from .transport import Client, ProtocolError, atomic_json

log = logging.getLogger("covenant")
DIPLOMACY = {"message", "offer", "answer"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class FileAgent(Agent):
    def __init__(self, directory=None):
        self.requested_directory = directory
        self.directory = None

    def observe(self, observation, events, cursor):
        atomic_json(self.directory / "observation.json", observation)
        atomic_json(
            self.directory / "events.json", {"cursor": cursor, "events": events}
        )


class Runner:
    def __init__(
        self,
        client: Client,
        agent: Agent | None = None,
        poll=2,
        fast=False,
        state_directory=".covenant",
    ):
        self.client, self.agent = client, agent or ReferenceAgent()
        self.poll, self.fast = max(0.25, poll), fast
        self.root = agent_directory(state_directory, client.connection)
        self.journal = Journal(self.root)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake_network = threading.Event()
        self.last_observation = None
        self.status = "connected"
        self.error = None
        self.responded_at = 0
        self.cursor = self.journal.get("cursor", 0)
        self.last_turn = self.journal.get("last_turn", -1)
        self.planned_turn = -1
        self.tactical_turn = -1
        self.goals_dirty = False
        self.stats = self.journal.get(
            "stats",
            {
                "model_successes": 0,
                "model_failures": 0,
                "orders_sent": 0,
                "diplomatic_actions": 0,
                "network_failures": 0,
                "unanswered_messages": 0,
                "events_received": 0,
                "stale_orders_discarded": 0,
            },
        )
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agent-callback"
        )
        self.future = None
        self.future_meta = {}
        self.next_reasoning = 0.0
        self.backoff = 0
        self.controller = TacticalController()
        if isinstance(self.agent, FileAgent):
            self.agent.directory = self.root / "files"
            self.agent.directory.mkdir(exist_ok=True)
        if hasattr(self.agent, "bind"):
            self.agent.bind(self.root)
        if getattr(self.agent, "model_agent", False) and not self.journal.get("goals"):
            self.journal.set(
                "goals",
                {"strategy": {"kind": "strategy", "intent": {"stance": "expand"}}},
            )
        goals = self.journal.get("goals", {})
        for goal_id, goal in list(goals.items()):
            try:
                validate_goal(goal)
            except ValueError as e:
                goals.pop(goal_id)
                self.journal.log(
                    "invalid_saved_goal", {"id": goal_id, "goal": goal, "error": str(e)}
                )
                self.journal.receive(
                    [
                        {
                            "kind": "goal",
                            "cursor": int(time.time() * 1000000),
                            "data": {"id": goal_id, "result": "failed: " + str(e)},
                        }
                    ],
                    self.cursor,
                )
        self.journal.set("goals", goals)
        atomic_json(
            self.root / "identity.json",
            {
                "server": client.connection.server,
                "gameId": client.connection.gameId,
                "playerId": client.connection.playerId,
            },
        )

    def persist(self):
        self.journal.set("stats", self.stats)
        atomic_json(
            self.root / "status.json",
            {
                "status": self.status,
                "error": self.error,
                "at": time.time(),
                "turn": (self.last_observation or {}).get("turn"),
                "stats": self.stats,
            },
        )
        atomic_json(self.root / "stats.json", self.stats)

    def set_goal(self, goal_id, goal):
        if not isinstance(goal_id, str) or len(goal_id) > 80:
            raise ValueError("Invalid goal ID.")
        goals = self.journal.get("goals", {})
        if goal is None:
            goals.pop(goal_id, None)
        else:
            validate_goal(goal)
            if len(json.dumps(goal)) > 8000:
                raise ValueError("Goal is too large.")
            goals[goal_id] = goal
        self.journal.set("goals", goals)
        atomic_json(self.root / "goals.json", goals)
        self.goals_dirty = True
        self.wake_network.set()

    @staticmethod
    def meaningful(e, you):
        d = e.get("data", {})
        if e["kind"] == "message":
            return d.get("to") == you and d.get("from") != you
        if e["kind"] == "trade":
            return d.get("to") == you or d.get("status") != "pending"
        if e["kind"] == "report":
            return d.get("kind") in ("capture", "battle", "system")
        return e["kind"] in ("turn", "reset", "goal")

    def _accept(self, obs):
        with self.lock:
            if self.last_observation and obs.get(
                "revision", 0
            ) < self.last_observation.get("revision", 0):
                return
            if "tiles" not in obs and self.last_observation:
                obs["tiles"] = self.last_observation["tiles"]
            self.last_observation = obs
        atomic_json(self.root / "observation.json", obs)

    def flush_commands(self, obs=None):
        for key, command in self.journal.outgoing():
            try:
                r = self.client.command(command["type"], command.get("data"), key)
                receipt = {"ok": True, "result": r.get("result"), "key": key}
                if r.get("observation"):
                    self._accept(r["observation"])
                self.stats[
                    (
                        "orders_sent"
                        if command["type"] == "orders"
                        else "diplomatic_actions"
                    )
                ] += 1
            except ProtocolError as e:
                if (
                    not e.status
                    or e.status >= 500
                    or e.status == 429
                    or e.code in ("CONFLICT", "CATCHING_UP")
                ):
                    self.error = {"kind": "network", "code": e.code, "message": str(e)}
                    self.stats["network_failures"] += 1
                    break
                receipt = {
                    "ok": False,
                    "error": str(e),
                    "status": e.status,
                    "code": e.code,
                    "key": key,
                }
                if e.code == "STALE_ORDER":
                    self.stats["stale_orders_discarded"] += 1
            self.journal.complete(key, receipt)
            self.journal.log("action_receipt", {"command": command, **receipt})
        if isinstance(self.agent, FileAgent):
            atomic_json(
                self.agent.directory / "receipts.json",
                {"commands": self.journal.receipts(10000)},
            )

    def _patch(self, plan, obs):
        if not plan:
            return None
        if "turn" not in plan:
            return plan
        if plan["turn"] != obs["turn"]:
            self.stats["stale_orders_discarded"] += 1
            return None
        patch = {"armies": [], "castles": []}
        for m in plan.get("moves", []):
            a = next(
                (
                    a
                    for a in obs["armies"]
                    if a["id"] == m["armyId"] and a["owner"] == obs["you"]
                ),
                None,
            )
            if not a:
                continue
            path = m.get("route", [{"x": m["x"], "y": m["y"]}])
            if path and pos(path[0]) == pos(a):
                path = path[1:]
            if path != a.get("route", []):
                patch["armies"].append(
                    {
                        "armyId": a["id"],
                        "from": {"x": a["x"], "y": a["y"]},
                        "route": path,
                        "revision": a.get("orderRevision", 0),
                    }
                )
        for p in plan.get("production", []):
            s = next(
                (
                    s
                    for s in obs["structures"]
                    if s["id"] == p["castleId"] and s["owner"] == obs["you"]
                ),
                None,
            )
            if not s:
                continue
            production = (
                {"troop": p["troop"], "count": p["count"]} if p.get("troop") else None
            )
            if s.get("production") != production:
                patch["castles"].append(
                    {
                        "castleId": s["id"],
                        "production": production,
                        "revision": s.get("orderRevision", 0),
                    }
                )
        return patch

    def set_orders(self, orders, obs, scope=None):
        patch = self._patch(orders, obs)
        if patch and (patch.get("armies") or patch.get("castles")):
            key = digest([scope or "orders", obs["turn"], patch])
            self.journal.enqueue(key, {"type": "orders", "data": patch})

    def queue(self, commands, scope):
        for i, c in enumerate(commands or []):
            if c.get("type") not in DIPLOMACY | {"orders", "ready", "unready"}:
                continue
            self.journal.enqueue(
                digest([scope, c.get("id", i)]),
                {"type": c["type"], "data": c.get("data", {})},
            )

    def dispatch(self, commands, observation):
        self.queue(commands, ["manual", observation["turn"]])
        self.flush_commands()

    def tactics(self, obs):
        goals = self.journal.get("goals", {})
        if not goals:
            return
        intent = {}
        explicit = {}
        castle_targets = []
        defense = []
        finished = []
        for key, g in goals.items():
            kind = g["kind"]
            target = next(
                (s for s in obs["structures"] if s["id"] == g.get("structureId")), None
            )
            if kind == "strategy":
                intent.update(g.get("intent", {}))
            elif kind == "capture":
                if target and target["owner"] == obs["you"]:
                    finished.append((key, "completed"))
                elif target:
                    castle_targets.append(target["id"])
                    if g.get("armyId"):
                        explicit[g["armyId"]] = target
                else:
                    finished.append((key, "failed: target no longer exists"))
            elif kind == "defend" and target:
                defense.append(target["id"])
            elif kind in ("rally", "hold") and g.get("armyId"):
                a = next((a for a in obs["armies"] if a["id"] == g["armyId"]), None)
                if a:
                    explicit[a["id"]] = a if kind == "hold" else g.get("destination", a)
                else:
                    finished.append((key, "failed: army destroyed or merged"))
        intent["castleTargets"] = castle_targets + intent.get("castleTargets", [])
        intent["defensivePriorities"] = defense + intent.get("defensivePriorities", [])
        intent["targets"] = {**intent.get("targets", {}), **explicit}
        plan = self.controller.plan(obs, intent)
        for g in goals.values():
            if g["kind"] == "recruit" and g.get("castleId"):
                plan["production"] = [
                    p for p in plan["production"] if p["castleId"] != g["castleId"]
                ] + [
                    {
                        "castleId": g["castleId"],
                        "troop": g.get("troop"),
                        "count": g.get("count", 1),
                    }
                ]
            if g["kind"] == "hold" and g.get("armyId"):
                a = next((a for a in obs["armies"] if a["id"] == g["armyId"]), None)
                if a:
                    plan["moves"] = [
                        m for m in plan["moves"] if m["armyId"] != a["id"]
                    ] + [{"armyId": a["id"], "x": a["x"], "y": a["y"], "route": []}]
        self.set_orders(plan, obs, ["tactical", self.journal.get("goals", {})])
        for key, result in finished:
            goals.pop(key, None)
            self.journal.log("goal", {"id": key, "result": result})
            self.journal.receive(
                [
                    {
                        "kind": "goal",
                        "cursor": int(time.time() * 1000000),
                        "data": {"id": key, "result": result},
                    }
                ],
                self.cursor,
            )
        if finished:
            self.journal.set("goals", goals)
        self.tactical_turn = obs["turn"]
        self.goals_dirty = False

    @staticmethod
    def _hook(agent, name, ctx, *args):
        fn = getattr(agent, name, None)
        if not fn:
            return None
        parameters = list(inspect.signature(fn).parameters)
        # v2 observation callbacks remain supported; v3 callbacks use ctx/context.
        first = (
            ctx
            if parameters and parameters[0] in ("ctx", "context")
            else ctx.get_state()
        )
        return fn(first, *args)

    def _invoke(self, events, new_turn, scope):
        ctx = Context(self, scope)
        if getattr(self.agent, "model_agent", False):
            return self.agent.reason(ctx, events)
        for e in events:
            if e["kind"] == "message" and e["data"].get("to") == ctx.get_state()["you"]:
                self._hook(self.agent, "on_message", ctx, e["data"])
        commands = self._hook(self.agent, "on_events", ctx, events) or []
        if isinstance(commands, dict):
            commands = [commands]
        self.queue(commands, scope + ["events"])
        if new_turn:
            # Capture the snapshot before invoking a slow legacy callback.
            obs = ctx.get_state()
            plan = self._hook(self.agent, "on_turn", ctx)
            self.set_orders(plan, obs, scope)
        return {"ok": True}

    def files(self, obs):
        directory = self.agent.directory
        try:
            acknowledgements = json.loads(
                (directory / "acknowledgements.json").read_text()
            )
            for item in acknowledgements.get("events", []):
                if item.get("state") not in ("handled", "answered", "no_reply"):
                    raise ValueError(
                        "Acknowledgement state must be handled, answered or no_reply."
                    )
                self.journal.mark([item["inboxId"]], item["state"], item.get("reason"))
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, KeyError) as e:
            atomic_json(directory / "error.json", {"error": str(e)})
        self.agent.observe(obs, self.journal.pending(10000), self.cursor)
        for filename in ("outbox.json", "orders.json"):
            try:
                data = json.loads((directory / filename).read_text())
                if filename == "outbox.json":
                    self.queue(data.get("commands", []), ["file"])
                else:
                    patch = data.get("orders", data)
                    self.set_orders(patch, obs, ["file", data.get("id", digest(data))])
            except FileNotFoundError:
                pass
            except (ValueError, TypeError, KeyError) as e:
                atomic_json(directory / "error.json", {"error": str(e)})

    def step(self):
        if self.status == "responding" and time.monotonic() - self.responded_at > 3:
            self.status = "connected"
        self.flush_commands()
        update = self.client.updates(self.cursor)
        obs = update["observation"]
        self._accept(obs)
        events = update.get("events", [])
        if update.get("reset"):
            events += [
                {"kind": "message", "cursor": update["cursor"], "data": m}
                for m in obs.get("messages", [])
                if m["to"] == obs["you"]
            ]
            events += [
                {
                    "kind": "reset",
                    "cursor": update["cursor"],
                    "data": {
                        "reason": "Expired live feed; refreshed retained conversations."
                    },
                }
            ]
        self.journal.receive(events, update["cursor"])
        self.cursor = update["cursor"]
        self.stats["events_received"] += len(events)
        self.flush_commands()
        obs = self.last_observation
        if (
            obs["status"] != "active"
            or next(p for p in obs["players"] if p["id"] == obs["you"])["eliminated"]
        ):
            self.status = "waiting" if obs["status"] == "lobby" else "offline"
            self.persist()
            return obs
        if self.future and self.future.done():
            try:
                result = self.future.result()
                self.stats["model_successes"] += int(
                    getattr(self.agent, "model_agent", False)
                )
                self.status = "responding"
                self.responded_at = time.monotonic()
                self.error = None
                self.backoff = 0
                message_states = {m["id"]: m["state"] for m in self.journal.messages()}
                for e in self.future_meta["events"]:
                    eid = e["inboxId"]
                    if message_states.get(eid) in ("answered", "no_reply"):
                        continue
                    if e["kind"] == "message" and getattr(
                        self.agent, "model_agent", False
                    ):
                        attempts = self.journal.get("message_attempts", {})
                        attempts[eid] = attempts.get(eid, 0) + 1
                        self.journal.set("message_attempts", attempts)
                        if attempts[eid] < 3:
                            self.journal.mark(
                                [eid],
                                "received",
                                "Agent did not handle message; retrying",
                            )
                        else:
                            self.journal.mark(
                                [eid],
                                "unanswered",
                                "Three reasoning cycles completed without a reply or explicit no_reply",
                            )
                            self.stats["unanswered_messages"] += 1
                    else:
                        self.journal.mark([eid], "handled")
            except Exception as e:
                self.stats["model_failures"] += 1
                self.backoff = min(5, self.backoff + 1)
                self.status = "retrying"
                self.error = {
                    "kind": getattr(
                        e,
                        "kind",
                        (
                            "model"
                            if getattr(self.agent, "model_agent", False)
                            else "callback"
                        ),
                    ),
                    "message": str(e)[:400],
                }
                self.journal.log("reasoning_error", self.error)
                self.journal.mark(
                    [e["inboxId"] for e in self.future_meta["events"]], "received"
                )
                self.planned_turn = -1
                self.next_reasoning = time.monotonic() + min(60, 2**self.backoff)
            self.future = None
        if (
            self.future
            and time.monotonic() - self.future_meta["at"]
            > getattr(self.agent, "timeout", 90) + 15
        ):
            if not getattr(self.agent, "model_agent", False):
                raise RuntimeError(
                    "Custom callback exceeded its watchdog. Restarting this kingdom worker."
                )
            if hasattr(self.agent, "cancel"):
                self.agent.cancel()
            self.status = "retrying"
            self.error = {
                "kind": "model",
                "message": "Reasoning watchdog interrupted an unresponsive model.",
            }
        if (self.tactical_turn != obs["turn"] or self.goals_dirty) and not update.get(
            "catchingUp"
        ):
            self.tactics(obs)
        if isinstance(self.agent, FileAgent):
            self.files(obs)
        elif not self.future and time.monotonic() >= self.next_reasoning:
            pending = self.journal.pending()
            ignored = [
                e["inboxId"] for e in pending if not self.meaningful(e, obs["you"])
            ]
            self.journal.mark(ignored, "handled")
            events = [e for e in pending if self.meaningful(e, obs["you"])]
            new_turn = self.planned_turn != obs["turn"]
            if events or new_turn:
                scope = ["reasoning", obs["turn"], [e["inboxId"] for e in events]]
                self.future_meta = {
                    "at": time.monotonic(),
                    "turn": obs["turn"],
                    "events": events,
                }
                self.journal.mark([e["inboxId"] for e in events], "presented")
                self.planned_turn = obs["turn"]
                self.status = "thinking"
                self.next_reasoning = time.monotonic() + 1
                self.future = self.executor.submit(
                    self._invoke, copy.deepcopy(events), new_turn, scope
                )
        self.flush_commands()
        if self.fast and not self.future and not self.journal.outgoing():
            self.journal.enqueue(
                digest(["ready", obs["turn"]]),
                {"type": "ready", "data": {"turn": obs["turn"]}},
            )
        self.last_turn = obs["turn"]
        self.journal.set("last_turn", self.last_turn)
        self.persist()
        return obs

    def run(self, max_turns=None, max_seconds=None):
        start = time.monotonic()
        seen = set()
        while not self.stop.is_set() and (
            max_seconds is None or time.monotonic() - start < max_seconds
        ):
            try:
                obs = self.step()
                seen.add(obs["turn"])
                if (
                    obs["status"] == "finished"
                    or obs.get("readOnly")
                    or next(p for p in obs["players"] if p["id"] == obs["you"])[
                        "eliminated"
                    ]
                ):
                    return obs
                if max_turns and len(seen) >= max_turns and not self.future:
                    return obs
            except ProtocolError as e:
                self.status = "offline" if e.status in (401, 403, 404) else "retrying"
                self.error = {
                    "kind": "authentication" if e.status in (401, 403) else "network",
                    "code": e.code,
                    "message": str(e),
                }
                self.stats["network_failures"] += 1
                self.persist()
                if e.status in (401, 403, 404) or e.code == "VERSION_MISMATCH":
                    raise
                log.warning("%s: %s", self.client.connection.playerId, str(e))
            self.wake_network.wait(self.poll)
            self.wake_network.clear()
        return self.last_observation

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.stop.set()
        self.wake_network.set()
        if hasattr(self.agent, "cancel"):
            self.agent.cancel()
        if self.future:
            try:
                self.future.result(timeout=3)
            except Exception:
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.status = "offline"
        self.persist()
        if self.future and not self.future.done():
            self.future.add_done_callback(lambda _: self.journal.close())
        else:
            self.journal.close()
