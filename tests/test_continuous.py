import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from covenant.legacy import Agent
from covenant.runner import Runner, FileAgent
from covenant.transport import Connection, ProtocolError, atomic_json
from test_v3 import observation


class FakeClient:
    def __init__(self):
        self.connection = Connection(
            "http://localhost:3013", "ABCD1234", "p1", "x" * 30
        )
        self.obs = observation()
        self.obs["you"] = "p1"
        self.obs["status"] = "active"
        self.events = []
        self.calls = []
        self.polls = 0
        self.fail = False

    def updates(self, after=0):
        self.polls += 1
        return {
            "observation": copy.deepcopy(self.obs),
            "cursor": len(self.events),
            "events": [e for e in self.events if e["cursor"] > after],
        }

    def command(self, kind, data=None, key=None):
        self.calls.append((kind, data, key))
        if self.fail:
            self.fail = False
            raise ProtocolError("temporary outage")
        return {
            "result": {"id": "receipt-" + str(len(self.calls))},
            "observation": copy.deepcopy(self.obs),
        }

    def message(self, text):
        n = len(self.events) + 1
        self.events.append(
            {
                "cursor": n,
                "kind": "message",
                "data": {
                    "id": "m" + str(n),
                    "from": "p2",
                    "to": "p1",
                    "text": text,
                    "turn": self.obs["turn"],
                    "at": 0,
                },
            }
        )


class Echo(Agent):
    def on_message(self, ctx, message):
        ctx.reply(message, "Reply to " + message["text"])


class Slow(Echo):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def on_turn(self, ctx):
        self.started.set()
        self.release.wait(3)


class ContinuousTests(unittest.TestCase):
    def run_until(self, r, predicate, seconds=4):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            r.step()
            if predicate():
                return
            time.sleep(0.015)
        self.fail("Condition was not reached before the test deadline")

    def test_network_continues_and_messages_queue_during_slow_reasoning_across_turns(
        self,
    ):
        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            a = Slow()
            r = Runner(c, a, state_directory=d)
            r.step()
            self.assertTrue(a.started.wait(1))
            c.message("first")
            r.step()
            c.obs["turn"] += 1
            c.message("second")
            r.step()
            self.assertGreaterEqual(c.polls, 3)
            self.assertEqual(len(r.journal.pending()), 2)
            a.release.set()
            self.run_until(r, lambda: sum(x[0] == "message" for x in c.calls) == 2)
            self.run_until(r, lambda: not r.future or r.future.done())
            self.assertEqual(
                len([m for m in r.journal.messages() if m["state"] == "answered"]), 2
            )
            r.close()

    def test_more_than_two_conversations_in_one_turn_and_reconnect_deduplication(self):
        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            r = Runner(c, Echo(), state_directory=d)
            for i in range(4):
                c.message(str(i))
                r.next_reasoning = 0
                self.run_until(
                    r, lambda: sum(x[0] == "message" for x in c.calls) == i + 1
                )
            self.run_until(r, lambda: r.future is None)
            r.close()
            count = len(c.calls)
            r = Runner(c, Echo(), state_directory=d)
            r.step()
            self.assertEqual(len(c.calls), count)
            self.assertEqual(r.cursor, 4)
            r.close()

    def test_file_outbox_and_receipts_survive_turns_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            r = Runner(c, FileAgent(), state_directory=d)
            file = r.agent.directory / "outbox.json"
            atomic_json(
                file,
                {
                    "commands": [
                        {
                            "id": "stable-message",
                            "type": "message",
                            "data": {"to": "p2", "text": "durable"},
                        }
                    ]
                },
            )
            r.step()
            r.step()
            c.obs["turn"] += 1
            r.step()
            self.assertEqual(sum(x[0] == "message" for x in c.calls), 1)
            r.close()
            r = Runner(c, FileAgent(), state_directory=d)
            r.step()
            self.assertEqual(sum(x[0] == "message" for x in c.calls), 1)
            self.assertTrue((r.agent.directory / "receipts.json").exists())
            r.close()

    def test_failed_transport_retries_the_same_action_id(self):
        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            r = Runner(c, FileAgent(), state_directory=d)
            r.queue(
                [
                    {
                        "type": "message",
                        "id": "one",
                        "data": {"to": "p2", "text": "hello"},
                    }
                ],
                ["file"],
            )
            c.fail = True
            r.flush_commands()
            self.assertEqual(len(r.journal.outgoing()), 1)
            r.flush_commands()
            self.assertEqual(c.calls[0], c.calls[1])
            self.assertEqual(r.journal.outgoing(), [])
            r.close()

    def test_model_failure_keeps_goals_and_reports_conversation_failure(self):
        class Broken(Agent):
            model_agent = True

            def reason(self, ctx, events):
                raise RuntimeError("model unavailable")

        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            c.message("need a reply")
            r = Runner(c, Broken(), state_directory=d)
            r.step()
            r.future.result if False else None
            self.run_until(r, lambda: r.stats["model_failures"] == 1)
            self.assertEqual(r.status, "retrying")
            self.assertEqual(r.error["kind"], "model")
            self.assertTrue(r.journal.get("goals"))
            self.assertTrue(r.journal.pending())
            self.assertFalse(any(x[0] == "message" for x in c.calls))
            r.close()

    def test_model_failure_after_reply_preserves_answered_state(self):
        class Partial(Agent):
            model_agent = True

            def reason(self, ctx, events):
                incoming = [e for e in events if e["kind"] == "message"]
                ctx.reply(incoming[0]["data"], "This answer reached the server.")
                raise RuntimeError("Later model stream failed")

        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            c.message("first")
            c.message("second")
            r = Runner(c, Partial(), state_directory=d)
            self.run_until(r, lambda: r.stats["model_failures"] == 1)
            states = {m["id"]: m["state"] for m in r.journal.messages()}
            self.assertEqual(states["message:m1"], "answered")
            self.assertEqual(states["message:m2"], "received")
            r.close()
            r = Runner(c, Agent(), state_directory=d)
            self.assertNotIn("message:m1", [e["inboxId"] for e in r.journal.pending()])
            r.close()

    def test_outbox_reply_receipt_marks_answer_even_after_callback_has_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            c.message("late receipt")
            r = Runner(c, FileAgent(), state_directory=d)
            r.journal.receive(c.events, 1)
            r.journal.mark(["message:m1"], "presented")
            r.journal.enqueue(
                "reply-key",
                {
                    "type": "message",
                    "data": {"to": "p2", "text": "answer"},
                    "replyTo": "m1",
                },
            )
            r.flush_commands()
            r.journal.retry_presented(["message:m1"])
            self.assertEqual(r.journal.messages()[0]["state"], "answered")
            r.close()

    def test_tool_context_never_contains_other_pairs_private_messages(self):
        from covenant.legacy_context import Context
        from covenant.harness import CodexStrategist

        with tempfile.TemporaryDirectory() as d:
            c = FakeClient()
            r = Runner(c, Agent(), state_directory=d)
            r._accept(c.obs)
            c.message("my own inbox")
            r.journal.receive(c.events, 1)
            ctx = Context(r, ["test"])
            a = CodexStrategist()
            a.thread_id = "test"
            result = a._tool(ctx, "get_conversation", {"player_id": "p3"}, "one")
            self.assertEqual(result["messages"], [])
            r.close()


if __name__ == "__main__":
    unittest.main()
