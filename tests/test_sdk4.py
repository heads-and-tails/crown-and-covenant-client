import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from covenant import Agent, Runtime, GameState, Army, RouteOrder, ProductionOrder
from covenant.catalog import discover, initialize, pin
from covenant.transport import ProtocolError


def observation():
    return {
        "id": "ABCDEFGH",
        "you": "p1",
        "status": "active",
        "turn": 1,
        "revision": 1,
        "deadline": 9999999999999,
        "size": 5,
        "players": [
            {
                "id": "p1",
                "name": "One",
                "resources": {"grain": 30},
                "eliminated": False,
            },
            {"id": "p2", "name": "Two", "eliminated": False},
        ],
        "tiles": [
            {"x": x, "y": y, "terrain": "plains"} for y in range(5) for x in range(5)
        ],
        "structures": [
            {
                "id": "c1",
                "kind": "castle",
                "owner": "p1",
                "x": 0,
                "y": 0,
                "production": None,
                "orderRevision": 0,
            },
            {
                "id": "r1",
                "kind": "resource",
                "resource": "wood",
                "owner": None,
                "x": 1,
                "y": 0,
            },
        ],
        "armies": [
            {
                "id": "a1",
                "owner": "p1",
                "x": 0,
                "y": 0,
                "troops": {"militia": 18},
                "route": [],
                "orderRevision": 0,
            }
        ],
        "currentOrders": {"armies": [], "castles": []},
        "income": {"grain": 6},
        "rules": {},
        "offers": [],
        "messages": [],
    }


class Idle(Agent):
    def on_start(self, ctx):
        pass

    def on_message(self, ctx, message):
        pass

    def on_turn(self, ctx, turn):
        pass

    def on_event(self, ctx, event):
        pass

    def on_stop(self, ctx, reason):
        pass


