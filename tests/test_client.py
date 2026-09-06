import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from covenant import ReferenceAgent, TacticalController, Connection, Client
from covenant.controller import legal_step
from covenant.runner import FileAgent, Runner
from covenant.transport import atomic_json, ProtocolError
from covenant.validation import validate_orders


def observation():
    return json.loads((Path(__file__).parent / "observation.json").read_text())


class ControllerTests(unittest.TestCase):
    def test_controller_returns_legal_deterministic_orders(self):
        o = observation()
        controller = TacticalController()
        for stance in ("expand", "attack", "defend"):
            for troop in ("militia", "archer", "pikeman", "knight", "mage", "siege"):
                intent = {"stance": stance, "preferredTroop": troop}
                orders = controller.plan(o, intent)
                self.assertEqual(orders, controller.plan(o, intent))
                validate_orders(orders, o)

    def test_path_reaches_other_quadrants_and_every_step_is_legal(self):
        o = observation()
        for destination in o["structures"]:
            start = o["armies"][0]
            path = TacticalController().path(o, start, destination)
            previous = (start["x"], start["y"])
            for step in path:
                self.assertTrue(legal_step(o, previous, (step["x"], step["y"])))
                previous = step["x"], step["y"]
            self.assertEqual(previous, (destination["x"], destination["y"]))

    def test_custom_targets_route_more_than_one_tile(self):
        o = observation()
        army = o["armies"][0]
        target = {"x": 14, "y": 14}
        orders = TacticalController().plan(o, {"targets": {army["id"]: target}})
        self.assertEqual(len(orders["moves"]), 1)
        self.assertTrue(
            legal_step(o, (2, 2), (orders["moves"][0]["x"], orders["moves"][0]["y"]))
        )

    def test_rejects_invalid_ownership_bool_coordinates_duplicates_and_stale_turn(self):
        o = observation()
        orders = ReferenceAgent().decide(o)
        for changed in (
            {"turn": 0},
            {"ready": "yes"},
            {"moves": [{"armyId": o["armies"][1]["id"], "x": 13, "y": 2}]},
            {"moves": [{"armyId": o["armies"][0]["id"], "x": True, "y": 2}]},
        ):
            with self.assertRaises(ValueError):
                validate_orders({**orders, **changed}, o)
        with self.assertRaises(ValueError):
            validate_orders({**orders, "moves": orders["moves"] * 2}, o)

    def test_reference_diplomacy_does_not_repeat_greetings(self):
        agent, o = ReferenceAgent(), observation()
        self.assertEqual(len(agent.diplomacy(o)), 3)
        self.assertEqual(agent.diplomacy(o), [])


class FileAndTransportTests(unittest.TestCase):
    def test_connection_roundtrip_redacts_token_and_has_private_permissions(self):
        c = Connection("https://example.com", "ABCD1234", "p1", "x" * 43)
        self.assertNotIn("x" * 43, repr(c))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "connection.json"
            c.save(path)
            self.assertEqual(Connection.load(path).token, c.token)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_connection_rejects_bad_identifiers(self):
        for game, player in (("../secrets", "p1"), ("ABCD1234", "p5")):
            with self.assertRaises(ValueError):
                Connection("https://example.com", game, player, "x" * 43)


if __name__ == "__main__":
    unittest.main()
