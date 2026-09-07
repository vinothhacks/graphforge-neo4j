"""Optional code<->database linking passes over the loaded graph.

These run AFTER git and db ingestion and create edges between the two otherwise
separate subgraphs. Text-based passes (BASED_ON / USES_TABLE / CROSS_DB_REFERENCE)
are heuristic substring matches and are scoped by database and a minimum table-name
length to limit false positives; MAPS_TO is an exact JPA table-name match.
"""

from .passes import PASSES, LinkRunner

__all__ = ["PASSES", "LinkRunner"]
