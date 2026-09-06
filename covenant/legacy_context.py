"""Synchronous scripting interface. Networking runs in the runner's separate thread."""

from __future__ import annotations
import copy
import hashlib
import json
import time
from pathlib import Path
from .transport import ProtocolError, atomic_json
from .controller import TacticalController


class Context:
    def __init__(self, runner, scope):
        self.runner, self.scope, self.sequence = runner, scope, 0
        self.root = runner.root

    def get_state(self):
        with self.runner.lock:
            return copy.deepcopy(self.runner.last_observation)

    def command(self, kind, data=None, action_id=None, *, reply_to=None):
        if kind not in ("orders", "message", "offer", "answer", "ready", "unready"):
            raise ValueError("Unsupported agent command.")
        self.sequence += 1
        key = hashlib.sha256(
            json.dumps(
                [
                    (
                        "durable-reply"
                        if action_id and action_id.startswith("reply:")
                        else self.scope
                    ),
                    action_id or self.sequence,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        command = {"type": kind, "data": data or {}}
        if reply_to:
            command["replyTo"] = reply_to
        self.runner.journal.enqueue(key, command)
        self.runner.wake_network.set()
        until = time.monotonic() + 45
        while not self.runner.stop.is_set() and time.monotonic() < until:
            receipt = self.runner.journal.receipt(key)
            if receipt:
                if not receipt["ok"]:
                    raise ProtocolError(
                        receipt["error"],
                        receipt.get("status", 0),
                        receipt.get("code", "ACTION_FAILED"),
                    )
                return receipt
            self.runner.stop.wait(0.1)
        return {
            "ok": False,
            "pending": True,
            "key": key,
            "error": "Awaiting server receipt. The durable outbox will retry; do not send a duplicate.",
        }

    def set_orders(self, patch, action_id=None):
        return self.command("orders", patch, action_id)

    def send_message(self, to, text, action_id=None):
        return self.command("message", {"to": to, "text": text}, action_id)

    def reply(self, message, text):
        return self.command(
            "message",
            {"to": message["from"], "text": text},
            "reply:" + message["id"] + ":" + hashlib.sha256(text.encode()).hexdigest(),
            reply_to=message["id"],
        )

    def no_reply(self, message_id, reason):
        if not reason.strip():
            raise ValueError("Explain why a reply is unnecessary.")
        self.runner.journal.mark(["message:" + message_id], "no_reply", reason[:500])
        return {"ok": True}

    def offer(self, to, give, want):
        return self.command(
            "offer", {"to": to, "kind": "trade", "give": give, "want": want}
        )

    def answer(self, offer_id, answer):
        return self.command("answer", {"offerId": offer_id, "answer": answer})

    def set_goal(self, goal_id, goal):
        self.runner.set_goal(goal_id, goal)
        return {"ok": True, "goals": self.runner.journal.get("goals", {})}

    def goals(self):
        return self.runner.journal.get("goals", {})

    def route(self, army_id, destination, avoid_structures=True):
        o = self.get_state()
        a = next(
            (a for a in o["armies"] if a["id"] == army_id and a["owner"] == o["you"]),
            None,
        )
        if not a:
            raise ValueError("Unknown owned army.")
        from .controller import routes, pos

        blocked = (
            {pos(s) for s in o["structures"] if pos(s) != pos(destination)}
            if avoid_structures
            else set()
        )
        path = routes(o, pos(a), blocked)(pos(destination))
        if not path and pos(a) != pos(destination):
            raise ValueError("Destination unreachable.")
        return [{"x": x, "y": y} for x, y in path]

    def move(self, army_id, route):
        o = self.get_state()
        a = next(
            (a for a in o["armies"] if a["id"] == army_id and a["owner"] == o["you"]),
            None,
        )
        if not a:
            raise ValueError("Unknown owned army.")
        return self.set_orders(
            {
                "armies": [
                    {
                        "armyId": army_id,
                        "from": {"x": a["x"], "y": a["y"]},
                        "route": route,
                        "revision": a.get("orderRevision", 0),
                    }
                ]
            }
        )

    def recruit(self, castle_id, troop, count=1):
        o = self.get_state()
        s = next(
            (
                s
                for s in o["structures"]
                if s["id"] == castle_id
                and s["owner"] == o["you"]
                and s["kind"] == "castle"
            ),
            None,
        )
        if not s:
            raise ValueError("Unknown owned castle.")
        return self.set_orders(
            {
                "castles": [
                    {
                        "castleId": castle_id,
                        "production": (
                            {"troop": troop, "count": count} if troop else None
                        ),
                        "revision": s.get("orderRevision", 0),
                    }
                ]
            }
        )

    def memory_path(self, name):
        # Only user memory files; runtime credentials, inboxes and diagnostics are not writable tools.
        if not isinstance(name, str) or not name or len(name) > 160:
            raise ValueError("Invalid memory name.")
        base = (self.root / "memory").resolve()
        base.mkdir(exist_ok=True)
        p = (base / name).resolve()
        if not p.is_relative_to(base) or p == base:
            raise ValueError("Memory paths must stay in this agent’s folder.")
        return p

    def read_memory(self, name):
        p = self.memory_path(name)
        return p.read_text(encoding="utf-8")[:64000] if p.exists() else ""

    def write_memory(self, name, text):
        if len(text.encode()) > 64000:
            raise ValueError(
                "Memory files are limited to 64 KB. Split topics into separate files."
            )
        p = self.memory_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        temp = p.with_suffix(p.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(p)
        return {"ok": True, "path": str(p.relative_to(self.root))}

    def list_memory(self):
        return [
            str(p.relative_to(self.root / "memory"))
            for p in (self.root / "memory").rglob("*")
            if p.is_file() and not p.is_symlink()
        ]
