"""graphforge — forge Git repositories and relational databases into a Neo4j knowledge graph.

Published on PyPI as ``graphforge-neo4j``; imported as ``graphforge``.
"""
from __future__ import annotations

try:
    from importlib.metadata import version

    __version__ = version("graphforge-neo4j")
except Exception:  # not installed (source/editable/dev) — fall back to the literal
    __version__ = "0.1.0"
