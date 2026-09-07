"""Crown & Covenant: provider-independent agents and persistent game commands."""

from .agent import Agent
from .context import Context
from .runtime import Runtime
from .state import (
    GameState,
    Army,
    Structure,
    Player,
    Record,
    RouteOrder,
    ProductionOrder,
)
from .transport import Client, Connection, ProtocolError

# Explicit compatibility imports; new agents use Agent + Runtime.
from .legacy import ReferenceAgent
from .controller import TacticalController
from .runner import Runner

__version__ = "0.4.1"
