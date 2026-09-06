"""Pair a browser to this computer and lease independent Luna-controlled seats."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import subprocess
import threading
import time
import uuid
from pathlib import Path
import json
from .transport import Connection, Client, ProtocolError, atomic_json
from .harness import CodexStrategist
from .runner import Runner

log = logging.getLogger('covenant')


class Host:
    def __init__(self, server, state_directory='.covenant', label='My PC', fast=False, poll=2, model_timeout=70):
        self.server, self.label, self.fast, self.poll = server.rstrip('/'), label, fast, poll
        origin = hashlib.sha256(self.server.encode()).hexdigest()[:16]
        self.root = Path(state_directory) / 'hosts' / origin
        self.root.mkdir(parents=True, exist_ok=True)
        self.instance = uuid.uuid4().hex
        self.model_timeout = model_timeout
        self.runners, self.futures = {}, {}
        self.executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix='kingdom')
        self.stop = threading.Event()
        self.credentials = None
        try:
            self.credentials = json.loads((self.root / 'host.json').read_text())
        except (OSError, ValueError):
            pass
        self.client = Client(Connection(self.server, '00000000', 'p1', self.credentials['token'] if self.credentials else 'unpaired-registration-key'), retries=2)

    def register(self):
        if not self.credentials:
            self.credentials = self.client._request('/agent-hosts', {'label': self.label, 'model': 'gpt-5.6-luna'})
            atomic_json(self.root / 'host.json', self.credentials)
            self.client.connection.token = self.credentials['token']
        if self.credentials.get('pairCode'):
            print(f"\nOpen {self.server} and choose Pair once.\nPairing code: {self.credentials['pairCode']}\nKeep this host running while you play.\n", flush=True)

    def step(self):
        statuses = [{'gameId': key[0], 'playerId': key[1], 'status': runner.status} for key, runner in self.runners.items()]
        work = self.client._request(f"/agent-hosts/{self.credentials['id']}/work", {'instance': self.instance, 'statuses': statuses})
        if work['paired'] and self.credentials.get('pairCode'):
            self.credentials.pop('pairCode', None)
            atomic_json(self.root / 'host.json', self.credentials)
            print('Browser paired. Create a Play against Luna lobby on that browser.', flush=True)
        if not work['paired'] and self.credentials.get('expiresAt', 0) < time.time() * 1000:
            code = self.client._request(f"/agent-hosts/{self.credentials['id']}/pairing", {})
            self.credentials.update(code)
            atomic_json(self.root / 'host.json', self.credentials)
            print(f"New pairing code: {code['pairCode']}", flush=True)
        jobs = {(j['gameId'], j['playerId']): j for j in work['jobs']}
        for key in list(self.runners):
            if key not in jobs:
                self.runners.pop(key).close()
                self.futures.pop(key, None)
        for key, job in jobs.items():
            # Lobby seats are connected by the host heartbeat. Start their network/model
            # loops only when play begins, avoiding three extra idle polling streams.
            if job['status'] != 'active':
                continue
            if key in self.runners:
                if self.futures[key].done():
                    # A finished/eliminated seat is retired by the next lease response. Network failures reconnect.
                    try:
                        result = self.futures[key].result()
                        if result and (result['status'] == 'finished' or next(p for p in result['players'] if p['id'] == result['you'])['eliminated']):
                            continue
                    except Exception:
                        log.warning('%s/%s: reconnecting local seat.', *key)
                    self.futures[key] = self.executor.submit(self.runners[key].run)
                continue
            if len(self.runners) >= 12:
                continue
            connection = Connection(self.server, job['gameId'], job['playerId'], job['token'])
            agent = CodexStrategist(timeout=self.model_timeout, memory_path=self.root / job['gameId'] / job['playerId'] / 'strategy.json')
            runner = Runner(Client(connection), agent, poll=self.poll, fast=self.fast, state_directory=self.root)
            self.runners[key] = runner
            self.futures[key] = self.executor.submit(runner.run)
            log.info('%s/%s: independent Luna opponent connected.', *key)
        return work

    def run(self, max_seconds=None):
        # Read only the login status; never read, forward, or print account credentials.
        status = subprocess.run(['codex', 'login', 'status'], capture_output=True, timeout=15, check=False)
        if status.returncode:
            raise ValueError('Sign in first with codex login, then run covenant host again.')
        self.register()
        start = time.monotonic()
        try:
            while not self.stop.is_set() and (max_seconds is None or time.monotonic() - start < max_seconds):
                delay = 3
                try:
                    work = self.step()
                    if not any(job['status'] == 'active' for job in work['jobs']):
                        delay = 10
                except ProtocolError as error:
                    if error.status in (401, 403, 404):
                        raise
                    log.warning('Host reconnecting (%s).', error.code)
                self.stop.wait(delay)
        finally:
            for runner in self.runners.values():
                runner.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
