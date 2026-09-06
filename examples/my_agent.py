"""Run: covenant run --connection connection.json --agent examples.my_agent:MyAgent"""
from covenant import Agent, TacticalController


class MyAgent(Agent):
    def __init__(self):
        self.controller = TacticalController()
        self.intent = {'stance': 'expand', 'preferredTroop': 'archer'}

    def on_turn(self, observation):
        return self.controller.plan(observation, self.intent)

    def on_events(self, observation, events):
        commands = []
        for event in events:
            offer = event.get('data', {})
            if event['kind'] == 'trade' and offer.get('to') == observation['you'] and offer.get('status') == 'pending':
                affordable = all(observation['treasury'][r] >= n for r, n in offer['want'].items())
                fair = sum(offer['give'].values()) >= sum(offer['want'].values())
                if affordable and fair:
                    commands.append({'id': f"accept-{offer['id']}", 'type': 'answer',
                                     'data': {'offerId': offer['id'], 'answer': 'accept'}})
        return commands
