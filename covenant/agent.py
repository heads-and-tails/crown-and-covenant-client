"""Turn planning and event-driven diplomacy, with legacy callback adapters."""
from .controller import TacticalController, RESOURCES


class Agent:
    def on_turn(self, observation: dict) -> dict:
        """Plan a turn. The runner keeps listening while this hook runs."""
        return self.decide(observation)

    def on_events(self, observation: dict, events: list[dict]) -> list[dict]:
        """React to private messages/trades at any time; return immediate commands."""
        return self.diplomacy(observation)

    def decide(self, observation: dict) -> dict:
        """Return {turn, moves, production, ready}. Override in your own agent."""
        return {'turn': observation['turn'], 'moves': [], 'production': [], 'ready': True}

    def diplomacy(self, observation: dict) -> list[dict]:
        """Return [{type: 'message'|'offer'|'answer', data: {...}}]."""
        return []


class ReferenceAgent(Agent):
    def __init__(self, intent: dict | None = None):
        self.controller = TacticalController()
        self.intent = intent or {}
        self._offered_turn = -1

    def decide(self, observation):
        return self.controller.plan(observation, self.intent)

    def diplomacy(self, o):
        you = o['you']
        me = next(p for p in o['players'] if p['id'] == you)

        actions = []
        for offer in o['offers']:
            if offer['to'] != you or offer['status'] != 'pending' or offer['expiresTurn'] <= o['turn']:
                continue
            accept = False
            if offer['kind'] == 'trade':
                accept = sum(offer['give'].values()) >= sum(offer['want'].values()) * .75 and all(o['treasury'][r] >= offer['want'].get(r, 0) for r in RESOURCES)
            if accept:
                actions.append({'type': 'answer', 'data': {'offerId': offer['id'], 'answer': 'accept'}})
        if self._offered_turn == o['turn']:
            return actions
        self._offered_turn = o['turn']
        opponents = [p for p in o['players'] if p['id'] != you and not p['eliminated']]
        if o['turn'] == 1:
            actions += [{'type': 'message', 'data': {'to': p['id'], 'text': f"Greetings from {me['name']}. I am securing nearby resources and welcome fair trades."}} for p in opponents]
        if o['turn'] % 3 == 0:
            surplus = max((r for r in RESOURCES if r != 'grain'), key=lambda r: o['treasury'][r])
            need = min((r for r in RESOURCES if r not in ('grain', surplus)), key=lambda r: o['treasury'][r])
            seller = next((p for p in opponents if any(s['owner'] == p['id'] and s.get('resource') == need for s in o['structures'])), None)
            if seller and o['treasury'][surplus] >= 8 and o['treasury'][need] < 8 and not any(f['from'] == you and f['to'] == seller['id'] and f['status'] == 'pending' for f in o['offers']):
                actions.append({'type': 'offer', 'data': {'to': seller['id'], 'kind': 'trade', 'give': {surplus: 4}, 'want': {need: 4}}})
        return actions
