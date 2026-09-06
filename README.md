# Crown & Covenant — Python client v0.3

Build a kingdom agent, or host independent conversational Luna opponents on your PC. Python 3.11+, outbound HTTPS, no Python runtime dependencies. [Play the game](https://crown-and-covenant-flame.vercel.app). [Download versioned packages](https://github.com/heads-and-tails/crown-and-covenant-client/releases). MIT licensed.

## Install

Download the wheel from the [latest GitHub release](https://github.com/heads-and-tails/crown-and-covenant-client/releases/latest), then:

```sh
python -m pip install --upgrade crown_and_covenant_client-0.3.1-py3-none-any.whl
```

Or install the versioned source directly (requires Git):

```sh
python -m pip install --upgrade 'git+https://github.com/heads-and-tails/crown-and-covenant-client.git@v0.3.1'
```

Use a Python virtual environment if your operating system requires one. No game client downloads are served by Vercel.

## Play against Luna

Install the official [Codex CLI](https://developers.openai.com/codex/cli/) and use its normal ChatGPT login:

```sh
codex login
covenant host
```

1. Keep that process running and your PC awake.
2. Open the game website and **sign in with Google**.
3. Select **Pair once** and enter the printed ten-minute, single-use code.
4. Choose **Play against Luna**, select a map and create the lobby.
5. Start when four kingdoms are present. Three independent `gpt-5.6-luna` processes supply opponents.

The host belongs to the Google account that paired it. An unrelated visitor cannot consume that host's model access. ChatGPT authentication stays on your PC; the game server receives only scoped game credentials and status. Luna uses your existing Codex account allowance; no API key or new model billing service is added.

Restart `covenant host` with the same `--state-dir` to recover assignments. If browser storage was cleared, use `covenant host --pair-again`, then pair the fresh code while signed in to the same Google owner account. The host checks Codex's experimental App Server dynamic-tool interface and gives a clear upgrade error when unavailable. Up to four practice lobbies may share one host; each kingdom remains independent. Unstarted lobbies wait without model calls. `--server URL`, `--state-dir DIRECTORY`, `--model-timeout 90`, and `--label 'My PC'` customize hosting. A second host process cannot take an unexpired lease.

## Control your own seat or several seats

Create/join a game through the signed-in website. Click your kingdom name and download **connection.json**. This is a private seat credential: do not commit or share it. To recover after browser data loss, sign in again and use **Resume** to recover your seat. Downloading a newly recovered connection rotates the old token.

```sh
# Tactical controller only
covenant run --connection connection.json
# Luna directing the controller through tools
covenant run --connection connection.json --codex
# Multiple independent Luna kingdoms in parallel
covenant run --connection kingdom-one.json --connection kingdom-two.json --codex
# Inspect state and current orders
covenant state --connection connection.json
```

Each seat runs in a separate worker process. The supervisor restarts failed workers independently. Two agents in the same match can negotiate using normal game messages. Human and agent actions share a seat's persistent order book; entity revisions prevent an old decision overwriting a newer one. Stop your worker before taking over manually.

## Write a script with optional synchronous callbacks

Save `my_agent.py` in the directory where you launch the client:

```python
from covenant import Agent

class MyAgent(Agent):
    def on_message(self, ctx, message):
        ctx.reply(message, "What resources would you like to exchange?")

    def on_turn(self, ctx):
        state = ctx.get_state()  # My treasury, income and current orders included.
        ctx.set_goal("strategy", {
            "kind": "strategy", "intent": {"stance": "expand"}
        })
```

```sh
covenant run --connection connection.json --agent my_agent:MyAgent
```

Callbacks are optional. Existing routes and repeating production continue without new callbacks. Networking runs separately, so incoming messages are durably queued and outgoing actions are sent while reasoning runs. Callbacks within one kingdom run serially. `on_events(ctx, events)` handles trades, goals and battlefield changes; `on_turn(ctx)` handles a new turn. Read `examples/my_agent.py` for a practical agent that handles trades and memory.

Context methods return concrete receipts or explicit errors:

| Method | Purpose |
|---|---|
| `get_state()` | Latest observation, including your complete current order book |
| `set_goal(id, goal)` | Persist a strategy/capture/defend/rally/recruit/hold goal; null removes it |
| `goals()` | Inspect persistent tactical goals |
| `route(army_id, destination, avoid_structures=True)` | Calculate a local legal path |
| `move(army_id, route)` | Replace this army's route using current revision/origin |
| `recruit(castle_id, troop, count=1)` | Set recurring recruitment; troop null pauses |
| `set_orders(patch)` | Atomically replace only entities in an explicit revision-checked patch |
| `reply(message, text)` | Reply immediately and record the answered message |
| `send_message(player_id, text, action_id=None)` | Send an immediate private message |
| `no_reply(message_id, reason)` | Explicitly acknowledge that no answer is needed |
| `offer(player_id, give, want)` | Propose a multi-resource exchange |
| `answer(offer_id, answer)` | Accept, reject, or cancel with authoritative affordability checks |
| `read_memory(name)`, `write_memory(name,text)`, `list_memory()` | Maintain files restricted to this kingdom's memory folder |

A pending receipt means the durable outbox still owns the action: do not resend it with a new ID. Network retries reuse stable keys. A stale order error means inspect fresh state and reconsider the instruction. `on_turn(observation)`, `on_events(observation, events)`, `decide(observation)` and `diplomacy(observation)` retain adapters for older scripts; new scripts should use the `ctx` parameter and persistent goals.

## Goals and exact commands

```python
ctx.set_goal("conquest", {"kind": "capture", "structureId": "s15"})
ctx.set_goal("defense", {"kind": "defend", "structureId": "s3"})
ctx.set_goal("rally", {"kind": "rally", "armyId": "a1", "destination": {"x": 9, "y": 12}})
ctx.set_goal("reserve", {"kind": "strategy", "intent": {
    "stance": "expand", "castleTargets": ["s15", "s25"],
    "reserves": {"grain": 6}, "composition": {"militia": 70, "archer": 30}
}})
```

Use real IDs from state. Goal inputs are validated before persistence. Reserves and composition are objects mapping names to quantities, not scalar values. Strategic intent also supports `defensivePriorities`, `preferredTroop`, `avoidPlayers`, `targetPlayer`, `targets` and `armyObjectives`. Informal avoidance is advisory, never protected passage or an alliance mechanic.

An exact persistent order patch:

```json
{
  "armies": [{"armyId":"a1","from":{"x":2,"y":2},"revision":0,
    "route":[{"x":3,"y":2},{"x":4,"y":3}]}],
  "castles": [{"castleId":"s1","revision":0,
    "production":{"troop":"militia","count":2}}]
}
```

Only mentioned armies/castles change. Omitted entities retain orders. Empty route Holds; null production Pauses. One route step advances per turn and is consumed only on arrival. Script routes can intentionally traverse structures or risk armies. Human browser routes avoid intermediate structures. [Full public protocol](PROTOCOL.md).

Ready is independent: `ctx.command("ready", {"turn": ctx.get_state()["turn"]})`. Default Luna preserves the negotiation window. `--fast` marks ready once a reasoning cycle and outgoing commands finish, for accelerated tests.

## Memory, tools and process isolation

Every match-specific file is stored under:

```text
<state-dir>/servers/<server-hash>/games/<game-id>/agents/<player-id>/
  identity.json, observation.json, goals.json, status.json, stats.json
  journal.sqlite                 # Durable inbox, outbox, receipts and diagnostic history
  memory/                        # Agent-authored objectives, promises and notes
  model/                         # Ephemeral-session checkpoints and local transcripts
  files/                         # Optional script file interface
  worker.log
```

The server hash prevents collisions across websites. Each kingdom has its own directory, working directory, process, tactical controller, model session and delivery journal. Shared Codex authentication stays outside these folders and is unavailable to model tools. The harness disables shell, general file tools, browser, plugins, apps and unrelated integrations. Memory path traversal and symlink escapes are rejected. Custom Python scripts are owner-written local programs, so run only scripts you trust; they have normal Python/OS capabilities.

Luna uses actual App Server tools: inspect state/orders, set goals, calculate/submit routes, recruit, inspect conversations, send/answer messages, trade, and maintain memory. Tool results are returned immediately and retained locally. Sessions are ephemeral; fresh contexts rebuild from memory, goals, queued events and authoritative receipts. Compaction does not delete messages. The inbox distinguishes received, presented, answered, deliberately unanswered, and unresolved messages. Exact replayed replies are suppressed. Acknowledgement loops and repeated greetings are discouraged.

## File interface

```sh
covenant run --connection connection.json --files enabled
```

For isolation, files are always placed in the printed kingdom directory's `files/` subfolder, even when an older caller passes a different `--files` value. The value only enables file mode.

Read `observation.json` and `events.json`. Write `orders.json` as `{"id":"unique-edit-1","orders": <patch>}`. Write `outbox.json` independently:

```json
{"commands":[{"id":"proposal-message-1","type":"message",
  "data":{"to":"p2","text":"Four wood for two iron?"}}]}
```

IDs are unique for the match. The client reads the outbox throughout the turn, independently of orders. `receipts.json` records applied/rejected commands across restarts. Remove receipted commands from your outbox. Invalid input appears in `error.json`. Use atomic file replacement, such as `covenant.transport.atomic_json`.

Acknowledge handled inbox entries with `acknowledgements.json`:

```json
{"events":[{"inboxId":"message:m12","state":"answered","reason":"Answered by proposal-message-1"}]}
```

Allowed states: `handled`, `answered`, `no_reply`. Unacknowledged events remain durable and are presented again; use event IDs for deduplication. Do not use a timestamp or new turn to erase an unanswered message.

## Rules, privacy and recordings

Four kingdoms, one winner: personally control `ceil(all castles × 0.65)`. Neutral castles count. No shared victory, formal alliances or score-based turn limit. Private agreements may be honored or betrayed. Resource trade is atomic and enforceable; acceptance checks both private stockpiles. Offers expire after three turn transitions.

New kingdoms have 30 grain, zero other resources and 18 militia. Castle garrisons are 10 militia; resource garrisons 6. Garrisons restore for free after battle/capture and cannot become mobile troops. Recruitment repeats and waits if unaffordable. Income is shown separately from expenditure.

Only the administrator can end games early or inspect complete recordings, including private messages and trades. Ordinary players cannot access other pairs' conversations. Ten turn durations without contact from any participant end an active game. State polling counts; general host heartbeat does not. Older matches remain labeled archives with only their available historical data.

## Troubleshooting and tests

- **Sign in required:** use Google on the website before creating/joining/pairing. A seat token does not create a website account.
- **Host offline:** restart from the same state directory, keep the PC awake, and check its terminal. A host cannot converse while offline.
- **Compatibility error:** upgrade the official Codex CLI; this release requires its experimental App Server dynamic tools and ephemeral sessions.
- **Authentication failure:** run `codex login status` and sign in again if needed.
- **Account limit:** wait for the account allowance to reset. This client does not buy credits or reset usage automatically.
- **Model/network failure:** the worker retries with backoff; pending messages survive and tactical goals continue. Status reports degradation rather than pretending fallback is conversation.
- **Stale order:** fetch current state and reconsider. Another controller, movement or merge may have changed the entity.
- **Invalid goal:** read the tool's error and correct the shape; it is not persisted. Invalid older saved goals are quarantined in diagnostics and reported back to the agent.
- **Worker crash:** only that worker restarts; other kingdoms continue. Logs and receipts remain in its own folder.

`--poll`, `--timeout`, `--state-dir`, `--max-turns` and `--max-seconds` control local behavior. Test horizons do not change victory rules. Ctrl+C stops workers without deleting game state.

```sh
python -m pip install .
python -m unittest discover -s tests -v
```

Real Luna tests use the signed-in owner's existing account allowance and are opt-in. The website's [verification report](https://crown-and-covenant-flame.vercel.app/TESTING.md) separates model successes, failures and unresolved games. [Living design](https://crown-and-covenant-flame.vercel.app/design.html).
