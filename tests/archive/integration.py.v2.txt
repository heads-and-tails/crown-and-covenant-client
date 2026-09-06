"""Real four-client matches. Run explicitly against a local or deployed game."""
import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covenant import Client, Connection, ReferenceAgent
from covenant.runner import Runner
from covenant.validation import validate_orders


def request(server, path, body):
    with urlopen(Request(server + '/api' + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}), timeout=20) as r:
        return json.load(r)


def make_match(server, seed=2026, seconds=180, kind='classic'):
    host = request(server, '/games', {'playerName': 'Python Ember', 'name': 'Python integration', 'seed': seed, 'turnSeconds': seconds, 'settings': {'kind': kind}})
    seats = [host] + [request(server, f'/games/{host["gameId"]}/join', {'playerName': name}) for name in ('Python Tide', 'Python Thorn', 'Python Gold')]
    clients = [Client(Connection(server, s['gameId'], s['playerId'], s['token'])) for s in seats]
    clients[0].command('start')
    return clients


def play(server, count=3, horizon=120):
    results = []
    for game in range(count):
        clients = make_match(server, 3000+game)
        agents = [ReferenceAgent() for _ in clients]
        commands = 0
        for step in range(horizon):
            states = [c.state() for c in clients]
            if states[0]['status'] == 'finished':
                break
            for client, agent, o in zip(clients, agents, states):
                if next(p for p in o['players'] if p['id'] == o['you'])['eliminated']:
                    continue
                for action in agent.diplomacy(o):
                    try:
                        client.command(action['type'], action.get('data'))
                        commands += 1
                    except Exception:
                        # Simultaneous offers can become unaffordable or superseded.
                        pass
            active = [(c, a, c.state()) for c, a in zip(clients, agents)]
            active = [(c, a, o) for c, a, o in active if not next(p for p in o['players'] if p['id'] == o['you'])['eliminated']]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(c.orders, validate_orders(a.decide(o), o)) for c, a, o in active]
                for future in futures:
                    future.result()
        final = clients[0].state()
        assert len(final['winners']) <= 1, 'Multiple winners'
        results.append({'gameId': final['id'], 'turns': final['turn'], 'winners': final['winners'], 'diplomaticActions': commands, 'finished': final['status'] == 'finished', 'horizon': horizon})
        print(json.dumps(results[-1]), flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default='http://127.0.0.1:3001')
    parser.add_argument('--games', type=int, default=3)
    parser.add_argument('--horizon', type=int, default=120)
    parser.add_argument('--report', default='/tmp/covenant-python-integration.json')
    args = parser.parse_args()
    results = play(args.server.rstrip('/'), args.games, args.horizon)
    Path(args.report).write_text(json.dumps(results, indent=2))
