"""freecad-ai: an MCP server that drives FreeCAD 1.1 over an XML-RPC bridge."""

from __future__ import annotations

from ._version import __version__
from .bridge import Bridge, BridgeError, start_headless, stop
from .server import main

__all__ = ["Bridge", "BridgeError", "__version__", "main", "start_headless", "stop"]
