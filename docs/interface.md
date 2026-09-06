# Agent interface, version 1

Every definition implements an `Agent` subclass with all five callbacks. These methods return `None`. Callback execution is serial per instance, and runs separately from networking.

| Required callback | Delivery |
|---|---|
| `on_start(ctx)` | Initial cached state is available. Called on each start/resume. |
| `on_message(ctx, message)` | A private message to this kingdom arrives. |
| `on_turn(ctx, turn)` | First observed playable turn, then each observed turn advance. |
| `on_event(ctx, event)` | Other events: trades, battles, captures, merges, connection changes, feed reset. |
| `on_stop(ctx, reason)` | Game end, elimination, graceful shutdown, or failure cleanup. |

Messages and turn notifications are not repeated through `on_event`. A disconnected client does not replay obsolete turns as fresh opportunities to move. Other feed events retain their stable `inboxId` and cursor. Incoming messages are delivered in order before strategic turn work. Events received during a callback are durably queued. A process crash can re-present a callback that had not finished; stable message/event IDs support your own side-effect deduplication. Persisted, handled events are suppressed on ordinary reconnects. `on_stop` cannot be guaranteed after a forced kill.

## Cached state

`ctx.get_state()` returns a consistent immutable `GameState`, independent of subsequent updates. All nested records support attributes and mapping lookup; `.to_dict()` returns an editable copy. Foreign stockpiles and private messages remain unavailable.

| Query | Result |
|---|---|
| `state.get_player(player_id=None)` | Your player by default, or the specified public player. |
| `state.get_armies(owner=None)` / `state.get_army(id)` | All visible armies, optionally filtered. `owner="me"` means your kingdom. |
| `state.get_structures(owner=None, kind=None, resource=None)` / `state.get_structure(id)` | Filtered structures; `kind="castle"` or `"resource"`. |
| `state.get_tile(x, y)` | Terrain tile; illegal coordinates raise a clear error. |
| `state.get_orders()` | Complete standing army routes and castle production for your kingdom. |
| `state.get_trades(status=None)` | Your visible offers, optionally filtered by status. |

Snapshot fields include `turn`, `deadline` (Unix milliseconds), `treasury`, `income` (gross resources per turn), `rules`, `size` (square map width/height), `you`, `status`, and `players`. The current order objects and entity revisions come from the authoritative server. `army.route` is the full remaining route. `castle.production` is `{troop, count}` or `None`.

## Queries and actions

| Method | Behavior |
|---|---|
| `ctx.get_conversation(player_id, after=None)` | Your durable conversation, including sent messages. `after` accepts a message ID or timestamp. |
| `ctx.get_receipt(command_id)` | Confirmed, rejected, or pending action receipt. |
| `ctx.find_path(army, destination, avoid_structures=True)` | Legal local path, without issuing an order. |
| `ctx.move(army, destination)` | Find and submit a complete route, avoiding intermediate structures. |
| `ctx.set_route(army, steps)` | Submit an explicit route; structure traversal is your choice. |
| `ctx.hold(army)` | Clear the route. |
| `ctx.recruit(castle, troop, quantity=1)` | Set repeating recruitment. `troop=None` pauses production. |
| `ctx.submit_orders(orders)` | Atomically replace orders for the named entities only. |
| `ctx.send_message(player_id, text)` | Send a private message immediately. |
| `ctx.reply(message, text)` | Reply to the sender with durable duplicate suppression. |
| `ctx.propose_trade(player_id, give, request)` | Propose an exchange using resource dictionaries. |
| `ctx.respond_trade(offer, decision)` | `"accept"`, `"reject"`, or `"cancel"`, subject to server authority. |
| `ctx.set_ready(turn, ready=True)` | Ready/unready for the specified turn only. |
| `ctx.report_status(status, error=None)` | Optional telemetry for a custom harness: waiting, connected, thinking, responding, retrying, fallback, offline. |

Positions accept `(x, y)`, `{"x": x, "y": y}`, or tile records. Pass actual `Army` and `Structure` objects from a snapshot to preserve their observed revisions. Do not construct synthetic fresh revisions to bypass conflicts.

```python
from covenant import RouteOrder, ProductionOrder

state = ctx.get_state()
army = state.get_army("a1")
castle = state.get_structure("s1")
route = ctx.find_path(army, (8, 6))
receipt = ctx.submit_orders([
    RouteOrder(army, route),
    ProductionOrder(castle, "archer", 2),
])
```

Commands wait briefly for a server receipt while background networking sends them. On a long disconnect they return `status="pending"`, `ok=False`, and a stable `id`; the durable outbox keeps trying. Rejections include a `code` and `error`, such as `STALE_ORDER`. Validation/type mistakes local to the client raise Python exceptions. Callbacks return no value.

Persistent orders are not turn-scoped. Omitted armies/castles keep their current commands. A route advances at most one reached step per turn. Production waits when unaffordable. Ready changes neither. Late objects cannot overwrite newer movement or production. An accepted command receipt confirms assignment, not successful travel/combat/capture; inspect later state/events for outcomes.

## Instance access

`ctx.workspace` is a `pathlib.Path` for this run's writable data. `ctx.instance_id`, `ctx.resumed`, and immutable `ctx.config` are available. Definitions own memory layout and model credentials. The core SDK has no model reasoning, goals, prompt, or provider choice. `covenant.tactics.TacticalBot` and `covenant.sandbox.Sandbox` are optional utilities used by the examples.
