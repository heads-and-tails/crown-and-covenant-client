"""A crashed or slow custom Python agent cannot block the transport forever."""
from __future__ import annotations
import importlib
import multiprocessing
from .agent import Agent


def _serve(pipe, spec):
    module, name = spec.split(':', 1)
    agent = getattr(importlib.import_module(module), name)()
    while True:
        method, obs = pipe.recv()
        try:
            pipe.send((True, getattr(agent, method)(obs)))
        except Exception as error:
            pipe.send((False, type(error).__name__ + ': ' + str(error)[:200]))


class IsolatedAgent(Agent):
    def __init__(self, spec: str, timeout: float = 15):
        if ':' not in spec:
            raise ValueError('Use module:ClassName for a custom agent.')
        self.spec, self.timeout = spec, timeout
        self.process = self.pipe = None

    def _call(self, method, obs):
        if not self.process or not self.process.is_alive():
            context = multiprocessing.get_context('spawn')
            self.pipe, child = context.Pipe()
            self.process = context.Process(target=_serve, args=(child, self.spec), daemon=True)
            self.process.start()
            child.close()
        self.pipe.send((method, obs))
        if not self.pipe.poll(self.timeout):
            self.process.terminate()
            self.process.join(timeout=2)
            self.pipe.close()
            raise TimeoutError('Custom agent exceeded its decision deadline.')
        ok, result = self.pipe.recv()
        if not ok:
            raise RuntimeError(result)
        return result

    def decide(self, observation):
        return self._call('decide', observation)

    def diplomacy(self, observation):
        return self._call('diplomacy', observation)

    def close(self):
        if self.process:
            self.process.terminate()
            self.process.join(timeout=2)
        if self.pipe:
            self.pipe.close()
