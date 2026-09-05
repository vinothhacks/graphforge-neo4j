"""graphforge — forge Git repositories and relational databases into a Neo4j knowledge graph.

Published on PyPI as ``graphforge-neo4j``; imported as ``graphforge``.
"""

from __future__ import annotations

try:
    from importlib.metadata import version

    __version__ = version("graphforge-neo4j")
except ImportError:
    # Not installed at all (a bare source tree). A literal release number here
    # silently drifts from pyproject.toml -- it read "0.1.0" long after the
    # project was 0.2.0, so `graphforge --version` simply lied. Something
    # obviously-not-a-release cannot.
    __version__ = "0+unknown"
