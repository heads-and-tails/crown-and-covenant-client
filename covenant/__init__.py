"""Crown & Covenant v3: persistent commands and continuous communication."""

from .agent import Agent, ReferenceAgent
from .context import Context
from .transport import Client, Connection, ProtocolError
from .runner import Runner

__version__ = "0.3.3"
from .controller import TacticalController
