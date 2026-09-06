"""Run from the checkout: covenant run --connection connection.json --agent examples.my_agent:MyAgent"""

from covenant import Agent


class MyAgent(Agent):
    def on_turn(self, ctx):
        if not ctx.goals():
            ctx.set_goal(
                "strategy",
                {
                    "kind": "strategy",
                    "intent": {
                        "stance": "expand",
                        "preferredTroop": "archer",
                        "reserves": {"grain": 4},
                    },
                },
            )

    def on_message(self, ctx, message):
        text = message["text"].lower()
        if text.strip() in ("thanks", "thank you", "okay", "ok"):
            ctx.no_reply(message["id"], "Acknowledgement; no new question.")
        else:
            ctx.reply(
                message,
                "Send a concrete trade offer. I will compare the quantities and my available stock.",
            )
        ctx.write_memory(
            "diplomacy.md",
            ctx.read_memory("diplomacy.md")[-50000:]
            + f"\n{message['from']}: {message['text']}",
        )

    def on_events(self, ctx, events):
        for event in events:
            offer = event.get("data", {})
            if event["kind"] != "trade" or offer.get("status") != "pending":
                continue
            state = ctx.get_state()
            if offer.get("to") != state["you"]:
                continue
            affordable = all(
                state["treasury"][r] >= n for r, n in offer["want"].items()
            )
            fair = sum(offer["give"].values()) >= sum(offer["want"].values())
            ctx.answer(offer["id"], "accept" if affordable and fair else "reject")
