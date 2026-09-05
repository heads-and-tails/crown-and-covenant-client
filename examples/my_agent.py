from covenant import Agent, TacticalController


class MyAgent(Agent):
    def decide(self, observation):
        return TacticalController().plan(observation, {'stance': 'expand', 'preferredTroop': 'knight'})

    def diplomacy(self, observation):
        # Add {type: 'message'|'offer'|'answer', data: {...}} commands here.
        return []
