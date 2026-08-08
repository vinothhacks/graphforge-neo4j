"""Expose the forged knowledge graph over the Model Context Protocol (MCP).

Run `graphforge mcp` and any MCP client (Claude Desktop, other agents) can query
the graph with these tools:

* ``get_schema``      — labels, relationship types, and per-label counts
* ``read_cypher``     — run a read-only Cypher query (writes are rejected)
* ``search_nodes``    — substring search on a property of a given label
* ``node_neighbors``  — the immediate neighbourhood of a node id
* ``find_code``       — locate files / classes / methods by name
* ``find_table``      — locate tables / columns by name

The graph-query logic lives in :class:`GraphQuery` (driver in, dicts out) so it
can be unit-tested without the MCP runtime. The ``mcp`` package is imported
lazily, only when the server is actually started.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..core.config import Neo4jSettings, load_settings

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|CALL\s+apoc\.\w+\.(?:create|delete))\b", re.IGNORECASE)


class GraphQuery:
    """Thin, read-focused query helper over a Neo4j driver."""

    def __init__(self, driver, database: str = "neo4j"):
        self.driver = driver
        self.database = database

    @classmethod
    def connect(cls, settings: Neo4jSettings) -> GraphQuery:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password))
        driver.verify_connectivity()
        return cls(driver, settings.database)

    def close(self) -> None:
        self.driver.close()

    # -- primitives --------------------------------------------------------
    def _read(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[dict]:
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                lambda tx: [r.data() for r in tx.run(cypher, params or {})]
            )

    # -- tools -------------------------------------------------------------
    def get_schema(self) -> dict:
        labels = [r["label"] for r in self._read("CALL db.labels() YIELD label RETURN label")]
        rels = [r["relationshipType"] for r in
                self._read("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
        counts = {}
        for label in labels:
            rec = self._read(f"MATCH (n:`{label}`) RETURN count(n) AS c")
            counts[label] = rec[0]["c"] if rec else 0
        return {"labels": labels, "relationshipTypes": rels, "nodeCountsByLabel": counts}

    def read_cypher(self, query: str, params: Optional[Dict[str, Any]] = None, limit: int = 200) -> List[dict]:
        if _WRITE.search(query):
            raise ValueError("read_cypher only accepts read-only queries (no CREATE/MERGE/DELETE/SET/…).")
        if " LIMIT " not in query.upper():
            query = query.rstrip("; \n") + f"\nLIMIT {int(limit)}"
        return self._read(query, params)

    def search_nodes(self, label: str, prop: str, value: str, limit: int = 25) -> List[dict]:
        if not _IDENT.fullmatch(label) or not _IDENT.fullmatch(prop):
            raise ValueError("label and prop must be simple identifiers")
        cypher = (
            f"MATCH (n:`{label}`) WHERE toString(n.`{prop}`) CONTAINS $value "
            "RETURN n LIMIT $limit"
        )
        return self._read(cypher, {"value": value, "limit": int(limit)})

    def node_neighbors(self, node_id: str, limit: int = 50) -> List[dict]:
        cypher = (
            "MATCH (n {id: $id})-[r]-(m) "
            "RETURN type(r) AS rel, startNode(r).id AS fromId, m.id AS neighborId, "
            "labels(m) AS neighborLabels LIMIT $limit"
        )
        return self._read(cypher, {"id": node_id, "limit": int(limit)})

    def find_code(self, text: str, limit: int = 25) -> List[dict]:
        cypher = (
            "MATCH (n) WHERE (n:File OR n:Class OR n:Method) AND "
            "(toString(n.name) CONTAINS $t OR toString(n.fqn) CONTAINS $t OR toString(n.path) CONTAINS $t) "
            "RETURN labels(n) AS labels, n.name AS name, coalesce(n.fqn, n.path) AS ref, n.repo AS repo "
            "LIMIT $limit"
        )
        return self._read(cypher, {"t": text, "limit": int(limit)})

    def find_table(self, text: str, limit: int = 25) -> List[dict]:
        cypher = (
            "MATCH (n) WHERE (n:Table OR n:Column) AND toString(n.name) CONTAINS $t "
            "RETURN labels(n) AS labels, n.name AS name, n.table AS table, n.database AS database "
            "LIMIT $limit"
        )
        return self._read(cypher, {"t": text, "limit": int(limit)})

    def find_procedure(self, text: str, limit: int = 25) -> List[dict]:
        cypher = (
            "MATCH (p:StoredProcedure) "
            "WHERE toString(p.name) CONTAINS $t OR toString(p.definition) CONTAINS $t "
            "RETURN p.database AS database, p.name AS name, p.parameters AS parameters "
            "LIMIT $limit"
        )
        return self._read(cypher, {"t": text, "limit": int(limit)})

    def impact_of_column(self, column: str) -> dict:
        """Cross-schema blast radius of a column: matching columns, FKs, indexes,
        and stored procedures / views whose body references the name."""
        cols = self._read(
            "MATCH (c:Column) WHERE toLower(c.name) CONTAINS toLower($col) "
            "RETURN c.database AS database, c.table AS table, c.name AS column, "
            "c.dataType AS dataType ORDER BY c.database, c.table", {"col": column})
        fks = self._read(
            "MATCH (c1:Column)-[:FOREIGN_KEY]->(c2:Column) "
            "WHERE toLower(c1.name) CONTAINS toLower($col) OR toLower(c2.name) CONTAINS toLower($col) "
            "RETURN c1.table AS fromTable, c1.name AS fromColumn, "
            "c2.table AS toTable, c2.name AS toColumn", {"col": column})
        indexes = self._read(
            "MATCH (i:Index) WHERE any(x IN i.columns WHERE toLower(x) CONTAINS toLower($col)) "
            "RETURN i.database AS database, i.table AS table, i.name AS name, "
            "i.columns AS columns, i.isUnique AS isUnique", {"col": column})
        procs = self._read(
            "MATCH (p:StoredProcedure) WHERE p.definition IS NOT NULL "
            "AND toLower(p.definition) CONTAINS toLower($col) "
            "RETURN p.database AS database, p.name AS procedure "
            "ORDER BY p.database, p.name", {"col": column})
        views = self._read(
            "MATCH (v:View) WHERE v.definition IS NOT NULL "
            "AND toLower(v.definition) CONTAINS toLower($col) "
            "RETURN v.database AS database, v.name AS view", {"col": column})
        return {"columns": cols, "foreignKeys": fks, "indexes": indexes,
                "storedProcedures": procs, "views": views}


def build_server(settings: Optional[Neo4jSettings] = None):
    """Construct a FastMCP server with graph tools bound to a live connection."""
    from mcp.server.fastmcp import FastMCP  # lazy: only needed to actually serve

    settings = settings or load_settings().neo4j
    gq = GraphQuery.connect(settings)
    server = FastMCP("graphforge")

    def _json(obj: Any) -> str:
        return json.dumps(obj, indent=2, default=str)

    @server.tool()
    def get_schema() -> str:
        """Return graph labels, relationship types, and node counts per label."""
        return _json(gq.get_schema())

    @server.tool()
    def read_cypher(query: str, limit: int = 200) -> str:
        """Run a READ-ONLY Cypher query against the knowledge graph and return rows as JSON."""
        return _json(gq.read_cypher(query, limit=limit))

    @server.tool()
    def search_nodes(label: str, prop: str, value: str, limit: int = 25) -> str:
        """Substring-search nodes of `label` where property `prop` contains `value`."""
        return _json(gq.search_nodes(label, prop, value, limit))

    @server.tool()
    def node_neighbors(node_id: str, limit: int = 50) -> str:
        """Return the immediate neighbours of the node with the given `id`."""
        return _json(gq.node_neighbors(node_id, limit))

    @server.tool()
    def find_code(text: str, limit: int = 25) -> str:
        """Find File / Class / Method nodes whose name, fqn, or path contains `text`."""
        return _json(gq.find_code(text, limit))

    @server.tool()
    def find_table(text: str, limit: int = 25) -> str:
        """Find Table / Column nodes whose name contains `text`."""
        return _json(gq.find_table(text, limit))

    @server.tool()
    def find_procedure(text: str, limit: int = 25) -> str:
        """Find StoredProcedure nodes whose name or SQL body contains `text`."""
        return _json(gq.find_procedure(text, limit))

    @server.tool()
    def impact_of_column(column: str) -> str:
        """Blast radius of a column change: matching columns across schemas, foreign
        keys, indexes, and stored procedures / views whose body references it."""
        return _json(gq.impact_of_column(column))

    return server


def run(settings: Optional[Neo4jSettings] = None) -> None:
    """Start the MCP server on stdio (blocks)."""
    build_server(settings).run()
