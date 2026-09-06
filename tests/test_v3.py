import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from covenant.durable import Journal, agent_directory
from covenant.transport import Connection, ProtocolError
from covenant.context import Context
from covenant.harness import (
    CodexStrategist,
    TOOLS,
    restricted_config,
    repeated_acknowledgement,
)
from covenant.runner import Runner
from covenant.agent import Agent


def observation():
    o = json.loads((Path(__file__).parent / "observation.json").read_text())
    o["protocolVersion"] = 3
    o["currentOrders"] = {"armies": [], "castles": []}
    o["income"] = {r: 0 for r in o["treasury"]}
    o["revision"] = 1
    for a in o["armies"]:
        a["route"] = []
        a["orderRevision"] = 0
    for s in o["structures"]:
        s["orderRevision"] = 0
    return o


class V3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_inbox_cursor_commit_restart_and_duplicate_suppression(self):
        j = Journal(self.root)
        event = {
            "cursor": 5,
            "kind": "message",
            "data": {"id": "m1", "from": "p2", "to": "p1", "text": "First"},
        }
        j.receive([event], 5)
        j.mark(["message:m1"], "presented")
        j.close()
        j = Journal(self.root)
        self.assertEqual(j.get("cursor"), 5)
        self.assertEqual(len(j.pending()), 1)
        j.receive([event], 6)
        self.assertEqual(len(j.pending()), 1)
        j.mark(["message:m1"], "answered")
        j.close()
        j = Journal(self.root)
        self.assertEqual(j.pending(), [])
        j.close()

    def test_outbox_retains_unsent_actions_and_durable_receipts(self):
        j = Journal(self.root)
        j.enqueue("k", {"type": "message", "data": {"to": "p2", "text": "offer"}})
        j.close()
        j = Journal(self.root)
        self.assertEqual(len(j.outgoing()), 1)
        j.complete("k", {"ok": True, "result": {"id": "m1"}})
        j.enqueue("k", {"type": "message", "data": {"to": "p2", "text": "offer"}})
        self.assertEqual(j.outgoing(), [])
        with self.assertRaises(ValueError):
            j.enqueue("k", {"type": "message", "data": {"to": "p3", "text": "other"}})
        j.close()

    def test_server_and_kingdom_folders_are_distinct(self):
        a = Connection("https://one.example", "ABCD1234", "p1", "x" * 30)
        b = Connection("https://one.example", "ABCD1234", "p2", "x" * 30)
        c = Connection("https://two.example", "ABCD1234", "p1", "x" * 30)
        paths = [agent_directory(self.root, x) for x in (a, b, c)]
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(str(paths[0]).endswith("games/ABCD1234/agents/p1"))

    def test_memory_tools_reject_traversal_absolute_paths_and_symlink_escape(self):
        runner = Mock()
        runner.root = self.root / "p1"
        runner.root.mkdir()
        ctx = Context(runner, ["test"])
        ctx.write_memory("goals.md", "Capture the bridge")
        self.assertEqual(ctx.read_memory("goals.md"), "Capture the bridge")
        for name in ("../../p2/memory.md", "/tmp/foreign.md"):
            with self.assertRaises(ValueError):
                ctx.read_memory(name)
        (runner.root / "memory" / "escape").symlink_to(
            self.root, target_is_directory=True
        )
        with self.assertRaises(ValueError):
            ctx.write_memory("escape/p2.txt", "no")
        self.assertFalse((self.root / "p2.txt").exists())

    def test_only_game_tools_are_exposed_and_shared_tools_are_disabled(self):
        names = {t["name"] for t in TOOLS}
        self.assertIn("send_message", names)
        self.assertIn("set_goal", names)
        self.assertNotIn("exec_command", names)
        config = restricted_config(self.root)
        self.assertFalse(config["features.shell_tool"])
        self.assertFalse(config["features.apps"])
        self.assertFalse(config["features.plugins"])
        self.assertEqual(config["web_search"], "disabled")

    def test_old_plan_adapter_uses_original_entity_revision(self):
        client = Mock()
        client.connection = Connection(
            "https://one.example", "ABCD1234", "p1", "x" * 30
        )
        r = Runner(client, Agent(), state_directory=self.root)
        o = observation()
        o["you"] = "p1"
        a = next(x for x in o["armies"] if x["owner"] == "p1")
        patch = r._patch(
            {
                "turn": o["turn"],
                "moves": [{"armyId": a["id"], "x": a["x"] + 1, "y": a["y"]}],
                "production": [],
            },
            o,
        )
        self.assertEqual(patch["armies"][0]["revision"], 0)
        self.assertEqual(patch["armies"][0]["from"], {"x": a["x"], "y": a["y"]})
        self.assertIsNone(r._patch({"turn": o["turn"] - 1}, o))
        r.close()

    def test_bad_model_goals_fail_before_persistence_and_saved_corruption_recovers(
        self,
    ):
        from covenant.goals import validate_goal

        for intent in (
            {"reserves": 6},
            {"composition": "militia"},
            {"targets": {"a1": "castle"}},
            {"castleTargets": "s1"},
        ):
            with self.assertRaises(ValueError):
                validate_goal({"kind": "strategy", "intent": intent})
        client = Mock()
        client.connection = Connection(
            "https://one.example", "ABCD1234", "p1", "x" * 30
        )
        r = Runner(client, Agent(), state_directory=self.root)
        with self.assertRaises(ValueError):
            r.set_goal("bad", {"kind": "strategy", "intent": {"reserves": 6}})
        self.assertNotIn("bad", r.journal.get("goals", {}))
        r.journal.set("goals", {"bad": {"kind": "strategy", "intent": {"reserves": 6}}})
        r.close()
        r = Runner(client, Agent(), state_directory=self.root)
        self.assertEqual(r.journal.get("goals"), {})
        self.assertTrue(any(e["kind"] == "goal" for e in r.journal.pending()))
        r.close()

    def test_no_reply_records_a_reason_without_sending(self):
        j = Journal(self.root)
        j.receive(
            [
                {
                    "kind": "message",
                    "cursor": 1,
                    "data": {"id": "m1", "from": "p2", "to": "p1", "text": "Thanks"},
                }
            ],
            1,
        )
        runner = Mock()
        runner.root = self.root
        runner.journal = j
        ctx = Context(runner, ["test"])
        ctx.no_reply("m1", "Acknowledgement, no further decision")
        self.assertEqual(j.messages()[0]["state"], "no_reply")
        self.assertEqual(j.outgoing(), [])
        j.close()

    def test_model_acknowledgement_guard_preserves_human_questions_and_new_terms(self):
        o = {"you": "p1", "turn": 4, "players": [{"id": "p2", "controller": "luna"}]}
        previous = {
            "event": {
                "data": {
                    "id": "m1",
                    "from": "p1",
                    "to": "p2",
                    "turn": 4,
                    "text": "Agreed. I will leave s14 clear and take Duskfort.",
                }
            }
        }
        incoming = {
            "id": "m2",
            "from": "p2",
            "to": "p1",
            "turn": 4,
            "text": "Confirmed. Proceed toward Duskfort; I will secure s14.",
        }
        reply = "Recorded. I will keep s14 clear and proceed toward Duskfort."
        self.assertTrue(repeated_acknowledgement(o, [previous], incoming, reply))
        self.assertFalse(repeated_acknowledgement(o, [], incoming, reply))
        for text in [
            "Confirmed. Could you send grain?",
            "Confirmed. The new price is 6 grain.",
            "Confirmed. I captured the western castle.",
        ]:
            self.assertFalse(
                repeated_acknowledgement(
                    o, [previous], {**incoming, "text": text}, reply
                )
            )
        o["players"][0]["controller"] = "human"
        self.assertFalse(repeated_acknowledgement(o, [previous], incoming, reply))

    def test_suppressed_model_reply_is_durable_and_sends_no_command(self):
        client = Mock()
        client.connection = Connection(
            "https://one.example", "ABCD1234", "p1", "x" * 30
        )
        runner = Runner(client, Agent(), state_directory=self.root)
        runner.last_observation = {
            "you": "p1",
            "turn": 4,
            "players": [{"id": "p2", "controller": "luna"}],
        }
        for i, (sender, recipient, text) in enumerate(
            [
                ("p1", "p2", "Agreed. I will leave Duskfort clear."),
                ("p2", "p1", "Confirmed. I will proceed to Duskfort."),
            ],
            1,
        ):
            runner.journal.receive(
                [
                    {
                        "kind": "message",
                        "cursor": i,
                        "data": {
                            "id": f"m{i}",
                            "from": sender,
                            "to": recipient,
                            "turn": 4,
                            "text": text,
                        },
                    }
                ],
                i,
            )
        model = CodexStrategist()
        ctx = Context(runner, ["test"])
        result = model._tool(
            ctx,
            "send_message",
            {
                "to": "p2",
                "reply_to": "m2",
                "text": "Recorded. I will leave Duskfort clear.",
            },
            "ack-test",
        )
        self.assertFalse(result["sent"])
        self.assertEqual(runner.journal.messages()[-1]["state"], "no_reply")
        self.assertEqual(runner.journal.outgoing(), [])
        self.assertEqual(model._tool(ctx, "send_message", {}, "ack-test"), result)
        runner.close()


if __name__ == "__main__":
    unittest.main()
