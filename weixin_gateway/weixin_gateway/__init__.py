"""Minimal personal Weixin iLink gateway."""

from .protocol import IlinkClient, ProtocolError
from .store import GatewayStore, IdentityStore

__version__ = "0.3.3"

__all__ = ["GatewayStore", "IdentityStore", "IlinkClient", "ProtocolError"]
