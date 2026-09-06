"""Public cached queries and durable immediate commands."""

import hashlib
import json
import threading
import time
import uuid
from .state import GameState, Record, Army, Structure, RouteOrder, ProductionOrder, thaw
from .pathing import routes, pos


def position(value):
    if isinstance(value, (tuple, list)) and len(value) == 2:
        value = {"x": value[0], "y": value[1]}
    if not isinstance(value, (dict, Record)):
        raise ValueError("Use a tile, {x, y}, or (x, y).")
    if any(type(value[k]) is not int for k in ("x", "y")):
        raise ValueError("Tile coordinates must be integers.")
    return {"x": value["x"], "y": value["y"]}


class Context:
    def __init__(self, runtime, event_id="background"):
        self._runtime, self._event_id, self._sequence = runtime, event_id, 0
        self._lock = threading.Lock()
        self.workspace = runtime.workspace
        self.instance_id = runtime.instance_id
        self.resumed = runtime.resumed
        self.config = Record(runtime.config)

    def get_state(self):
        with self._runtime.lock:
            if self._runtime.snapshot is None:
                self._runtime.snapshot = GameState(self._runtime.observation)
            return self._runtime.snapshot

    def get_conversation(self, player_id, after=None):
        return tuple(Record(m) for m in self._runtime.conversation(player_id, after))

    def get_receipt(self, command_id):
        value = self._runtime.journal.receipt(command_id)
        return Record(value or {"id": command_id, "status": "pending", "ok": False})

    def _command(self, kind, data, *, stable_key=None, reply_to=None):
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        identity = stable_key or (
            [self._event_id, sequence, kind, thaw(data)]
            if self._event_id != "background"
            else uuid.uuid4().hex
        )
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        command = {"type": kind, "data": thaw(data)}
        if reply_to:
            command["replyTo"] = reply_to
        self._runtime.journal.enqueue(key, command)
        self._runtime.wake.set()
        until = time.monotonic() + self._runtime.command_timeout
        while time.monotonic() < until and not self._runtime.stop.is_set():
            receipt = self._runtime.journal.receipt(key)
            if receipt:
                return Record(receipt)
            self._runtime.stop.wait(0.03)
        return Record({"id": key, "status": "pending", "ok": False})

    def find_path(self, army, destination, avoid_structures=True):
        self._entity(army, Army)
        o, end = self.get_state().to_dict(), position(destination)
        blocked = (
            {pos(s) for s in o["structures"] if pos(s) != pos(end)}
            if avoid_structures
            else set()
        )
        path = routes(o, pos(army), blocked)(pos(end))
        if not path and pos(army) != pos(end):
            raise ValueError(
                "No legal route to this destination without crossing intermediate structures."
            )
        return tuple(Record({"x": x, "y": y}) for x, y in path)

    def _entity(self, value, cls):
        if not isinstance(value, cls):
            raise TypeError(
                f"Pass a {cls.__name__} from get_state() to preserve its observed revision."
            )
        if value.owner != self.get_state().you:
            raise ValueError("This entity belongs to another kingdom.")

    def move(self, army, destination):
        return self.set_route(army, self.find_path(army, destination))

    def set_route(self, army, steps):
        return self.submit_orders([RouteOrder(army, steps)])

    def hold(self, army):
        return self.set_route(army, [])

    def recruit(self, castle, troop, quantity=1):
        return self.submit_orders([ProductionOrder(castle, troop, quantity)])

    def submit_orders(self, orders):
        patch = {"armies": [], "castles": []}
        for order in orders:
            if isinstance(order, RouteOrder):
                self._entity(order.army, Army)
                patch["armies"].append(
                    {
                        "armyId": order.army.id,
                        "from": position(order.army),
                        "route": [position(p) for p in order.steps],
                        "revision": order.army.get("orderRevision", 0),
                    }
                )
            elif isinstance(order, ProductionOrder):
                self._entity(order.castle, Structure)
                patch["castles"].append(
                    {
                        "castleId": order.castle.id,
                        "production": (
                            {"troop": order.troop, "count": order.quantity}
                            if order.troop
                            else None
                        ),
                        "revision": order.castle.get("orderRevision", 0),
                    }
                )
            else:
                raise TypeError(
                    "Use RouteOrder(army, steps) or ProductionOrder(castle, troop, quantity)."
                )
        return self._command("orders", patch)

    def send_message(self, player_id, text):
        return self._command("message", {"to": player_id, "text": text})

    def reply(self, message, text):
        return self._command(
            "message",
            {"to": message["from"], "text": text},
            stable_key=["reply", message["id"], text],
            reply_to=message["id"],
        )

    def propose_trade(self, player_id, give, request):
        return self._command(
            "offer", {"to": player_id, "kind": "trade", "give": give, "want": request}
        )

    def respond_trade(self, offer, decision):
        if decision not in ("accept", "reject", "cancel"):
            raise ValueError("Use accept, reject, or cancel.")
        return self._command("answer", {"offerId": offer["id"], "answer": decision})

    def set_ready(self, turn, ready=True):
        return self._command("ready" if ready else "unready", {"turn": turn})

    def report_status(self, status, error=None):
        if status not in (
            "waiting",
            "connected",
            "thinking",
            "responding",
            "retrying",
            "fallback",
            "offline",
        ):
            raise ValueError("Unknown controller status.")
        self._runtime.status = status
        self._runtime.error = thaw(error) if error else None
        self._runtime.persist()
