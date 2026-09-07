"""graphforge — forge Git repositories and relational databases into a Neo4j knowledge graph.

Published on PyPI as ``graphforge-neo4j``; imported as ``graphforge``.
"""

from __future__ import annotations

__all__ = ["__version__"]


def __getattr__(name: str) -> str:
    """Resolve ``__version__`` on first access (PEP 562).

    ``importlib.metadata.version()`` was the single largest cost of importing
    ``graphforge.cli`` — more than half of it — and only ``--version`` needs the
    answer. Deferring it means every other command stops paying for it.
    """
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from importlib.metadata import version

        resolved = version("graphforge-neo4j")
    except ImportError:
        # Not installed at all (a bare source tree); PackageNotFoundError is an
        # ImportError subclass, so this covers that too. A literal release number
        # here silently drifts from pyproject.toml -- it read "0.1.0" long after
        # the project was 0.2.0, so `graphforge --version` simply lied. Something
        # obviously-not-a-release cannot.
        resolved = "0+unknown"
    globals()["__version__"] = resolved  # cache: __getattr__ fires at most once
    return resolved
