from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from .transport import Client, Connection, ProtocolError
from .agent import ReferenceAgent
from .harness import CodexStrategist
from .runner import FileAgent, Runner
from .custom import IsolatedAgent
from .host import Host


def main(argv=None):
    parser = argparse.ArgumentParser(description='Crown & Covenant local agent runner')
    sub = parser.add_subparsers(dest='command', required=True)
    host = sub.add_parser('host', help='Pair this PC once and supply three private Luna opponents')
    host.add_argument('--server', default='https://crown-and-covenant-flame.vercel.app')
    host.add_argument('--state-dir', default='.covenant')
    host.add_argument('--label', default='My PC')
    host.add_argument('--fast', action='store_true', help='Resolve early when everyone is ready (testing)')
    host.add_argument('--max-seconds', type=float)
    host.add_argument('--model-timeout', type=float, default=70)
    run = sub.add_parser('run', help='Control a kingdom from this computer')
    run.add_argument('--connection', required=True, help='Private connection.json downloaded from the website')
    mode = run.add_mutually_exclusive_group()
    mode.add_argument('--agent', help='Import a custom agent as module:ClassName')
    mode.add_argument('--files', help='Directory for observation.json, orders.json and outbox.json')
    mode.add_argument('--codex', action='store_true', help='Use the local Codex login for a strategic model')
    run.add_argument('--model', default='gpt-5.6-luna')
    run.add_argument('--poll', type=float, default=3)
    run.add_argument('--fast', action='store_true', help='Allow model agents to resolve early when all ready')
    run.add_argument('--max-turns', type=int)
    run.add_argument('--max-seconds', type=float)
    run.add_argument('--state-dir', default='.covenant')
    run.add_argument('--timeout', type=float, default=15, help='Custom-agent timeout in seconds')
    state = sub.add_parser('state', help='Print your current observation as JSON')
    state.add_argument('--connection', required=True)
    join = sub.add_parser('join', help='Take a free seat without opening a browser')
    join.add_argument('--server', required=True)
    join.add_argument('--game', required=True)
    join.add_argument('--name', required=True)
    join.add_argument('--output', default='connection.json')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    agent = None
    runner = None
    try:
        if args.command == 'host':
            Host(args.server, args.state_dir, args.label, args.fast, model_timeout=args.model_timeout).run(args.max_seconds)
            return 0
        if args.command == 'join':
            # No model or account credentials are involved in taking a seat.
            url = args.server.rstrip('/') + '/api/games/' + args.game.upper() + '/join'
            req = Request(url, data=json.dumps({'playerName': args.name}).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=15) as response:
                result = json.load(response)
            connection = Connection(args.server, result['gameId'], result['playerId'], result['token'])
            connection.save(args.output)
            print(f'Joined {connection.gameId} as {connection.playerId}. Saved {args.output}.')
            return 0
        connection = Connection.load(args.connection)
        client = Client(connection)
        if args.command == 'state':
            print(json.dumps(client.state(), indent=2))
            return 0
        sys.path.insert(0, str(Path.cwd()))
        if args.files:
            agent = FileAgent(args.files)
        elif args.codex:
            memory = Path(args.state_dir) / connection.gameId / connection.playerId / 'strategy.json'
            agent = CodexStrategist(args.model, memory_path=memory)
        elif args.agent:
            agent = IsolatedAgent(args.agent, args.timeout)
        else:
            agent = ReferenceAgent()
        logging.info('Connected kingdom %s in lobby %s', connection.playerId, connection.gameId)
        runner = Runner(client, agent, args.poll, args.fast, args.state_dir)
        runner.run(args.max_turns, args.max_seconds)
        return 0
    except KeyboardInterrupt:
        logging.info('Stopped. Your kingdom remains available to reconnect.')
        return 0
    except (OSError, ValueError, ProtocolError) as error:
        logging.error('%s', error)
        return 1
    finally:
        if runner:
            runner.close()
        if isinstance(agent, IsolatedAgent):
            agent.close()


if __name__ == '__main__':
    raise SystemExit(main())
