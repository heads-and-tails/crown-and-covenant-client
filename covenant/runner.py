from __future__ import annotations
import hashlib
import json
import logging
import time
from pathlib import Path
from .agent import Agent, ReferenceAgent
from .harness import CodexStrategist
from .transport import Client, ProtocolError, atomic_json
from .validation import validate_orders

log = logging.getLogger('covenant')
DIPLOMACY = {'message', 'offer', 'answer', 'break-alliance'}


class FileAgent(Agent):
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def observe(self, observation):
        atomic_json(self.directory / 'observation.json', observation)

    def decide(self, observation):
        try:
            orders = json.loads((self.directory / 'orders.json').read_text())
            if orders.get('turn') == observation['turn']:
                return validate_orders(orders, observation)
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as error:
            atomic_json(self.directory / 'error.json', {'turn': observation['turn'], 'error': str(error)})
        return None

    def diplomacy(self, observation):
        try:
            outbox = json.loads((self.directory / 'outbox.json').read_text())
            if outbox.get('turn') != observation['turn']:
                return []
            result = []
            for command in outbox.get('commands', [])[:12]:
                if command.get('type') in DIPLOMACY and isinstance(command.get('id'), str):
                    result.append(command)
            return result
        except (FileNotFoundError, ValueError, AttributeError):
            return []