class Fake:
    def __init__(self):
        self.o = observation()
        self.events = []
        self.calls = []
        self.polls = 0
        self.fail = False

    def updates(self, cursor=0):
        self.polls += 1
        return {
            "observation": copy.deepcopy(self.o),
            "events": copy.deepcopy(self.events),
            "cursor": len(self.events),
        }

    def command(self, kind, data, key):
        self.calls.append((kind, data, key))
        if self.fail:
            raise ProtocolError("Outdated army", 409, "STALE_ORDER")
        return {"result": {"accepted": True}, "observation": copy.deepcopy(self.o)}


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = Fake()
        self.r = Runtime(
            self.client, Idle(), self.tmp.name, poll=0.01, command_timeout=0
        )
        self.r._accept(self.client.o)

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def test_required_callbacks_and_immutable_queries(self):
        with self.assertRaises(TypeError):
            Agent()
        s = self.r.ctx.get_state()
        a = s.get_army("a1")
        self.assertIsInstance(a, Army)
        with self.assertRaises(TypeError):
            a["x"] = 4
        with self.assertRaises((TypeError, AttributeError)):
            a.x = 4
        self.assertEqual(s.get_structures(owner="me")[0].id, "c1")
        self.assertNotIn("resources", s.get_player("p2"))
        self.client.o["turn"] = 2
        self.r._accept(self.client.o)
        self.assertEqual(s.turn, 1)

    def test_partial_revision_orders_and_pending_receipts(self):
        s = self.r.ctx.get_state()
        receipt = self.r.ctx.submit_orders(
            [
                RouteOrder(s.get_army("a1"), [(0, 1), (1, 1)]),
                ProductionOrder(s.get_structure("c1"), "militia", 2),
            ]
        )
        self.assertEqual(receipt.status, "pending")
        self.r.flush()
        self.assertEqual(self.r.ctx.get_receipt(receipt.id).status, "confirmed")
        data = self.client.calls[0][1]
        self.assertEqual(data["armies"][0]["revision"], 0)
        self.assertEqual(data["armies"][0]["from"], {"x": 0, "y": 0})
        self.client.fail = True
        receipt = self.r.ctx.hold(s.get_army("a1"))
        self.r.flush()
        self.assertEqual(self.r.ctx.get_receipt(receipt.id).code, "STALE_ORDER")

    def test_represented_callback_dedupes_identical_commands_but_not_new_observations(
        self,
    ):
        from covenant.context import Context

        first = Context(self.r, "turn:1")
        same = Context(self.r, "turn:1")
        changed = Context(self.r, "turn:1")
        a = first.get_state().get_army("a1")
        one = first.hold(a)
        self.assertEqual(one.id, same.hold(a).id)
        self.client.o["armies"][0]["orderRevision"] = 2
        self.r._accept(self.client.o)
        self.assertNotEqual(one.id, changed.hold(changed.get_state().get_army("a1")).id)

    def test_human_paths_and_explicit_structure_traversal(self):
        a = self.r.ctx.get_state().get_army("a1")
        path = self.r.ctx.find_path(a, (2, 0))
        self.assertNotIn((1, 0), [(p.x, p.y) for p in path])
        path = self.r.ctx.find_path(a, (2, 0), False)
        self.assertEqual([(p.x, p.y) for p in path], [(1, 0), (2, 0)])

    def test_callback_network_independence_and_crash_recovery(self):
        seen = []
        entered = threading.Event()

        class Slow(Idle):
            def on_message(self, ctx, message):
                seen.append(message.id)
                entered.set()
                time.sleep(0.15)

            def on_turn(self, ctx, turn):
                seen.append(("turn", turn))

            def on_event(self, ctx, event):
                seen.append(("event", event.kind))

        self.r.agent = Slow()
        self.client.events = [
            {
                "kind": "message",
                "cursor": 1,
                "data": {"id": "m1", "from": "p2", "to": "p1", "text": "Trade?"},
            }
        ]
        t = threading.Thread(target=self.r.run, kwargs={"max_seconds": 0.35})
        t.start()
        self.assertTrue(entered.wait(1))
        before = self.client.polls
        time.sleep(0.08)
        self.assertGreater(self.client.polls, before + 3)
        t.join(3)
        self.assertEqual(seen.count("m1"), 1)
        self.assertEqual(seen.count(("turn", 1)), 1)
        self.assertNotIn(("event", "message"), seen)
        self.r.close()
        self.r = Runtime(self.client, Idle(), self.tmp.name)
        self.assertTrue(self.r.resumed)
        self.assertEqual(self.r.journal.pending(), [])
        self.r.journal.receive(
            [{"kind": "message", "cursor": 2, "data": {"id": "m2"}}], 2
        )
        self.r.journal.mark(["message:m2"], "presented")
        self.r.close()
        self.r = Runtime(self.client, Idle(), self.tmp.name)
        self.assertEqual(self.r.journal.pending()[0]["inboxId"], "message:m2")

    def test_catalog_does_not_execute_and_pins_versions(self):
        root = Path(self.tmp.name)
        initialize(root)
        items = discover(root / "agents")
        self.assertEqual(
            {x.public["id"] for x in items}, {"tactical", "openai", "codex"}
        )
        definition = next(d for d in items if d.public["id"] == "tactical")
        p = definition.root / "evil.py"
        p.write_text("raise RuntimeError('Do not import during discovery')")
        definition = next(
            d for d in discover(root / "agents") if d.public["id"] == "tactical"
        )
        self.assertTrue(definition.public["available"])
        pinned = pin(definition, root / "pin")
        p.write_text("pass")
        self.assertIn("raise", (pinned / "evil.py").read_text())
        new = next(d for d in discover(root / "agents") if d.public["id"] == "tactical")
        with self.assertRaises(ValueError):
            pin(new, root / "pin")

    def test_openai_tool_roundtrip_without_api_spend(self):
        spec = importlib.util.spec_from_file_location(
            "openai_example",
            Path(__file__).parents[1] / "covenant/templates/openai/model.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        responses = [
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "get_state",
                        "arguments": "{}",
                    }
                ],
            },
            {
                "status": "completed",
                "output": [{"type": "message", "role": "assistant", "content": []}],
            },
        ]
        bodies = []
        import io

        def request(r, timeout):
            bodies.append(json.loads(r.data))
            return io.StringIO(json.dumps(responses.pop(0)))

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-real"}),
            patch("urllib.request.urlopen", request),
        ):
            m = module.Model(self.r.ctx)
            result = m.run(
                "Test",
                "Hello",
                [
                    {
                        "name": "get_state",
                        "description": "State",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                ],
                lambda n, a: {"turn": 1},
            )
        self.assertEqual(result["tool_calls"], 1)
        self.assertFalse(bodies[0]["store"])
        self.assertEqual(bodies[1]["input"][-1]["type"], "function_call_output")


if __name__ == "__main__":
    unittest.main()
