import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from test_client import observation
from covenant.agent import Agent, ReferenceAgent
from covenant.runner import Runner, FileAgent
from covenant.transport import Connection, atomic_json
from covenant.harness import compact_context, CodexStrategist, output_schema


def client(o):
    c = MagicMock()
    c.connection = Connection('http://localhost:3001', 'ABCD1234', 'p1', 'x' * 43)
    c.updates.return_value = {'observation': o, 'cursor': 0, 'events': []}
    c.seconds_left.return_value = 120
    c.command.return_value = {'result': {'ok': True}}
    c.orders.return_value = {'observation': o}
    return c


def finish(r):
    if r.future:
        r.future.result(timeout=3)
    r.step()


class SlowAgent(Agent):
    def __init__(self):
        self.release = threading.Event()
        self.seen = []
    def on_turn(self, o):
        self.release.wait(3)
        return ReferenceAgent().decide(o)
    def on_events(self, o, events):
        self.seen.extend(events)
        return [{'type': 'message', 'data': {'to': 'p2', 'text': 'Answer ' + str(e['cursor'])}} for e in events if e['kind'] == 'message']


class ContinuousTests(unittest.TestCase):
    def test_network_deadlines_and_cross_turn_conversation_continue_while_thinking(self):
        with tempfile.TemporaryDirectory() as d:
            o, agent = observation(), SlowAgent()
            c = client(o)
            r = Runner(c, agent, state_directory=d)
            try:
                r.step()
                c.seconds_left.return_value = 5
                c.updates.return_value['cursor'] = 1
                c.updates.return_value['events'] = [{'cursor': 1, 'kind': 'message', 'data': {'to': 'p1', 'from': 'p2', 'text': 'Trade?'}}]
                start = time.monotonic()
                r.step()
                self.assertLess(time.monotonic() - start, .5)
                self.assertEqual(c.orders.call_count, 1)
                self.assertEqual(r.stats['deadline_fallbacks'], 1)
                next_turn = copy.deepcopy(o)
                next_turn['turn'] = 2
                c.updates.return_value = {'observation': next_turn, 'cursor': 2, 'events': []}
                c.seconds_left.return_value = 120
                agent.release.set()
                finish(r)
                self.assertEqual(r.stats['stale_orders_discarded'], 1)
                r.last_model_at = 0
                r.step()
                finish(r)
                self.assertTrue(any(e['cursor'] == 1 for e in agent.seen))
                self.assertTrue(any(call.args[0] == 'message' for call in c.command.call_args_list))
                self.assertTrue(all(call.args[0]['turn'] in (1, 2) for call in c.orders.call_args_list))
            finally:
                agent.release.set()
                r.close()

    def test_more_than_two_event_bursts_are_handled_and_cursor_recovers(self):
        class Conversational(Agent):
            def on_events(self, o, events):
                return [{'type': 'message', 'data': {'to': 'p2', 'text': f"Follow-up {e['cursor']}"}} for e in events if e['kind'] == 'message']
        with tempfile.TemporaryDirectory() as d:
            c, a = client(observation()), Conversational()
            r = Runner(c, a, state_directory=d)
            r.step(); finish(r)
            for i in range(1, 5):
                c.updates.return_value = {'observation': observation(), 'cursor': i, 'events': [{'cursor': i, 'kind': 'message', 'data': {'from': 'p2', 'to': 'p1'}}]}
                r.last_model_at = 0
                r.step(); finish(r)
            self.assertEqual(c.command.call_count, 4)
            r.close()
            resumed = Runner(c, a, state_directory=d)
            self.assertEqual(resumed.cursor, 4)
            self.assertEqual(len(resumed.sent), 4)
            resumed.close()

    def test_file_outbox_survives_turn_boundary_and_restart_without_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d) / 'files'
            a, o = FileAgent(directory), observation()
            atomic_json(directory / 'outbox.json', {'commands': [{'id': 'unique-1', 'type': 'message', 'data': {'to': 'p2', 'text': 'Promise'}}]})
            c = client(o)
            r = Runner(c, a, state_directory=d)
            r.step()
            self.assertEqual(c.command.call_count, 1)
            r.close()
            o['turn'] = 2
            resumed = Runner(c, a, state_directory=d)
            resumed.step()
            self.assertEqual(c.command.call_count, 1)
            receipts = json.loads((directory / 'receipts.json').read_text())
            self.assertTrue(receipts['commands'][0]['ok'])
            resumed.close()

    def test_pending_delivery_is_durable_and_private_contexts_are_filtered(self):
        o = observation()
        o['messages'] = [{'id': 'private', 'from': 'p2', 'to': 'p3', 'text': 'SECRET'}, {'id': 'mine', 'from': 'p2', 'to': 'p1', 'text': 'My message'}]
        o['players'][1]['token'] = 'CREDENTIAL'
        ctx = compact_context(o)
        self.assertNotIn('SECRET', json.dumps(ctx))
        self.assertNotIn('CREDENTIAL', json.dumps(ctx))
        self.assertEqual(len(ctx['untrusted_private_messages']), 1)
        with tempfile.TemporaryDirectory() as d:
            c = client(o)
            r = Runner(c, state_directory=d)
            r.queue([{'type': 'message', 'data': {'to': 'p2', 'text': 'Durable'}}], 'event-1')
            r.persist(); r.close()
            resumed = Runner(c, state_directory=d)
            resumed.flush_commands(o)
            self.assertEqual(c.command.call_count, 1)
            resumed.close()

    def test_informal_cooperation_does_not_create_formal_commands(self):
        o = observation()
        actions = [{'kind': kind, 'to': 'p2', 'give': {}, 'want': {}} for kind in ('alliance', 'break_alliance', 'shell')]
        self.assertEqual(CodexStrategist._commands(actions, o), [])


class StrategySchemaTests(unittest.TestCase):
    def test_dynamic_targets_do_not_constrain_free_text_or_other_context_fields(self):
        o = observation()
        props = output_schema(o)['properties']
        self.assertNotIn('enum', props['memory'])
        self.assertNotIn('enum', props['diplomacy']['items']['properties']['text'])
        self.assertNotIn('enum', props['diplomacy']['items']['properties']['offerId'])
        self.assertEqual(props['defensivePriorities']['items']['enum'], [s['id'] for s in o['structures'] if s['kind'] == 'castle' and s['owner'] == o['you']])
        self.assertEqual(props['armyObjectives']['items']['properties']['armyId']['enum'], [a['id'] for a in o['armies'] if a['owner'] == o['you']])
