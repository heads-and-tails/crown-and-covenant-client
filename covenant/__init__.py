"""Agent-first client for Crown & Covenant. No third-party runtime dependencies."""
from .agent import Agent, ReferenceAgent
from .controller import TacticalController
from .transport import Client, Connection, ProtocolError

__all__ = ['Agent', 'ReferenceAgent', 'TacticalController', 'Client', 'Connection', 'ProtocolError']
__version__ = '0.1.0'
