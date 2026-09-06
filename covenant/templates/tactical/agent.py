"""A complete player-written definition: change these hooks to build your own agent."""

import json
from covenant import Agent
from covenant.tactics import TacticalBot
from covenant.transport import atomic_json


class MyAgent(Agent):
    def on_start(self, ctx):
        self.file = ctx.workspace / "tactics.json"
        try:
            self.memory = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.memory = {}
        self.bot = TacticalBot(self.memory)

    def on_message(self, ctx, message):
        with (ctx.workspace / "messages.jsonl").open("a") as f:
            f.write(json.dumps(message.to_dict()) + "\n")
        # This example is deliberately not a conversational model.

    def on_turn(self, ctx, turn):
        self.bot.step(ctx)
        atomic_json(self.file, self.memory)
        if ctx.config.get("fast"):
            ctx.set_ready(turn)

    def on_event(self, ctx, event):
        if event.kind in ("trade", "offer"):
            for offer in ctx.get_state().get_trades("pending"):
                if offer.to_player == ctx.get_state().you and sum(
                    offer.give.values()
                ) >= sum(offer.want.values()):
                    ctx.respond_trade(offer, "accept")

    def on_stop(self, ctx, reason):
        atomic_json(self.file, self.memory)
