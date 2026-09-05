"""Explicit, bounded real Luna trials using the user's existing local login."""
import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration import make_match
from covenant.harness import CodexStrategist
from covenant.validation import validate_orders


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default='http://127.0.0.1:3001')
    parser.add_argument('--turns', type=int, default=8)
    parser.add_argument('--output', default='/tmp/covenant-luna-trial')
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    clients = make_match(args.server, 72643)
    agents = [CodexStrategist(timeout=50, memory_path=root / f'p{i+1}.json') for i in range(4)]
    for i, client in enumerate(clients):
        client.connection.save(root / f'connection-p{i+1}.json')
    report = {'gameId': clients[0].connection.gameId, 'turns': [], 'errors': [], 'model': 'gpt-5.6-luna'}
    for index in range(args.turns):
        observations = [c.state() for c in clients]
        if observations[0]['status'] == 'finished':
            break
        active = [i for i, o in enumerate(observations) if not next(p for p in o['players'] if p['id'] == o['you'])['eliminated']]
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {i: pool.submit(agents[i].decide, observations[i]) for i in active}
            orders = {i: f.result() for i, f in futures.items()}
        sent = []
        for i in active:
            for action in agents[i].diplomacy(observations[i]):
                try:
                    clients[i].command(action['type'], action.get('data'))
                    sent.append({'player': f'p{i+1}', **action})
                except Exception as error:
                    report['errors'].append({'turn': observations[i]['turn'], 'player': f'p{i+1}', 'error': str(error)})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            calls = [pool.submit(clients[i].orders, validate_orders(orders[i], observations[i])) for i in active]
            for call in calls:
                call.result()
        turn = {'turn': observations[0]['turn'], 'seconds': round(time.monotonic()-started, 2), 'actions': sent, 'strategies': [a.intent for a in agents], 'metrics': [dict(a.metrics) for a in agents]}
        report['turns'].append(turn)
        Path(root/'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'turn': turn['turn'], 'seconds': turn['seconds'], 'diplomaticActions': len(sent), 'successes': sum(a.metrics['model_successes'] for a in agents), 'fallbacks': sum(a.metrics['fallbacks'] for a in agents)}), flush=True)
    final = clients[0].state()
    report['finalStatus'], report['finalTurn'] = final['status'], final['turn']
    report['alliances'] = final['alliances']
    report['metrics'] = [a.metrics for a in agents]
    Path(root/'report.json').write_text(json.dumps(report, indent=2))
    assert sum(a.metrics['model_successes'] for a in agents) >= 4, 'No successful four-agent model round'


if __name__ == '__main__':
    main()
