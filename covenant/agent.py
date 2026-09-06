"""Provider-independent player interface. All five hooks are explicit."""

from abc import ABC, abstractmethod


class Agent(ABC):
    @abstractmethod
    def on_start(self, ctx):
        """Initial state is available; ctx.resumed identifies recovery."""

    @abstractmethod
    def on_message(self, ctx, message):
        """A private message with a durable message.id."""

    @abstractmethod
    def on_turn(self, ctx, turn):
        """First playable turn and each newly observed playable turn."""

    @abstractmethod
    def on_event(self, ctx, event):
        """Trades, battles, captures, merges and connection changes."""

    @abstractmethod
    def on_stop(self, ctx, reason):
        """Graceful stop; forcibly killed processes cannot run cleanup."""
