"""Continuous network loop with isolated, serial reasoning and durable event receipts."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from .agent import Agent, ReferenceAgent
from .harness import CodexStrategist
from .transport import Client, ProtocolError, atomic_json
from .validation import validate_orders

log = logging.getLogger('covenant')
DIPLOMACY = {'message', 'offer', 'answer'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class FileAgent(Agent):
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def observe(self, observation, events=None, cursor=0):
        atomic_json(self.directory / 'observation.json', observation)
        atomic_json(self.directory / 'events.json', {'cursor': cursor, 'events': events or []})

    def decide(self, observation):
        try:
            orders = json.loads((self.directory / 'orders.json').read_text())
            if orders.get('turn') == observation['turn']:
                return validate_orders(orders, observation)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, TypeError) as error:
            atomic_json(self.directory / 'error.json', {'turn': observation['turn'], 'error': str(error)})
        return None

    def diplomacy(self, observation):
        try:
            outbox = json.loads((self.directory / 'outbox.json').read_text())
            # The optional legacy turn field remains supported. Omit it for a continuous outbox.
            if 'turn' in outbox and outbox['turn'] != observation['turn']:
                return []
            return [c for c in outbox.get('commands', [])[:12] if c.get('type') in DIPLOMACY and isinstance(c.get('id'), str)]
        except (FileNotFoundError, ValueError, AttributeError, TypeError):
            return []


class Runner:
    def __init__(self, client: Client, agent: Agent | None = None, poll: float = 2, fast: bool = False, state_directory: str | Path = '.covenant'):
        self.client, self.agent = client, agent or ReferenceAgent()
        self.poll, self.fast = max(.25, poll), fast
        self.root = Path(state_directory) / client.connection.gameId / client.connection.playerId
        self.root.mkdir(parents=True, exist_ok=True)
        self.last_turn = -1
        self.planned_turn = -1
        self.cursor = 0
        self.pending_events, self.pending_commands, self.action_results = [], [], []
        self.sent = {}
        self.pending_orders = None
        self.last_file_digest = ''
        self.submitted_turn = -1
        self.stats = {'turns_planned': 0, 'orders_sent': 0, 'diplomatic_actions': 0, 'errors': 0, 'deadline_fallbacks': 0, 'agent_fallbacks': 0, 'stale_orders_discarded': 0, 'events_received': 0}
        self.status = 'connected'
        self.fallback = ReferenceAgent()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'reasoning-{client.connection.playerId}')
        self.future = None
        self.last_model_at = 0.
        self.response_until = 0.
        self.stop = threading.Event()
        self.last_observation = None
        try:
            saved = json.loads((self.root / 'runner.json').read_text())
            for key in ('cursor', 'last_turn', 'planned_turn', 'pending_events', 'pending_commands', 'action_results', 'sent', 'pending_orders', 'last_file_digest', 'submitted_turn'):
                if key in saved:
                    setattr(self, key, saved[key])
            self.stats.update(saved.get('stats', {}))
            # An interrupted turn call must be resumed; command keys are stable across restarts.
            if saved.get('thinking_turn') is not None:
                self.planned_turn = -1
        except (ValueError, OSError):
            pass

    def persist(self):
        state = {k: getattr(self, k) for k in ('cursor', 'last_turn', 'planned_turn', 'pending_events', 'pending_commands', 'action_results', 'sent', 'pending_orders', 'last_file_digest', 'submitted_turn', 'stats')}
        state['thinking_turn'] = self.future_meta['turn'] if self.future else None
        atomic_json(self.root / 'runner.json', state)
        atomic_json(self.root / 'stats.json', {**self.stats, **(self.agent.metrics if isinstance(self.agent, CodexStrategist) else {}), 'status': self.status})

    @staticmethod
    def meaningful(event, you):
        data, kind = event.get('data', {}), event.get('kind')
        if kind == 'message':
            return data.get('to') == you and data.get('from') != you
        if kind == 'trade':
            return data.get('to') == you or (data.get('from') == you and data.get('status') != 'pending')
        if kind == 'report':
            return data.get('kind') in ('capture', 'battle', 'elimination', 'victory')
        return kind in ('reset', 'turn')

    def queue(self, commands, scope):
        for index, command in enumerate(commands[:16]):
            if not isinstance(command, dict) or command.get('type') not in DIPLOMACY:
                continue
            key = digest(['file', command['id']]) if isinstance(command.get('id'), str) else digest([scope, index, command])
            if key not in self.sent and not any(c['key'] == key for c in self.pending_commands):
                self.pending_commands.append({'key': key, 'command': command})

    def flush_commands(self, obs):
        for entry in self.pending_commands[:12]:
            command, key = entry['command'], entry['key']
            try:
                result = self.client.command(command['type'], command.get('data'), key)
                receipt = {'ok': True, 'id': command.get('id'), 'key': key, 'result': result.get('result')}
                self.stats['diplomatic_actions'] += 1
            except ProtocolError as error:
                self.stats['errors'] += 1
                if not error.status or error.status >= 500 or error.status == 429 or error.code in ('CONFLICT', 'CATCHING_UP'):
                    continue
                receipt = {'ok': False, 'id': command.get('id'), 'key': key, 'error': str(error)}
            self.sent[key] = receipt
            self.pending_commands.remove(entry)
            self.action_results = (self.action_results + [{'turn': obs['turn'], 'type': command['type'], **receipt}])[-24:]
            self.persist()
        if isinstance(self.agent, FileAgent):
            atomic_json(self.agent.directory / 'receipts.json', {'cursor': self.cursor, 'commands': list(self.sent.values())[-2000:]})

    def dispatch(self, commands, observation):
        self.queue(commands, ['manual', observation['turn']])
        self.persist()
        self.flush_commands(observation)

    def set_orders(self, orders, obs):
        if not orders:
            return
        if orders.get('turn') != obs['turn']:
            self.stats['stale_orders_discarded'] += 1
            return
        try:
            checked = validate_orders(orders, obs)
        except (ValueError, TypeError, KeyError):
            checked = self.fallback.decide(obs)
            self.status = 'fallback'
            self.stats['agent_fallbacks'] += 1
        if isinstance(self.agent, CodexStrategist) and not self.fast:
            checked['ready'] = False
        self.pending_orders = checked
        self.persist()

    def flush_orders(self, obs):
        checked = self.pending_orders
        if not checked:
            return
        if checked['turn'] != obs['turn']:
            self.stats['stale_orders_discarded'] += 1
            self.pending_orders = None
            return
        signature = digest(checked)
        if signature != self.last_file_digest:
            self.client.orders(checked, f'orders-{obs["turn"]}-{signature[:32]}')
            self.last_file_digest = signature
            self.submitted_turn = obs['turn']
            self.stats['orders_sent'] += 1
            atomic_json(self.root / 'orders.json', checked)
        self.pending_orders = None
        self.persist()

    def _invoke(self, obs, events, new_turn):
        # Only this thread invokes callbacks for this kingdom. No other kingdom's state is passed here.
        if new_turn:
            hook = getattr(self.agent, 'on_turn', None) or self.agent.decide
            orders = hook(obs)
            commands = self.agent.diplomacy(obs) if isinstance(self.agent, CodexStrategist) else []
            # New-style custom agents receive events too, even when bundled with a turn.
            if not isinstance(self.agent, CodexStrategist):
                hook = getattr(self.agent, 'on_events', None)
                commands += (hook(obs, events) if hook else self.agent.diplomacy(obs)) or []
            return orders, commands
        hook = getattr(self.agent, 'on_events', None)
        return None, (hook(obs, events) if hook else self.agent.diplomacy(obs)) or []

    def step(self):
        update = self.client.updates(self.cursor)
        obs = update['observation']
        self.last_observation = obs
        self.cursor = update['cursor']
        incoming = update.get('events', [])
        if update.get('reset'):
            incoming = incoming + [{'cursor': self.cursor, 'kind': 'reset', 'data': {'reason': 'Reconnect snapshot replaces expired events'}}]
        known = {e.get('cursor') for e in self.pending_events}
        self.pending_events += [e for e in incoming if e.get('cursor') not in known and self.meaningful(e, obs['you'])]
        self.pending_events = self.pending_events[-200:]
        self.stats['events_received'] += len(incoming)
        obs['recent_action_results'] = self.action_results[-12:]
        atomic_json(self.root / 'observation.json', obs)
        if isinstance(self.agent, FileAgent):
            self.agent.observe(obs, self.pending_events, self.cursor)
        if obs.get('readOnly') or obs['status'] != 'active' or next(p for p in obs['players'] if p['id'] == obs['you'])['eliminated']:
            self.persist()
            return obs
        if self.last_turn != obs['turn']:
            self.last_turn = obs['turn']
            if self.pending_orders and self.pending_orders['turn'] != obs['turn']:
                self.pending_orders = None
            log.info('%s: turn %d', obs['you'], obs['turn'])
        if self.future and self.future.done():
            meta = self.future_meta
            try:
                orders, commands = self.future.result()
                self.status = 'fallback' if isinstance(self.agent, CodexStrategist) and self.agent.last_source == 'fallback' else 'responding'
                self.response_until = time.monotonic() + 3
            except Exception as error:
                log.warning('%s: agent failed (%s); using tactical fallback.', obs['you'], type(error).__name__)
                self.status = 'fallback'
                self.stats['agent_fallbacks'] += 1
                orders, commands = self.fallback.decide(obs), []
            self.set_orders(orders, obs)
            for command in commands:
                if isinstance(command, dict) and command.get('type') == 'orders':
                    self.set_orders(command.get('data'), obs)
            self.queue(commands, meta['scope'])
            self.pending_events = [e for e in self.pending_events if e.get('cursor') not in meta['cursors']]
            self.future = None
            self.persist()
        if isinstance(self.agent, FileAgent):
            self.set_orders(self.agent.decide(obs), obs)
            self.queue(self.agent.diplomacy(obs), ['file'])
        self.flush_orders(obs)
        new_turn = self.planned_turn != obs['turn']
        if not isinstance(self.agent, FileAgent) and not self.future and (new_turn or self.pending_events) and time.monotonic() - self.last_model_at >= (3 if isinstance(self.agent, CodexStrategist) else .2):
            events = copy.deepcopy(self.pending_events)
            context = copy.deepcopy(obs)
            context['reasoning_events'] = events
            self.future_meta = {'turn': obs['turn'], 'cursors': {e.get('cursor') for e in events}, 'scope': ['turn', obs['turn']] if new_turn else ['events', [e.get('cursor') for e in events]]}
            self.planned_turn = obs['turn']
            self.stats['turns_planned'] += int(new_turn)
            self.last_model_at = time.monotonic()
            self.status = 'thinking'
            self.future = self.executor.submit(self._invoke, context, events, new_turn)
        # Network/deadline work never waits for the model. A late answer may still continue its conversation.
        if self.submitted_turn != obs['turn'] and self.client.seconds_left(obs) <= 10:
            self.stats['deadline_fallbacks'] += 1
            self.status = 'fallback'
            orders = self.fallback.decide(obs)
            orders['ready'] = False
            self.set_orders(orders, obs)
            self.flush_orders(obs)
        elif not self.future and self.status == 'responding' and time.monotonic() > self.response_until:
            self.status = 'connected'
        self.flush_commands(obs)
        self.persist()
        return obs

    def run(self, max_turns=None, max_seconds=None):
        start, seen_turns = time.monotonic(), set()
        while not self.stop.is_set() and (max_seconds is None or time.monotonic() - start < max_seconds):
            try:
                obs = self.step()
                if self.submitted_turn > 0:
                    seen_turns.add(self.submitted_turn)
                if obs['status'] == 'finished' or obs.get('readOnly') or next(p for p in obs['players'] if p['id'] == obs['you'])['eliminated']:
                    return obs
                if max_turns and len(seen_turns) >= max_turns:
                    return obs
            except ProtocolError as error:
                self.stats['errors'] += 1
                self.status = 'offline'
                if error.status in (401, 403, 404):
                    raise
                log.warning('%s: connection issue; retrying (%s).', self.client.connection.playerId, error.code)
            self.stop.wait(self.poll)
        return self.last_observation

    def close(self):
        self.stop.set()
        if hasattr(self.agent, 'cancel'):
            self.agent.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
