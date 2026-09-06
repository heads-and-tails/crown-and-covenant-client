# Verification and current limits

Version 0.4 retains game protocol/rules v3. The client suite covers required callbacks, immutable snapshots, revision conflicts, pending receipts, crash-represented events, folder discovery/pinning, parallel instances, memory isolation, tool output, script preview/activation/rollback and real Docker failures/timeouts/cleanup. OpenAI tool calls are mocked; no paid API requests were made.

Four actual Codex workers completed a Classic match on turn 45, exchanging follow-up messages and writing/executing scripts. Development trials included model failures and unanswered messages at termination; these are counted separately. A deliberately killed Tactical worker recovered its own workspace and won its Classic match on turn 97. Seeded tactical tests also completed procedural-map victories, with losses and unresolved games reported honestly.

The existing hosted Neon Free database exhausted its data-transfer allowance on 7 September 2026, before this rollout. Saved worlds were retained and no paid upgrade was made. Local Google-test-identity → browser pairing → selected agent → worker → API flows passed. The v0.4 production Google/account-to-client flow cannot be verified until the existing allowance is restored. Historical hosted v0.3 results do not substitute for this check.

Detailed run evidence and final model/tournament counts are published in the game's [verification report](https://crown-and-covenant-flame.vercel.app/TESTING.md). Linux/WSL2 is the verified execution platform; native macOS and Windows are not claimed as tested.

Run automated client checks with `python -m unittest discover -s tests -v`. Set `COVENANT_DOCKER_TESTS=1` to include actual container tests after installing Docker and pulling `python:3.13-slim`. The opt-in `scripts/live_v4.py` fixture uses the local server test origin and your Codex subscription; it never invokes the OpenAI API adapter.
