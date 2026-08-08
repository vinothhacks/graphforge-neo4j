"""Local web dashboard: masked config + live load status for the graph."""

from .server import build_status, serve

__all__ = ["build_status", "serve"]
