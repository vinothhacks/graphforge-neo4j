"""MCP server exposing the knowledge graph to any MCP client."""

from .server import GraphQuery, build_server, run

__all__ = ["GraphQuery", "build_server", "run"]
