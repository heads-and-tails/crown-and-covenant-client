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
from covenant.harness import compact_context, CodexStrategist
from covenant.runner import FileAgent, Runner
from covenant.transport import atomic_json, ProtocolError
from covenant.validation import validate_orders


def observation():
    return json.loads((Path(__file__).parent / 'observation.json').read_text())


class ControllerTests(unittest.TestCase):
    def test_controller_returns_legal_deterministic_orders(self):
        o = observation()
        controller = TacticalController()
        for stance in ('expand', 'attack', 'defend'):
            for troop in ('militia', 'archer', 'pikeman', 'knight', 'mage', 'siege'):
                intent = {'stance': stance, 'preferredTroop': troop}
                orders = controller.plan(o, intent)
                self.assertEqual(orders, controller.plan(o, intent))
                validate_orders(orders, o)

    def test_path_reaches_other_quadrants_and_every_step_is_legal(self):
        o = observation()
        for destination in o['structures']:
            start = o['armies'][0]
            path = TacticalController().path(o, start, destination)
            previous = (start['x'], start['y'])
            for step in path:
                self.assertTrue(legal_step(o, previous, (step['x'], step['y'])))
                previous = step['x'], step['y']
            self.assertEqual(previous, (destination['x'], destination['y']))

    def test_custom_targets_route_more_than_one_tile(self):
        o = observation()
        army = o['armies'][0]
        target = {'x': 14, 'y': 14}
        orders = TacticalController().plan(o, {'targets': {army['id']: target}})
        self.assertEqual(len(orders['moves']), 1)
        self.assertTrue(legal_step(o, (2, 2), (orders['moves'][0]['x'], orders['moves'][0]['y'])))

    def test_rejects_invalid_ownership_bool_coordinates_duplicates_and_stale_turn(self):
        o = observation()
        orders = ReferenceAgent().decide(o)
        for changed in ({'turn': 0}, {'ready': 'yes'}, {'moves': [{'armyId': o['armies'][1]['id'], 'x': 13, 'y': 2}]}, {'moves': [{'armyId': o['armies'][0]['id'], 'x': True, 'y': 2}]}):
            with self.assertRaises(ValueError):
                validate_orders({**orders, **changed}, o)
        with self.assertRaises(ValueError):
            validate_orders({**orders, 'moves': orders['moves'] * 2}, o)

    def test_reference_diplomacy_does_not_repeat_greetings(self):
        agent, o = ReferenceAgent(), observation()
        self.assertEqual(len(agent.diplomacy(o)), 3)
        self.assertEqual(agent.diplomacy(o), [])


class FileAndTransportTests(unittest.TestCase):
    def test_connection_roundtrip_redacts_token_and_has_private_permissions(self):
        c = Connection('https://example.com', 'ABCD1234', 'p1', 'x' * 43)
        self.assertNotIn('x' * 43, repr(c))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'connection.json'
            c.save(path)
            self.assertEqual(Connection.load(path).token, c.token)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_connection_rejects_bad_identifiers(self):
        for game, player in (('../secrets', 'p1'), ('ABCD1234', 'p5')):
            with self.assertRaises(ValueError):
                Connection('https://example.com', game, player, 'x' * 43)

    def test_file_agent_ignores_stale_and_partially_written_orders(self):
        with tempfile.TemporaryDirectory() as d:
            agent, o = FileAgent(d), observation()
            agent.observe(o)
            self.assertEqual(json.loads((Path(d)/'observation.json').read_text())['you'], 'p1')
            self.assertIsNone(agent.decide(o))
            (Path(d)/'orders.json').write_text('{')
            self.assertIsNone(agent.decide(o))
            orders = ReferenceAgent().decide(o)
            atomic_json(Path(d)/'orders.json', {**orders, 'turn': 0})
            self.assertIsNone(agent.decide(o))
            atomic_json(Path(d)/'orders.json', orders)
            self.assertEqual(agent.decide(o), orders)

    def test_file_outbox_is_bounded_and_turn_scoped(self):
        with tempfile.TemporaryDirectory() as d:
            agent, o = FileAgent(d), observation()
            commands = [{'id': str(i), 'type': 'message', 'data': {'to': 'p2', 'text': 'hello'}} for i in range(20)]
            atomic_json(Path(d)/'outbox.json', {'turn': 1, 'commands': commands})
            self.assertEqual(len(agent.diplomacy(o)), 12)
            atomic_json(Path(d)/'outbox.json', {'turn': 0, 'commands': commands})
            self.assertEqual(agent.diplomacy(o), [])

    def test_runner_retries_orders_after_transport_failure_without_replanning(self):
        client = MagicMock()
        client.connection = Connection('http://localhost:3001', 'ABCD1234', 'p1', 'x'*43)
        o = observation()
        client.updates.return_value = {'observation': o, 'cursor': 1, 'events': []}
        client.seconds_left.return_value = 120
        client.command.return_value = {'result': {'ok': True}}
        client.orders.side_effect = [ProtocolError('network failure'), {'observation': o}]
        with tempfile.TemporaryDirectory() as d:
            runner = Runner(client, state_directory=d)
            runner.step()
            runner.future.result(timeout=2)
            with self.assertRaises(ProtocolError):
                runner.step()
            runner.step()
            self.assertEqual(client.orders.call_count, 2)
            self.assertEqual(client.orders.call_args_list[0], client.orders.call_args_list[1])
            self.assertEqual(runner.stats['turns_planned'], 1)
            runner.close()


class HarnessTests(unittest.TestCase):
    def test_context_is_bounded_and_excludes_credentials(self):
        o = observation()
        o['token'] = 'sensitive-token'
        o['messages'] = [{'id': f'm{i}', 'from': 'p2', 'to': 'p1', 'text': 'x'*5000, 'turn': 1, 'at': 0} for i in range(100)]
        ctx = compact_context(o, 'm'*9000)
        self.assertNotIn('sensitive-token', json.dumps(ctx))
        self.assertEqual(len(ctx['untrusted_private_messages']), 18)
        self.assertTrue(all(len(m['text']) <= 1200 for m in ctx['untrusted_private_messages']))
        self.assertEqual(len(ctx['your_strategic_memory']), 1500)

    def test_model_failure_uses_valid_fallback(self):
        agent, o = CodexStrategist(), observation()
        with patch.object(agent, 'think', side_effect=RuntimeError('model unavailable')):
            validate_orders(agent.decide(o), o)
        self.assertEqual(agent.metrics['fallbacks'], 1)
        self.assertEqual(agent.metrics['model_successes'], 0)

    def test_model_commands_cannot_target_self_or_execute_code(self):
        o = observation()
        commands = CodexStrategist._commands([{'kind': 'shell', 'text': 'bad'}, {'kind': 'message', 'to': 'p1', 'text': 'self'}, {'kind': 'message', 'to': 'p2', 'text': 'Trade?'}], o)
        self.assertEqual(commands, [{'type': 'message', 'data': {'to': 'p2', 'text': 'Trade?'}}])


if __name__ == '__main__':
    unittest.main()
