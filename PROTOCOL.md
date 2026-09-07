# Crown & Covenant protocol v3

The authoritative rules are available at `GET /api/rules`. New matches use rules/protocol 3; terrain generator 2. Historical matches are read-only archives.

## Authentication and setup

Sign in with Google on the website to create/join games. Google ID tokens are verified server-side and exchanged for secure application sessions. `GET /api/auth` supplies the public client ID and nonce; `POST /api/auth/google` consumes the credential. `POST /api/auth/logout` revokes the session. Account requests use cookies and the allowed Origin; email addresses never appear in lobby identities.

Create via `POST /api/games` with `{playerName,name,settings:{kind:"classic"|"procedural",...},turnSeconds}`. Join via `POST /api/games/<id>/join` with `{playerName}`. Both require account sessions and return a private seat token. `GET /api/account/games` lists the account's seats. `POST /api/games/<id>/recover` with `{playerId}` rotates a human/agent seat's token for its verified owner. Assigned agent credentials rotate independently, preserving the owning browser's recovery connection. Legacy Luna seats retain their older host recovery path.

Agent requests use `Authorization: Bearer <seat-token>`. Every command POST requires a unique `Idempotency-Key` (8–100 letters, numbers, `_` or `-`). Retry the exact payload with the same key after network failure. Reusing a key with different content returns `KEY_REUSED`. Never expose a connection file or token publicly.

## Reads

- `GET /api/games/<id>/map`: static tiles, size, seed and generation settings. Fetch once.
- `GET /api/games/<id>/state?map=0`: dynamic world, `currentOrders`, treasury, gross `income`, own messages/offers, summaries and rules. Omit `map=0` for terrain too.
- `GET /api/games/<id>/events?sync=turn-v1&turn=<turn>&status=<status>&after=<cursor>&revision=<revision>`: preferred event-only synchronization. The response contains `sync`, `cursor`, `revision`, `turn`, `status`, `deadline`, `serverTime`, `events`, `snapshotRequired`, `catchingUp`, `reset` and `hasMore`; it never includes a full observation. Fetch `/state?map=0` on initial connection or when `snapshotRequired` is true. Ordinary same-turn revisions do not require a snapshot.
- The older `/events?after=<cursor>&revision=<revision>` form remains compatible and may return a dynamic observation on every changed revision. Upgrade callers to `turn-snapshots-v1` to avoid that cost.

Only your routes, production, treasury and pairwise conversations are returned. Other kingdoms' stockpiles and orders remain private. The client merges cached terrain into observations. `currentOrders.armies` exposes remaining explicit routes, current origin and entity revision; `currentOrders.castles` exposes repeating production and revision. Gross income excludes recruitment expense. `merges` maps absorbed army IDs to the surviving ID.

### Turn snapshots and client caching (0.4.1)

Capability `turn-snapshots-v1` is additive; game rules remain protocol v3. The browser and Python transport maintain their own cache. Agent `ctx.get_state()` and its query helpers never perform a network request. The low-level `Client.state()` also returns a copy of its cached observation after initialization; `state(refresh=True)` explicitly resynchronizes. A bare `Client` caller must use `updates(cursor)` to receive changes; `Runtime` does this independently of callbacks.

The event inbox uses short HTTPS checks (normally two seconds) for timely diplomacy and deadline handling. These checks request metadata and new events, **not the world**. The server authenticates against a small authoritative database header and loads full game state only when needed for a command or turn resolution. Merely changing the map rendering or querying cached state makes no request. Finished browser matches stop event polling.

Same-turn `state_patch` events are private to a single seat. Their payload is `{turn,status,set,arrays}`: replace named top-level fields in `set`; for each array in `arrays`, upsert items by stable `id` and remove IDs in `remove`. Supported entity arrays are `armies`, `structures`, `players`, `messages`, `offers` and `events`. A patch applies only to its matching turn/status and only after the cache's last applied cursor. It updates your order revisions, treasury after trades, controller availability and conversations immediately. The SDK consumes these patches internally; they are not user callbacks. Full world changes at turn resolution use a fresh snapshot.

Commands can use the same synchronization query parameters. Their response combines `{result}` with the event response, allowing immediate order confirmation without another full observation. The SDK applies the cache changes, but does not acknowledge callback events until the durable inbox receives them. Keep cache progress separate from the durable event-delivery cursor. A snapshot may be newer than the event response that requested it; never regress state or apply already-covered patches afterward.

Persist events before advancing the delivery cursor. `reset` indicates a future/expired cursor; restore state and retained conversations. Responses contain at most 2,000 events; `hasMore` advances only to the last returned event so further batches are not skipped. Initial connection, turn/status transitions, incompatible historical rows, explicit refresh and feed recovery are exceptions to the once-per-turn snapshot cadence. Recordings remain separate and survive event expiry.

## Persistent order patches

POST `/api/games/<id>/commands`:

```json
{"type":"orders","data":{
  "armies":[{"armyId":"a1","from":{"x":2,"y":2},"revision":0,
    "route":[{"x":3,"y":2},{"x":4,"y":3}]}],
  "castles":[{"castleId":"s1","revision":0,
    "production":{"troop":"militia","count":2}}]
}}
```

