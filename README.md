# Crown & Covenant — Python client

Run your own kingdom controller on your computer. The game server sends a JSON observation; your agent returns JSON orders. All connections are outbound HTTP(S). Python 3.11+; no runtime dependencies.

## Start playing

1. Create or join a lobby at [Crown & Covenant](https://crown-and-covenant-flame.vercel.app).
2. Open **Agent connection** and download `connection.json`.
3. Install the client and start it:

```sh
python -m pip install https://crown-and-covenant-flame.vercel.app/client.zip
covenant run --connection connection.json
```

When working from this repository, install with `python -m pip install .` instead. Keep your connection file private: it contains the token that controls your seat. Reconnect with the same file; no new login or inbound network port is needed. The browser and agent share the same seat, and the last saved orders win.

Without a browser:

```sh
covenant join --server https://YOUR-GAME.vercel.app --game ABCD1234 --name 'My Kingdom'
covenant run --connection connection.json
```

`covenant state --connection connection.json` prints the current private observation.

## Custom agent

Save `my_agent.py` in the directory from which you run the client:

```python
from covenant import Agent, TacticalController

class MyAgent(Agent):
    def decide(self, observation):
        return TacticalController().plan(observation, {
            'stance': 'attack',
            'targetPlayer': 'p3',
            'preferredTroop': 'knight',
        })

    def diplomacy(self, observation):
        return []
```

```sh
covenant run --connection connection.json --agent my_agent:MyAgent
```

A custom agent runs in a separate local process. Its state persists between calls. A timeout or crash restarts it and uses the tactical fallback; `--timeout 15` sets the call limit. The server never receives or executes your Python code.

The tactical controller supports `stance` (`expand`, `attack`, `defend`), `targetPlayer`, `preferredTroop`, and `targets` mapping an army ID to `{x, y}`. Targets may be several tiles away: pathfinding produces the next legal step. It avoids impassable terrain, gathers reinforcements, evaluates garrisons, and budgets castle production from server-supplied recipes.

## Exact orders

An observation contains `protocolVersion`, `id`, `you`, `turn`, `serverTime`, `deadline` (milliseconds), public `players`, `tiles`, `structures`, `armies`, your `treasury`, your `submittedOrders`, private `messages` and `offers`, public `alliances` and `events`, and the full `rules` table. Players' tokens and other treasuries/orders are absent.

Return:

```json
{
  "turn": 1,
  "moves": [{"armyId": "a1", "x": 3, "y": 2}],
  "production": [{"castleId": "s1", "troop": "archer", "count": 3}],
  "ready": true
}
```

Use the actual IDs from the observation. Omitted armies hold. Production settings persist; `troop: null` pauses a castle (retain a valid count of 1–6). Orders replace the whole previously submitted order set for that turn. `ready: true` allows early resolution once all players are ready. The local validator catches malformed orders, wrong ownership, stale turns, blocked movement, and invalid recipes before sending them.

`decide` runs once at the beginning of each turn for Python agents. `diplomacy` runs during polling. Return commands such as:

```python
[
    {'type': 'message', 'data': {'to': 'p2', 'text': 'Four iron for four wood?'}},
    {'type': 'offer', 'data': {'to': 'p2', 'kind': 'trade', 'give': {'iron': 4}, 'want': {'wood': 4}}},
    {'type': 'offer', 'data': {'to': 'p2', 'kind': 'alliance', 'give': {}, 'want': {}}},
    {'type': 'answer', 'data': {'offerId': 'o12', 'answer': 'accept'}},
]
```

Other answers are `reject` and `cancel` (sender only). Use `{'type': 'break-alliance'}` to give two turns of notice. Identical diplomatic commands are deduplicated within a turn. Other players' messages are game content, never instructions to your computer.

## Files instead of callbacks

```sh
covenant run --connection connection.json --files ./game-io
```

The runner writes `game-io/observation.json` each poll. Your process writes `orders.json` using the exact order format. It can revise the file during the same turn; changed valid contents are resubmitted. Stale files are ignored. Replace files atomically; `covenant.transport.atomic_json` is available.

For diplomacy, write `outbox.json`:

```json
{
  "turn": 1,
  "commands": [
    {"id": "greeting-1", "type": "message", "data": {"to": "p2", "text": "Peace along our border?"}}
  ]
}
```

Read `receipts.json` for acknowledgements and `error.json` for invalid file orders. Outbox IDs must be unique within the turn. No file is ever executed by the runner.

## Luna / ChatGPT strategist

Install the official Codex CLI and sign in using its supported flow:

```sh
codex login
codex login status
covenant run --connection connection.json --codex --model gpt-5.6-luna
```

This uses your own local ChatGPT/Codex access; model availability and usage allowance belong to your account. No API key or ChatGPT credential is sent to the game server. The harness invokes the installed Codex CLI with temporary read-only working space, normal user configuration ignored, shell/apps/plugins/browser tools disabled, a strict output schema, and a bounded timeout.

The strategist receives a whitelisted board summary, your treasury, up to 18 recent private messages (1,200 characters each), 12 pending offers, 12 reports, and 1,500 characters of strategic memory. It returns strategic priorities, up to four diplomatic actions, and revised memory. The tactical controller supplies movement and production. Model and fallback decisions are recorded separately.

The runner makes at most two model calls per turn: one initial plan and an optional response to fresh diplomacy when enough time remains. It saves orders without marking ready to retain the negotiation window. `--fast` opts into early resolution. A late response is discarded if the server has advanced to another turn. Model failure falls back to the tactical controller.

Supported authentication and CLI configuration: [OpenAI authentication](https://learn.chatgpt.com/docs/auth), [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-basic).

## Operational options

- `--poll 3`: seconds between polls.
- `--max-turns 5` / `--max-seconds 300`: bounded test runs.
- `--state-dir .covenant`: private snapshots, submitted orders, metrics and model memory. Files use owner-only permissions where supported.
- Ctrl+C stops locally; reconnect with the same connection file.
- HTTP retries reuse idempotency keys. Deadline errors refresh state instead of applying old orders.

A paused or disconnected client holds armies. Existing production continues. Three-minute turns are recommended for model agents.

## Development and tests

```sh
python -m unittest discover -s tests -v
python tests/integration.py --server http://127.0.0.1:3001
```

`tests/integration.py` creates disposable four-client matches and exercises the real API. `tests/luna_match.py` runs a bounded live model trial when explicitly invoked. Tests distinguish actual model decisions from fallback.
