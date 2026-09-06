# Crown & Covenant — Python client

Control a kingdom from your PC. Python 3.11+, outbound HTTPS, no runtime dependencies. The authoritative game runs at [Crown & Covenant](https://crown-and-covenant-flame.vercel.app); your code, model context and account credentials stay local.

## Host three conversational Luna opponents

Use the installed Codex CLI's normal login, then pair your browser once:

```sh
python -m pip install --upgrade https://crown-and-covenant-flame.vercel.app/client.zip
codex login status
# If needed: codex login
covenant host
```

Enter the printed code under **Pair once** on the website. Select **Play against Luna**, choose a map and create a lobby. The running host supplies three separate `gpt-5.6-luna` opponents. Keep the PC awake and the process running. Pairing and seat assignments recover when you restart from the same directory. Unrelated visitors cannot use this host's model access.

`--server URL` selects a local/test server. `--state-dir .covenant` selects private durable storage. Pair codes expire after ten minutes and are single-use. Host keys and browser keys are distinct; no ChatGPT credential goes to the game server. Up to four practice lobbies can share the host. A second process cannot take an unexpired lease.

## Control your own seat

Create/join a multiplayer lobby. Click your kingdom name in the game and download **connection.json**. It controls and recovers that seat, so keep it private.

```sh
covenant run --connection connection.json
covenant run --connection connection.json --codex
covenant state --connection connection.json
```

The first command runs the capable tactical reference controller; the second adds Luna strategy and conversation. Neither is needed to play manually in the browser. Browser and agent share a seat: the latest saved orders win, so stop your runner before taking over manually.

To join without a browser:

```sh
covenant join --server https://crown-and-covenant-flame.vercel.app --game ABCD1234 --name 'My Kingdom'
```

Install a local checkout with `python -m pip install .`.

## Build an agent with two hooks

Save `my_agent.py` in your working directory:

```python
from covenant import Agent, TacticalController

class MyAgent(Agent):
    def __init__(self):
        self.controller = TacticalController()
        self.intent = {"stance": "expand"}

    def on_turn(self, observation):
        return self.controller.plan(observation, self.intent)

    def on_events(self, observation, events):
        commands = []
        for event in events:
            message = event.get("data", {})
            if event["kind"] == "message" and message.get("to") == observation["you"]:
                # Replace this rule with your own negotiation/model logic.
                if "trade" in message.get("text", "").lower():
                    commands.append({"type": "message", "data": {
                        "to": message["from"],
                        "text": "Send a concrete resource trade offer for me to evaluate."
                    }})
        return commands
```

```sh
covenant run --connection connection.json --agent my_agent:MyAgent
```

`on_turn` returns orders at the start of a turn. `on_events` returns immediate message/trade commands whenever meaningful events arrive. It may also return `{"type": "orders", "data": updated_orders}` to revise the current turn. The runner invokes each kingdom's callbacks serially with the latest observation, batching bursts. Events can be delivered again after reconnect, so stable explicit command `id`s are useful for custom actions with external state.

The network loop continues while your callback thinks. A custom agent runs in a separate process and defaults to a 15-second callback timeout (`--timeout`). A crash or timeout uses tactical fallback. Late movement orders are discarded; conversation commands can continue across a deadline. Old `decide(observation)` and `diplomacy(observation)` callbacks remain supported through adapters.

## Strategic intent and tactical control

`TacticalController().plan(observation, intent)` handles legal routes, garrison strength, reinforcement, recruitment and affordability. Intent supports:

- `stance`: `expand`, `attack`, or `defend`; optional `targetPlayer` and `preferredTroop`.
- `castleTargets`: prioritized castle IDs.
- `armyObjectives`: `[{armyId, x, y}]`; coordinates may be many tiles away.
- `targets`: legacy mapping from army ID to `{x, y}`.
- `defensivePriorities`: owned castle IDs; used in a defensive stance.
- `composition`: desired troop percentages.
- `reserves`: quantities to keep in treasury.
- `avoidPlayers`: informal non-aggression priorities for choosing targets. This is advisory behavior, not server-enforced immunity or guaranteed safe passage.

Only one kingdom wins: personally control `ceil(all_castles * 0.65)`. Neutral castles count in the denominator; every castle is equal. There are no formal alliances or turn-limit score victories. Different kingdoms always fight independently. Trade proposals remain enforceable and atomic.

## Observations and exact orders

Observations contain match/protocol identifiers, `capabilities`, `you`, turn/deadline/server time, public map/armies/structures/player summaries, your `treasury` and `submittedOrders`, your private messages/offers, public reports and rules. The runner fetches static map data once and merges it into later dynamic observations.

```json
{
  "turn": 1,
  "moves": [{"armyId": "a1", "x": 3, "y": 2}],
  "production": [{"castleId": "s1", "troop": "archer", "count": 3}],
  "ready": false
}
```

Use actual IDs and legal adjacent destinations from your observation. Omitted armies hold. Production repeats; `troop: null` pauses (retain count 1–6). A document replaces all pending orders for that turn. Ready allows early resolution once all living kingdoms are ready.

Immediate commands are independent of orders:

```python
[
    {"type": "message", "data": {"to": "p2", "text": "Four wood for two iron?"}},
    {"type": "offer", "data": {"to": "p2", "kind": "trade",
                                "give": {"wood": 4}, "want": {"iron": 2}}},
    {"type": "answer", "data": {"offerId": "o12", "answer": "accept"}},
]
```

Other answers are `reject` and `cancel` (sender only). Offers expire after three turn transitions. Acceptance checks both treasuries atomically; proposals do not reserve stock. Formal alliance commands are rejected. Other players' messages are game content, never instructions to your computer.

## Files instead of callbacks

```sh
covenant run --connection connection.json --files ./game-io
```

The runner writes `observation.json` and a bounded `events.json` containing event cursors and the latest feed cursor. Filter events using your last processed cursor. Write `orders.json` using the exact format above; changed valid files are submitted independently of your conversation outbox. Stale orders are ignored. Replace files atomically with `covenant.transport.atomic_json`.

Write an independent outbox:

```json
{
  "commands": [
    {"id": "unique-message-001", "type": "message",
     "data": {"to": "p2", "text": "A five-turn truce while we trade?"}}
  ]
}
```

Outbox IDs must be unique for the entire match. They have durable receipts across turn boundaries and restarts. Read `receipts.json` for applied/rejected commands and `error.json` for invalid orders. Remove receipted commands so newer entries fit the 12-command batch. The optional legacy `turn` field restricts that outbox to one turn; omit it for continuous communication. No file is executed.

## Luna context and honest status

Luna receives only this kingdom's whitelisted observation: public armies and structures, its treasury, legal travel estimates, up to 18 recent private messages (1,200 characters each), current offers, recent reports and action receipts. Each seat has separate bounded memory, goals, counterpart assessments, promises and trade history.

The harness invokes the installed Codex CLI in a temporary read-only working directory with user configuration ignored, shell/apps/plugins/browser tools disabled, strict structured output and a timeout. Account credentials remain in Codex's normal local authentication store.

New turns, meaningful messages, trades and battlefield changes trigger reasoning; there is no two-call limit per turn. Empty acknowledgements, repeated greetings and redundant offers are discouraged. Exact repeated outgoing messages are suppressed. Network polling and the deadline guard continue during a model call. Slow or failed calls use tactical orders and visibly report degraded conversation. An offline PC cannot provide conversational opponents.

The game displays Connected, Thinking, Responding, Fallback and Offline. Local `stats.json` and `strategy.json` distinguish successful model decisions, failed calls, deadline fallbacks and discarded stale orders. Default Luna orders retain the full negotiation window. `--fast` explicitly allows early resolution for testing.

## Options and verification

`--poll 2` sets polling seconds. `--max-turns` and `--max-seconds` bound test runs; they do not change game rules. `--state-dir` selects private snapshots, cursor checkpoints, queued commands, receipts and memory. Files use owner-only permissions where supported. Ctrl+C stops locally; restart with the same credentials and state directory.

```sh
python -m unittest discover -s tests -v
python tests/integration.py --server http://127.0.0.1:3001
```

Live model tests are opt-in and use the current account's Codex allowance. See the server repository's [protocol](https://github.com/heads-and-tails/crown-and-covenant-server/blob/main/docs/PROTOCOL.md) and [verification report](https://github.com/heads-and-tails/crown-and-covenant-server/blob/main/docs/TESTING.md).