class Runner:
    def __init__(self, client: Client, agent: Agent | None = None, poll: float = 3, fast: bool = False, state_directory: str | Path = '.covenant'):
        self.client, self.agent = client, agent or ReferenceAgent()
        self.poll, self.fast = max(.25, poll), fast
        self.root = Path(state_directory) / client.connection.gameId / client.connection.playerId
        self.root.mkdir(parents=True, exist_ok=True)
        self.last_turn = -1
        self.last_file_digest = ''
        self.sent: dict[str, object] = {}
        self.model_calls_this_turn = 0
        self.last_model_at = 0.
        self.last_diplomatic_signature = ''
        self.pending_orders = None
        self.action_results = []
        self.stats = {'turns_planned': 0, 'orders_sent': 0, 'diplomatic_actions': 0, 'errors': 0, 'fallbacks': 0}
        self.fallback = ReferenceAgent()

    def dispatch(self, commands: list[dict], observation: dict):
        for command in commands[:12]:
            if not isinstance(command, dict) or command.get('type') not in DIPLOMACY:
                continue
            canonical = json.dumps(command, sort_keys=True, separators=(',', ':'))
            identity = str(command.get('id', canonical))
            key = hashlib.sha256(f'{observation["turn"]}:{identity}'.encode()).hexdigest()
            if key in self.sent:
                continue
            try:
                result = self.client.command(command['type'], command.get('data'), key)
                self.sent[key] = {'ok': True, 'id': command.get('id'), 'result': result.get('result')}
                self.action_results.append({'turn': observation['turn'], 'type': command['type'], 'ok': True, 'result': result.get('result')})
                self.stats['diplomatic_actions'] += 1
                log.info('Diplomacy: %s', command['type'])
            except ProtocolError as error:
                if error.status and error.status < 500:
                    self.sent[key] = {'ok': False, 'id': command.get('id'), 'error': str(error)}
                self.stats['errors'] += 1
                self.action_results.append({'turn': observation['turn'], 'type': command['type'], 'ok': False, 'error': str(error)})
                log.info('Diplomacy not applied: %s', error)
        if isinstance(self.agent, FileAgent):
            atomic_json(self.agent.directory / 'receipts.json', {'turn': observation['turn'], 'commands': list(self.sent.values())})

    def step(self) -> dict:
        obs = self.client.state()
        self.action_results = self.action_results[-12:]
        obs['recent_action_results'] = self.action_results
        atomic_json(self.root / 'observation.json', obs)
        if isinstance(self.agent, FileAgent):
            self.agent.observe(obs)
        if obs['status'] != 'active' or next(p for p in obs['players'] if p['id'] == obs['you'])['eliminated']:
            return obs
        new_turn = obs['turn'] != self.last_turn
        if new_turn:
            self.last_turn, self.sent = obs['turn'], {}
            self.model_calls_this_turn, self.last_file_digest = 0, ''
            self.pending_orders = None
            log.info('Turn %d · %.0f seconds remaining', obs['turn'], self.client.seconds_left(obs))
        signature = json.dumps({'inbox': [m['id'] for m in obs['messages'] if m['to'] == obs['you']][-12:], 'offers': [(f['id'], f['status']) for f in obs['offers'][-12:]]})
        should_think = isinstance(self.agent, CodexStrategist) and not new_turn and signature != self.last_diplomatic_signature and self.model_calls_this_turn < 2 and time.monotonic() - self.last_model_at > 20 and self.client.seconds_left(obs) > self.agent.timeout + 8
        orders = None
        if new_turn or isinstance(self.agent, FileAgent) or should_think:
            try:
                if isinstance(self.agent, CodexStrategist):
                    # Bound a model call by the actual deadline, retaining time to submit.
                    self.agent.timeout = min(50, max(3, self.client.seconds_left(obs) - 8))
                    self.model_calls_this_turn += 1
                    self.last_model_at = time.monotonic()
                orders = self.agent.decide(obs)
                self.stats['turns_planned'] += int(new_turn)
            except Exception as error:
                log.warning('Agent decision failed (%s); using tactical fallback.', type(error).__name__)
                self.stats['fallbacks'] += 1
                orders = self.fallback.decide(obs)
            if isinstance(self.agent, CodexStrategist):
                fresh = self.client.state()
                if fresh['turn'] != obs['turn'] or fresh['status'] != 'active':
                    # Do not apply decisions or diplomatic promises from a superseded turn.
                    self.agent.pending_diplomacy = []
                    return fresh
                obs = fresh
                self.last_diplomatic_signature = signature
        try:
            self.dispatch(self.agent.diplomacy(obs), obs)
        except Exception as error:
            log.warning('Diplomacy hook failed (%s).', type(error).__name__)
            self.stats['errors'] += 1
        if orders:
            try:
                checked = validate_orders(orders, obs)
            except (ValueError, TypeError, KeyError) as error:
                log.warning('Invalid agent orders: %s. Using tactical fallback.', error)
                self.stats['fallbacks'] += 1
                checked = self.fallback.decide(obs)
            # Models retain the negotiation window. --fast explicitly allows early resolution.
            if isinstance(self.agent, CodexStrategist) and not self.fast:
                checked['ready'] = False
            self.pending_orders = checked
        if self.pending_orders and self.pending_orders['turn'] == obs['turn']:
            checked = self.pending_orders
            digest = hashlib.sha256(json.dumps(checked, sort_keys=True).encode()).hexdigest()
            if not isinstance(self.agent, FileAgent) or digest != self.last_file_digest:
                result = self.client.orders(checked, f'orders-{obs["turn"]}-{digest[:32]}')
                self.pending_orders = None
                self.last_file_digest = digest
                self.stats['orders_sent'] += 1
                atomic_json(self.root / 'orders.json', checked)
                log.info('Orders saved: %d moves, %d production settings', len(checked['moves']), len(checked['production']))
                obs = result['observation']
        atomic_json(self.root / 'stats.json', {**self.stats, **(self.agent.metrics if isinstance(self.agent, CodexStrategist) else {})})
        return obs

    def run(self, max_turns: int | None = None, max_seconds: float | None = None):
        start, seen_turns = time.monotonic(), set()
        while max_seconds is None or time.monotonic() - start < max_seconds:
            try:
                obs = self.step()
                if self.last_turn > 0:
                    seen_turns.add(self.last_turn)
                if obs['status'] == 'finished':
                    log.info('Match finished. Winners: %s. %s', ', '.join(obs['winners']), obs['victoryReason'])
                    return obs
                if max_turns and len(seen_turns) >= max_turns:
                    return obs
            except ProtocolError as error:
                self.stats['errors'] += 1
                if error.status in (401, 403, 404):
                    raise
                log.warning('Connection/order issue: %s. Retrying.', error)
                if error.code == 'STALE_TURN':
                    self.last_turn = -1
            time.sleep(self.poll)
        return None
