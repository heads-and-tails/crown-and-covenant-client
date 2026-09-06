"""Copy this file into agents/my-agent/agent.py and add the README's manifest."""

import json
from covenant import Agent
from covenant.tactics import TacticalBot
from covenant.transport import atomic_json


class MyAgent(Agent):
    def on_start(self, ctx):
        self.file = ctx.workspace / "campaign.json"
        try:
            memory = json.loads(self.file.read_text())
        except (OSError, ValueError):
            memory = {}
        self.bot = TacticalBot(memory)

    def on_message(self, ctx, message):
        with (ctx.workspace / "messages.jsonl").open("a") as f:
            f.write(json.dumps(message.to_dict()) + "\n")
        if "?" in message.text:
            ctx.reply(
                message,
                "I can consider a concrete trade offer. My commander is expanding resource income.",
            )

    def on_turn(self, ctx, turn):
        self.bot.step(ctx)
        atomic_json(self.file, self.bot.memory)

    def on_event(self, ctx, event):
        if event.kind == "trade" and event.get("data", {}).get("status") == "pending":
            offer = event.data
            if offer.to_player == ctx.get_state().you and sum(
                offer.give.values()
            ) >= sum(offer.want.values()):
                ctx.respond_trade(offer, "accept")

    def on_stop(self, ctx, reason):
        atomic_json(self.file, self.bot.memory)
