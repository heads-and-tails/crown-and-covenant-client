import copy
import unittest
from urllib.parse import urlparse, parse_qs
from covenant.transport import Client, Connection
from test_sdk4 import observation


class Wire(Client):
    def __init__(self):
        super().__init__(Connection("http://localhost:3001", "CAFE0001", "p1", "a" * 32))
        self.o = observation()
        self.o.update(id="CAFE0001", protocolVersion=3, serverTime=1000, eventCursor=10001,
                      capabilities=["turn-snapshots-v1"], treasury={"grain": 30})
        self.events, self.calls = [], []

    def _request(self, path, body=None, key=None):
        self.calls.append((path, body))
        if path.endswith("/map"):
            return {"size": self.o["size"], "tiles": copy.deepcopy(self.o["tiles"])}
        if "/state?" in path:
            return {k: copy.deepcopy(v) for k, v in self.o.items() if k != "tiles"}
        q = parse_qs(urlparse(path).query)
        result = {"sync": "turn-v1", "turn": self.o["turn"], "status": self.o["status"],
                  "revision": self.o["revision"], "cursor": self.o["eventCursor"], "serverTime": 1000,
                  "snapshotRequired": int(q["turn"][0]) != self.o["turn"],
                  "events": [copy.deepcopy(e) for e in self.events if e["cursor"] > int(q["after"][0])]}
        if body:
            result["result"] = {"ok": True}
        return result

    def change(self, *, set=None, arrays=None, message=None):
        self.o["revision"] += 1
        self.o["eventCursor"] += 10
        if message:
            self.events.append({"kind": "message", "cursor": self.o["eventCursor"] - 1, "data": message})
        self.events.append({"kind": "state_patch", "cursor": self.o["eventCursor"],
                            "data": {"turn": self.o["turn"], "status": self.o["status"], "set": set or {}, "arrays": arrays or {}}})


class SyncTests(unittest.TestCase):
    def count_states(self, c):
        return sum("/state?" in p for p, _ in c.calls)

    def test_local_queries_and_idle_polls_fetch_one_snapshot(self):
        c = Wire()
        for _ in range(50):
            c.state()["treasury"]["grain"] = -100
            self.assertEqual(c.state()["treasury"]["grain"], 30)
            c.updates(c._cache_cursor)
        self.assertEqual(self.count_states(c), 1)
        self.assertEqual(sum(p.endswith("/map") for p, _ in c.calls), 1)

    def test_turn_refresh_and_commands_do_not_refetch_unchanged_world(self):
        c = Wire()
        c.state()
        for _ in range(5):
            c.command("message", {"to": "p2", "text": "Question"})
        self.assertEqual(self.count_states(c), 1)
        c.o["turn"] += 1
        c.o["revision"] += 1
        c.o["armies"][0]["x"] = 1
        for _ in range(5):
            update = c.updates(c._cache_cursor)
        self.assertEqual(self.count_states(c), 2)
        self.assertEqual(update["observation"]["armies"][0]["x"], 1)

    def test_commands_patch_cache_without_consuming_callback_events(self):
        c = Wire()
        c.state()
        received = c._cache_cursor
        message = {"id": "m1", "from": "p2", "to": "p1", "text": "Offer?"}
        c.change(set={"treasury": {"grain": 24, "wood": 3}},
                 arrays={"messages": {"upsert": [message], "remove": []}}, message=message)
        response = c.command("message", {"to": "p2", "text": "Yes"})
        self.assertEqual(response["observation"]["treasury"]["wood"], 3)
        for _ in range(3):
            update = c.updates(received)
            self.assertEqual(len(update["observation"]["messages"]), 1)
            self.assertEqual([e["kind"] for e in update["events"]], ["message"])
        self.assertEqual(self.count_states(c), 1)

    def test_removed_entities_and_stale_duplicate_patches(self):
        c = Wire()
        c.state()
        initial = c._cache_cursor
        c.change(arrays={"armies": {"upsert": [{**c.o["armies"][0], "id": "merged"}], "remove": ["a1"]}})
        c.updates(initial)
        c.change(set={"treasury": {"grain": 20}})
        for _ in range(2):
            o = c.updates(initial)["observation"]
            self.assertEqual([a["id"] for a in o["armies"]], ["merged"])
            self.assertEqual(o["treasury"]["grain"], 20)

    def test_explicit_recovery_keeps_map_cached(self):
        c = Wire()
        c.state()
        c.state(refresh=True)
        self.assertEqual(self.count_states(c), 2)
        self.assertEqual(sum(p.endswith("/map") for p, _ in c.calls), 1)


if __name__ == "__main__":
    unittest.main()
