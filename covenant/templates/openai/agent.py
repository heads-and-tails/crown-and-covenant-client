from strategy import StrategicAgent
from model import Model


class MyAgent(StrategicAgent):
    def make_model(self, ctx):
        return Model(ctx)
