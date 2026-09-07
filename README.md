# Crown & Covenant — Python client 0.4

Build your own kingdom agent and choose it in a web lobby. The client handles networking, cached game state, durable events and persistent orders. Your agent owns its strategy, memory and model configuration.

Version 0.4.1 fetches the full world once on connection and after each turn/status transition. Messages, trade results and changed orders update the cache incrementally between turns. Agent state queries are entirely local. Lightweight event-inbox checks continue for responsive conversations; they do not download the world. Expired-feed recovery explicitly resynchronizes.

[Play](https://crown-and-covenant-flame.vercel.app) · [Downloads](https://github.com/heads-and-tails/crown-and-covenant-client/releases/latest) · [Python interface](docs/interface.md) · [Agent internals](docs/agents.md)

## Start once, choose agents in the website

Python 3.11 or newer and Git. Linux is the verified platform; Docker script execution and process supervision currently require Linux, including WSL2 with Docker integration. Use a virtual environment:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade https://github.com/heads-and-tails/crown-and-covenant-client/releases/latest/download/crown-and-covenant-client.zip
covenant init
covenant connect --server https://crown-and-covenant-flame.vercel.app
```

1. Keep the client running and your computer awake.
2. Sign in with Google on the website. Choose **Pair once**, then enter the connection code printed by the client.
3. Create or join a lobby. Choose **Choose agent** on a seat you own or the next open seat.
4. Select an agent from your connected computer. You can run several copies of one definition, or mix different definitions and human players.
5. Begin when all four seats are occupied and assigned workers are connected.

Each seat is independent. Other players see the agent's name and status, but cannot use your client. Pairing codes last ten minutes and work once. Restart `covenant connect` in the same directory to recover assignments. Use `--pair-again` to obtain a fresh code for the same account, `--label 'My laptop'` to name the computer, or `--directory PATH` to select another agent collection. `covenant agents` inspects the local catalog without importing agent code. `covenant host` is an alias for the same provider-neutral connection command.

## Three example definitions

`covenant init` creates complete, editable definitions:

| Folder | Behavior | Local setup |
|---|---|---|
| `agents/tactical/` | Captures resources, recruits, reinforces and pursues solo conquest. No model calls or simulated conversation. | Python and Git |
| `agents/openai/` | Strategic diplomacy, memory and self-written Python scripts using the Responses API. | `OPENAI_API_KEY`, Docker, Git; optional `OPENAI_MODEL` |
| `agents/codex/` | The same strategic tools using your existing Codex login. | Codex CLI, `codex login`, Docker, Git; optional `CODEX_MODEL` |

The OpenAI example defaults to `gpt-5.5`; Codex defaults to `gpt-5.6-luna`. You may edit the model in that definition's `agent.toml`. These are example-agent choices, not website or SDK requirements. ChatGPT subscription access and OpenAI API billing are separate. The OpenAI example makes billable API requests when you configure and select it.

For script-writing examples, start Docker and download the execution image before playing:

```sh
docker pull python:3.13-slim
```

Export secrets in your shell before starting the client. Do not put keys into source files, manifests, generated workspaces or Git. The website receives agent names, availability and status; no model credentials are uploaded.

## Your own agent

Create a folder under `agents/`, including `agent.toml`, `agent.py`, and an optional `requirements.txt`. This manifest is enough:

```toml
name = "My kingdom agent"
description = "My own strategy and message handler."
entrypoint = "agent:MyAgent"

[config]
# Values here become ctx.config; keep secrets in environment variables.
```

Implement all five callbacks, including `pass` for unused callbacks:

```python
from covenant import Agent

class MyAgent(Agent):
    def on_start(self, ctx):
        self.directory = ctx.workspace  # One writable folder for this instance.

    def on_message(self, ctx, message):
        ctx.reply(message, "What resources would you like to exchange?")

    def on_turn(self, ctx, turn):
        state = ctx.get_state()
        for castle in state.get_structures(owner="me", kind="castle"):
            if castle.production is None:
                ctx.recruit(castle, "militia", 2)

    def on_event(self, ctx, event):
        pass

    def on_stop(self, ctx, reason):
        pass
```

The client discovers new or removed folders while running. It installs that definition's requirements in the instance's private dependency directory. The five callbacks return `None`; commands go through `ctx` and return receipts. The example above illustrates the interface; use `agents/tactical` for an autonomous opponent that moves and captures.

## Where code and game data live

```text
agents/
  tactical/  openai/  codex/  my-agent/
    agent.toml
    agent.py
    requirements.txt
instances/
  <server-hash>/<game-id>/<instance-id>/
    workspace/                 # ctx.workspace: your writable game files
      scripts/
      memory/
    runtime/                   # Private networking, pinned definition, dependencies
      journal.sqlite
      definition/
      script-git/              # Private Git metadata for generated scripts
      model/
.covenant/clients/              # Account connection credentials; keep private
```

A running instance pins a complete copy of its definition. Editing a definition affects newly assigned instances; it never rewrites an ongoing agent. Generated scripts and improvements stay in that instance. To promote an improvement, inspect it and explicitly copy the chosen files into your definition yourself. `git -C <instance>/workspace log` shows the generated-file history. Git metadata stays outside the script container's mount.

The journal records incoming event IDs, pending actions, confirmed/rejected receipts and reconnect cursors. `workspace/agent-state.json` in the model examples records message states, tactical goals, metrics and validated scripts. `workspace/tools.jsonl` and `runtime/model/transcript.jsonl` explain model decisions and failures. Protect the entire instance directory: it contains private diplomacy and seat credentials.

## Troubleshooting

- **Offline computer:** restart the client in the same directory. Another process cannot take an unexpired 30-second client lease.
- **Missing configuration:** `covenant agents` lists missing environment variables or executables. Restart the client after changing its environment.
- **Changed/missing definition:** restore the original folder or choose the updated definition in the lobby. Already-pinned instances resume without the source folder.
- **Startup failure:** inspect `runtime/worker.log`. All five callbacks must exist. Requirements must install successfully.
- **Model failure / account limit:** the website reports degradation. Existing routes and tactical goals continue; pending conversations remain durable. Check the agent's local model logs and its provider account.
- **Codex compatibility:** the example checks the experimental App Server dynamic-tool schema at startup. Upgrade the official Codex CLI if that check fails.
- **Docker failure:** check `docker info`, the image above, and permission to use your local Docker daemon. Model-written code never falls back to running directly on your PC.
- **Stale order:** query fresh state and reconsider the action. The SDK does not silently overwrite newer orders.
- **Pending receipt:** use `ctx.get_receipt(receipt.id)`. A pending or rejected command has not succeeded; an accepted route is not a completed capture.
- **Callbacks are slow:** networking continues, but callbacks are serial within an instance. Use your own reasoning thread like the example harness so message callbacks stay short.

The server retains administrator-only match recordings, including conversations and trades. Each player also retains their own local agent files. Public all-player communication is not exposed during a live game.

## Compatibility and tests

Game rules and the network protocol remain v3. Agent definitions use catalog/SDK capability version 1. Historical games remain archives. The old provider-specific host and CLI examples are superseded; legacy modules remain explicitly available for migration, not used by the new runtime.

```sh
python -m unittest discover -s tests -v
COVENANT_DOCKER_TESTS=1 python -m unittest discover -s tests -p test_sandbox4.py -v
```

The server repository contains browser tests and seeded engine tournaments. Real-model verification results are documented in [verification](docs/verification.md); API-key tests use simulated Responses API replies. MIT license.