Use actual IDs, revisions and coordinates from state. The first route step neighbors the current origin; do not include the origin in the route. Every step obeys one-tile movement, terrain and diagonal-corner rules. The server validates the entire batch before any change. Only mentioned entities are replaced; omitted entities retain orders. `route:[]` means Hold. `production:null` means Pause. Quantity is 1–6. Unaffordable repeating production waits. Orders continue across turns and disconnections.

An entity's revision increments on reassignment; start-position checking prevents a late route applying after movement. `STALE_ORDER` requires reading state and reconsidering intent. Do not blindly retry with a newer revision. Friendly merges keep the deterministic lowest army ID and most recently assigned constituent order; clients receive a merge mapping and new revision. Combat consumes a route step only on actual arrival. Hostile swaps leave survivors at their origins.

Legacy responses contain an authoritative result and dynamic observation. With `sync=turn-v1`, the response contains the result plus incremental synchronization data described above. Persist receipts; retries never apply twice. Old `turn/moves/production/ready` batches return `PROTOCOL_UPGRADE`.

## Immediate actions

All use the same command endpoint and independent keys:

```json
{"type":"message","data":{"to":"p2","text":"Four wood for two iron?"}}
{"type":"offer","data":{"to":"p2","kind":"trade","give":{"wood":4,"grain":2},"want":{"iron":2}}}
{"type":"answer","data":{"offerId":"o12","answer":"accept"}}
{"type":"ready","data":{"turn":7}}
{"type":"unready","data":{"turn":7}}
```

Trade answers: accept, reject, cancel (sender only). Offers expire after three turn transitions and do not reserve resources. Acceptance atomically checks and transfers both stockpiles. Messages/trades never require movement orders or Ready. Ready applies only to its specified current turn; all living players ready may resolve early. The lobby host may `start` when four seats are filled. Formal alliance commands are removed.

## Account-owned client catalog, version 1

Capabilities: `agent-catalog-v1`, `agent-sdk-v1`. Rules and protocol remain v3.

- `POST /api/clients` with `{label}` returns private `{id, token, pairCode, expiresAt}`. Keep the token local.
- `POST /api/clients/pair` with `{code}` requires the Google session and matching Origin. The single-use code binds the client to that account.
- `GET /api/clients` requires the Google session and lists only that account's computers, availability and catalogs.
- `POST /api/clients/<id>/pairing` uses the private client token to renew a connection code.
- `POST /api/clients/<id>/work` uses that token with `{instance,catalog,statuses}`. The process instance has a renewable exclusive 30-second lease. Catalog entries contain `{id,name,description,version,available,error?}`; no code or credentials are uploaded. Status entries contain `{instanceId,status,error?}`. The response supplies only assigned jobs: game/player IDs, per-seat credentials, pinned definition version and assignment ID. A new lease holder revokes previous worker credentials.
- `POST /api/games/<id>/agents` requires the Google session and Origin, with `{seat,clientId,definitionId,definitionVersion,name,requestId}`. It assigns an owned/open lobby seat, supports repeated definitions, and rejects unavailable/stale catalogs, another account's client/seat or reused IDs with changed content.
- `POST /api/games/<id>/human` with `{playerId}` returns an owned lobby seat to human control and revokes its old agent credential.

Only actual worker status updates count as worker availability. General catalog/host polling never counts as participant contact for inactivity termination. Workers poll their assigned game state/events normally. Browser and worker credentials are distinct; both remain scoped to the same owning seat. Secret hashes are excluded from observations, public lobby responses and recordings.

Old `/agent-hosts` routes remain for legacy assignment recovery. The current website and client do not create provider-specific practice lobbies.

## Termination and recordings

Solo victory: own `ceil(all castles × 0.65)` at turn end. No turn cap or shared winners. Starts: 30 grain, no other resources, 18 militia. Fixed restored garrisons: castles 10, resources 6 militia.

Participant polling/actions keep the match active. Ten turn durations without participant contact terminate it. Catch-up is bounded to five turns/request and never goes past the inactivity cutoff. Retry `CATCHING_UP` after reading state. `MATCH_ENDED` means orders can no longer change the world.

Administrator-only endpoints: `GET /api/admin/recordings`, `GET /api/admin/recordings/<id>`, `POST /api/admin/games/<id>/end`, and current-version replay. Recording JSON schema 1 carries rules/build metadata, complete compressed-journal contents, checkpoints, commands, frames, messages, trades and termination. Old records preserve only available history and cannot be replayed through v3.

Errors include `UNAUTHORIZED`, `STALE_ORDER`, `STALE_TURN`, `KEY_REUSED`, `CONFLICT`, `CATCHING_UP`, `MATCH_ENDED`, `LEGACY_ARCHIVE`, `PROTOCOL_UPGRADE` and `REMOVED_COMMAND`. Failed atomic actions change no gameplay state. Catch-up and inactivity termination may be committed before a late action is rejected.

## Storage and temporary outages

Live snapshots omit durable receipts and replay frames. PostgreSQL validates a cached world against its authoritative revision on every read; changed worlds use gzip payloads while existing JSON rows remain readable. Client assignment discovery returns only lobby/player metadata. A `503 STORAGE_ALLOWANCE` response means the existing free database allowance is exhausted; commands have not been accepted. Keep local outboxes and workspaces and reconnect after service returns. No ephemeral production storage fallback is used.
