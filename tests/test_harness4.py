"""Harness behavior with a scripted model; real subscription evidence is opt-in."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from covenant import Runtime
from test_sdk4 import Fake, Idle
from covenant.sandbox import Sandbox

spec = importlib.util.spec_from_file_location(
    "example_strategy",
    Path(__file__).parents[1] / "covenant/templates/codex/strategy.py",
)
strategy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strategy)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Runtime(Fake(), Idle(), self.tmp.name, command_timeout=0)
        self.r._accept(self.r.client.o)
        self.a = strategy.StrategicAgent()
        # Initialize real workspace/memory, but run each behavior deterministically in this test.
        with patch("threading.Thread.start"):
            self.a.on_start(self.r.ctx)

        class Box:
            def __init__(box):
                box.lock = __import__("threading").Lock()

            def path(box, path):
                p = (self.r.workspace / path).resolve()
                if not p.is_relative_to(self.r.workspace):
                    raise ValueError("escape")
                return p

            def run(box, path, preview=False):
                return {"ok": True, "preview": preview, "output": "tested", "source_unchanged": True}

        self.a.sandbox = Box()

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def test_preview_activation_versioning_rollback_and_pending_messages(self):
        self.a.tool(
            "write_file", {"path": "scripts/plan.py", "text": 'print("working")\n'}
        )
        self.assertFalse(
            self.a.tool(
                "activate_script", {"path": "scripts/plan.py", "enabled": True}
            )["ok"]
        )
        self.assertTrue(
            self.a.tool("run_script", {"path": "scripts/plan.py", "preview": True})[
                "ok"
            ]
        )
        self.assertTrue(
            self.a.tool(
                "activate_script", {"path": "scripts/plan.py", "enabled": True}
            )["ok"]
        )
        self.a.tool(
            "write_file",
            {"path": "scripts/plan.py", "text": 'raise RuntimeError("bad change")\n'},
        )
        self.assertFalse(
            self.a.tool(
                "activate_script", {"path": "scripts/plan.py", "enabled": True}
            )["ok"]
        )
        self.a.rollback("scripts/plan.py")
        self.assertEqual(
            (self.r.workspace / "scripts/plan.py").read_text(), 'print("working")\n'
        )
        self.assertFalse(self.a.memory["scripts"]["scripts/plan.py"]["active"])
        self.assertTrue((self.r.private / "script-git" / "HEAD").exists())
        self.assertFalse((self.r.workspace / ".git").is_dir())
        from covenant.state import Record

        self.a.on_message(
            self.r.ctx,
            Record({"id": "message1", "from": "p2", "to": "p1", "text": "What trade?"}),
        )
        self.assertEqual(
            json.loads(self.a.file.read_text())["messages"]["message1"]["status"],
            "received",
        )
        self.a.tool(
            "no_reply", {"message_id": "message1", "reason": "Explicit test decision"}
        )
        self.assertEqual(
            json.loads(self.a.file.read_text())["messages"]["message1"]["status"],
            "no_reply",
        )

    def test_same_turn_goal_changes_wake_tactics_and_expire(self):
        before = self.a.generation
        result = self.a.tool(
            "set_goal",
            {
                "id": "nap",
                "goal_json": json.dumps(
                    {
                        "kind": "strategy",
                        "intent": {"avoidPlayers": ["p2"]},
                        "untilTurn": 8,
                    }
                ),
            },
        )
        self.assertTrue(result["ok"])
        self.assertGreater(self.a.generation, before)
        self.assertTrue(self.a.tactical_wake.is_set())

    def test_compaction_bounds_history_and_rejects_memory_directory_escape(self):
        self.a.memory["tactics"]["completed"] = [{"target": str(i)} for i in range(200)]
        self.a.memory["receipts"] = [{"result": "x" * 100000}]
        self.assertEqual(len(self.a.objective_summary()["recentCompleted"]), 6)
        self.assertLess(len(json.dumps(self.a.receipt_summary())), 2100)
        (self.a.root / "memory").rmdir()
        (self.r.private / "secret.md").write_text("private runtime secret")
        (self.a.root / "memory").symlink_to(self.r.private, target_is_directory=True)
        self.assertNotIn("private runtime secret", json.dumps(self.a.memory_notes()))

    def test_callbacks_do_not_share_state_or_memory_between_instances(self):
        with tempfile.TemporaryDirectory() as other:
            r = Runtime(Fake(), Idle(), other, command_timeout=0)
            r._accept(r.client.o)
            b = strategy.StrategicAgent()
            with patch("threading.Thread.start"):
                b.on_start(r.ctx)
            self.a.memory["messages"]["private"] = {"status": "received"}
            self.a.persist()
            self.assertNotIn("private", b.memory["messages"])
            self.assertNotEqual(self.a.root, b.root)
            self.assertNotEqual(self.a.git_dir, b.git_dir)
            r.close()


if __name__ == "__main__":
    unittest.main()
