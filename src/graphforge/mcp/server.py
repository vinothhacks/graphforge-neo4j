"""Expose the forged knowledge graph over the Model Context Protocol (MCP).

Run `graphforge mcp` and any MCP client (Claude Desktop, other agents) can query
the graph with these tools:

* ``get_schema``             — labels, relationship types, and per-label counts
  (TTL-cached; see :class:`GraphQuery.get_schema`)
* ``read_cypher``            — run a read-only Cypher query (writes are rejected)
* ``search_nodes``           — substring search on a property of a given label (paged)
* ``node_neighbors``         — the immediate neighbourhood of a node id
* ``find_code``              — locate files / classes / methods by name (paged, case-insensitive)
* ``search_codebase``        — unified code / schema search (paged, case-insensitive)
* ``find_table``             — locate tables / columns by name (paged)
* ``find_procedure``         — locate stored procedures by name or body
* ``impact_of_column``       — raw blast radius of a column name
* ``explain_impact``         — impact report for a column *or* table, grouped into
  ``direct`` / ``transitive`` severity buckets, with a human-readable summary
* ``find_dead_code``         — stale + unreferenced file/class *candidates* in a repo
* ``blast_radius_of_file``   — classes a file declares, their methods, and callers

The paged tools (``search_nodes`` / ``find_code`` / ``find_table`` /
``search_codebase``) accept ``limit`` and ``offset`` and answer
``{rows, total, hasMore, limit, offset}``.

The graph-query logic lives in :class:`GraphQuery` (driver in, dicts out) so it
can be unit-tested without the MCP runtime. The ``mcp`` package is imported
lazily, only when the server is actually started.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from ..core.config import Neo4jSettings, load_settings

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: A LIMIT clause the user actually wrote, and a RETURN that can carry one.
#: Both are matched against masked text so a LIMIT inside a string literal
#: is treated as data, not as a clause.
_HAS_LIMIT = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_HAS_RETURN = re.compile(r"\bRETURN\b", re.IGNORECASE)

#: Paging with SKIP/LIMIT and no ORDER BY is unsound: Neo4j guarantees no order
#: between two queries, so the same row can appear on two pages while another is
#: never returned at all. Every paged query orders by the node's deterministic
#: id, which is unique and indexed, with elementId as a tiebreak for anything
#: that somehow lacks one. (`id()` would do, but is deprecated in Neo4j 5 and
#: warns on every query.)
_PAGE_ORDER = "ORDER BY n.id, elementId(n) "

#: Default lifetime of a cached ``get_schema`` snapshot, in seconds.
DEFAULT_SCHEMA_TTL = 60.0

#: Process-wide schema cache, shared by every :class:`GraphQuery` built through
#: :meth:`GraphQuery.connect` with the same connection settings. The dashboard
#: opens a fresh connection per request, so a purely per-instance cache would
#: never register a hit; the key is the connection, never the driver object.
_SCHEMA_CACHE: dict[tuple, tuple[float, dict]] = {}

#: Appended to every ``find_dead_code`` answer — the result is a lead, not a verdict.
DEAD_CODE_CAVEAT = (
    "Candidates only, never proof: reflection, dependency injection, "
    "configuration-driven wiring, tests, generated code and cross-repo callers are "
    "invisible to this query. Confirm by hand before deleting anything."
)


def clear_schema_cache() -> None:
    """Drop every cached schema snapshot (process-wide). Safe to call any time."""
    _SCHEMA_CACHE.clear()


def _copy_schema(schema: dict) -> dict:
    """Hand back a private copy so a caller mutating the result cannot poison the cache."""
    return {
        "labels": list(schema.get("labels") or []),
        "relationshipTypes": list(schema.get("relationshipTypes") or []),
        "nodeCountsByLabel": dict(schema.get("nodeCountsByLabel") or {}),
        "relationshipCountsByType": dict(schema.get("relationshipCountsByType") or {}),
    }


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _page(rows: list[dict], total: int, limit: int, offset: int) -> dict:
    """Wrap a slice of rows with the metadata a paging client needs."""
    return {"rows": rows, "total": int(total), "limit": int(limit), "offset": int(offset),
            "hasMore": (int(offset) + len(rows)) < int(total)}


def _clamp_limit(limit: int) -> int:
    try:
        return max(1, int(limit))
    except (TypeError, ValueError):
        return 25


def _clamp_offset(offset: int) -> int:
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def _clean_rows(rows: list[dict] | None, *keys: str) -> list[dict]:
    """Drop the all-null placeholder maps Cypher's ``collect()`` yields on OPTIONAL MATCH."""
    out = []
    for row in rows or []:
        if isinstance(row, dict) and any(row.get(k) not in (None, "") for k in (keys or tuple(row))):
            out.append(row)
    return out


class GraphQuery:
    """Thin, read-focused query helper over a Neo4j driver."""

    def __init__(self, driver, database: str = "neo4j", *,
                 schema_ttl: float = DEFAULT_SCHEMA_TTL,
                 clock: Callable[[], float] | None = None,
                 cache_key: tuple | None = None):
        self.driver = driver
        self.database = database
        #: Default staleness window for :meth:`get_schema`, in seconds.
        self.schema_ttl = float(schema_ttl)
        #: Monotonic clock; injectable so cache expiry is testable without sleeping.
        self._clock = clock or time.monotonic
        #: ``None`` → this instance keeps a private cache (tests, ad-hoc use).
        #: A tuple → the shared process-wide cache under that connection key.
        self._cache_key = cache_key
        self._local_cache: dict[tuple, tuple[float, dict]] = {}

    @classmethod
    def connect(cls, settings: Neo4jSettings) -> GraphQuery:
        settings.check_connectable()
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password))
        driver.verify_connectivity()
        return cls(driver, settings.database,
                   cache_key=("neo4j", settings.uri, settings.user, settings.database))

    def close(self) -> None:
        self.driver.close()

    # -- primitives --------------------------------------------------------
    def _read(self, cypher: str, params: dict[str, Any] | None = None,
              timeout: float | None = None) -> list[dict]:
        def _work(tx):
            result = tx.run(cypher, params or {})
            return [r.data() for r in result]

        if timeout is not None:
            from neo4j import unit_of_work
            _work = unit_of_work(timeout=timeout)(_work)

        with self.driver.session(database=self.database) as session:
            return session.execute_read(_work)

    def _count(self, cypher: str, params: dict[str, Any] | None = None) -> int:
        rows = self._read(cypher, params)
        if not rows:
            return 0
        value = rows[0].get("total", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # -- schema cache ------------------------------------------------------
    @property
    def _cache(self) -> dict[tuple, tuple[float, dict]]:
        return _SCHEMA_CACHE if self._cache_key is not None else self._local_cache

    @property
    def _schema_cache_key(self) -> tuple:
        return self._cache_key or ("local", self.database)

    def invalidate_schema_cache(self) -> None:
        """Forget this connection's cached schema; the next read hits Neo4j."""
        self._cache.pop(self._schema_cache_key, None)

    def get_schema(self, ttl: float | None = None, *, refresh: bool = False) -> dict:
        """Return graph labels, relationship types, and node counts per label.

        The answer costs ``2 + len(labels)`` round trips, and the dashboard asks
        for it on every load, so it is cached for ``ttl`` seconds (default
        :data:`DEFAULT_SCHEMA_TTL`, 60s).

        **Staleness window.** A cached answer can be up to ``ttl`` seconds behind
        the graph: labels added or nodes written by a concurrent ingest are
        invisible until the entry expires. The cache is never invalidated by
        graph activity — nothing here watches Neo4j. Callers that must see the
        current graph pass ``refresh=True`` (re-reads and re-primes) or
        ``ttl=0`` (bypasses the cache entirely and stores nothing);
        :meth:`invalidate_schema_cache` and :func:`clear_schema_cache` drop
        entries explicitly, e.g. right after an ingest.
        """
        ttl = self.schema_ttl if ttl is None else float(ttl)
        cache, key = self._cache, self._schema_cache_key
        if refresh or ttl <= 0:
            cache.pop(key, None)
        else:
            hit = cache.get(key)
            if hit is not None and (self._clock() - hit[0]) < ttl:
                return _copy_schema(hit[1])
        schema = self._load_schema()
        if ttl > 0:
            cache[key] = (self._clock(), _copy_schema(schema))
        return schema

    def _load_schema(self) -> dict:
        labels = [r["label"] for r in self._read("CALL db.labels() YIELD label RETURN label")]
        rels = [r["relationshipType"] for r in
                self._read("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
        counts = {}
        for label in labels:
            rec = self._read(f"MATCH (n:`{label}`) RETURN count(n) AS c")
            counts[label] = rec[0]["c"] if rec else 0
        # One query per type rather than a single grouped scan: this form reads
        # the relationship count store and is constant-time, where
        # `MATCH ()-[r]->() RETURN type(r), count(*)` walks every relationship.
        rel_counts = {}
        for rel in rels:
            rec = self._read(f"MATCH ()-[r:`{rel}`]->() RETURN count(r) AS c")
            rel_counts[rel] = rec[0]["c"] if rec else 0
        return {"labels": labels, "relationshipTypes": rels, "nodeCountsByLabel": counts,
                "relationshipCountsByType": rel_counts}

    # -- tools -------------------------------------------------------------
    def read_cypher(self, query: str, params: dict[str, Any] | None = None, limit: int = 200) -> list[dict]:
        from ..query_guard import (
            READ_TX_TIMEOUT_SECONDS,
            check_read_query,
            clamp_read_limit,
            mask_query,
        )

        reason = check_read_query(query, limit)
        if reason:
            raise ValueError(reason)
        limit = clamp_read_limit(limit)
        # Decide on masked text. Matching the raw query got two things wrong: a
        # newline before LIMIT is formatting, not absence of a cap (so multi-line
        # Cypher had a second LIMIT appended -- a syntax error), and a " LIMIT "
        # inside a string literal is data, not a clause (so the cap was skipped).
        masked = mask_query(query)
        if _HAS_RETURN.search(masked) and not _HAS_LIMIT.search(masked):
            query = query.rstrip("; \n") + f"\nLIMIT {int(limit)}"
        rows = self._read(query, params, timeout=READ_TX_TIMEOUT_SECONDS)
        # Backstop for shapes that cannot carry a LIMIT clause: a standalone
        # CALL is a syntax error with one appended, so it is capped here.
        return rows[:limit]

    def search_nodes(self, label: str, prop: str, value: str, limit: int = 25,
                     offset: int = 0) -> list[dict]:
        """Substring-search one property of one label. Returns a plain list of rows."""
        if not _IDENT.fullmatch(label) or not _IDENT.fullmatch(prop):
            raise ValueError("label and prop must be simple identifiers")
        cypher = (
            f"MATCH (n:`{label}`) WHERE toString(n.`{prop}`) CONTAINS $value "
            "RETURN n " + _PAGE_ORDER + "SKIP $offset LIMIT $limit"
        )
        return self._read(cypher, {"value": value, "limit": _clamp_limit(limit),
                                   "offset": _clamp_offset(offset)})

    def search_nodes_page(self, label: str, prop: str, value: str, limit: int = 25,
                          offset: int = 0) -> dict:
        """:meth:`search_nodes` plus ``total`` / ``hasMore`` (one extra count query)."""
        if not _IDENT.fullmatch(label) or not _IDENT.fullmatch(prop):
            raise ValueError("label and prop must be simple identifiers")
        limit, offset = _clamp_limit(limit), _clamp_offset(offset)
        rows = self.search_nodes(label, prop, value, limit, offset)
        total = self._count(
            f"MATCH (n:`{label}`) WHERE toString(n.`{prop}`) CONTAINS $value "
            "RETURN count(n) AS total", {"value": value})
        return _page(rows, total, limit, offset)

    def node_neighbors(self, node_id: str, limit: int = 50) -> list[dict]:
        cypher = (
            "MATCH (n {id: $id})-[r]-(m) "
            "RETURN type(r) AS rel, startNode(r).id AS fromId, m.id AS neighborId, "
            "labels(m) AS neighborLabels LIMIT $limit"
        )
        return self._read(cypher, {"id": node_id, "limit": int(limit)})

    # Case-insensitive substring match on the properties a human would type.
    _CODE_PRED = (
        "(n:File OR n:Class OR n:Method) AND "
        "(toLower(toString(coalesce(n.name, ''))) CONTAINS toLower($t) OR "
        "toLower(toString(coalesce(n.fqn, ''))) CONTAINS toLower($t) OR "
        "toLower(toString(coalesce(n.path, ''))) CONTAINS toLower($t))"
    )
    _SCHEMA_PRED = (
        "(n:Table OR n:Column OR n:StoredProcedure) AND "
        "(toLower(toString(coalesce(n.name, ''))) CONTAINS toLower($t) OR "
        "toLower(toString(coalesce(n.definition, ''))) CONTAINS toLower($t))"
    )
    _FIND_CODE_WHERE = "MATCH (n) WHERE " + _CODE_PRED + " "
    _FIND_SCHEMA_WHERE = "MATCH (n) WHERE " + _SCHEMA_PRED + " "
    _SEARCH_KINDS = frozenset({"code", "schema", "all"})
    #: `ref` is what tells two same-named hits apart. A Method carries neither
    #: `fqn` nor `path` -- its qualifier is `owner`, the declaring class -- and a
    #: free function has an empty `owner`, which coalesce does not skip. Without
    #: the full chain every `apply_schema`, and then every module-level `main`,
    #: rendered as the same row. `n.id` is the last resort and is always unique.
    _SEARCH_RETURN: ClassVar[dict[str, str]] = {
        "code": (
            "RETURN labels(n) AS labels, n.name AS name, "
            "coalesce(n.fqn, n.path, CASE WHEN coalesce(n.owner, '') <> '' THEN n.owner END, n.id) AS ref, "
            "n.repo AS repo "
        ),
        "schema": (
            "RETURN labels(n) AS labels, n.name AS name, "
            "n.database AS database, n.table AS table "
        ),
        "all": (
            "RETURN labels(n) AS labels, n.name AS name, "
            "coalesce(n.fqn, n.path, CASE WHEN coalesce(n.owner, '') <> '' THEN n.owner END, n.id) AS ref, "
            "n.repo AS repo, "
            "n.database AS database, n.table AS table "
        ),
    }

    def _search_where(self, kind: str, repo: str) -> str:
        if kind == "schema":
            where = self._FIND_SCHEMA_WHERE
        elif kind == "all":
            where = f"MATCH (n) WHERE (({self._CODE_PRED}) OR ({self._SCHEMA_PRED})) "
        else:
            where = self._FIND_CODE_WHERE
        if repo:
            where += "AND n.repo = $repo "
        return where

    def search_codebase(self, text: str, kind: str = "code", repo: str = "",
                        limit: int = 25, offset: int = 0) -> dict:
        """Case-insensitive codebase / schema search, paged.

        ``kind`` is ``code`` (File / Class / Method), ``schema`` (Table / Column /
        StoredProcedure), or ``all``. Optional ``repo`` filters ``n.repo``.
        """
        kind = (kind or "code").strip().lower()
        if kind not in self._SEARCH_KINDS:
            allowed = ", ".join(sorted(self._SEARCH_KINDS))
            raise ValueError(f"kind must be one of {allowed}, not {kind!r}")
        repo = (repo or "").strip()
        limit, offset = _clamp_limit(limit), _clamp_offset(offset)
        where = self._search_where(kind, repo)
        params: dict[str, Any] = {"t": text, "limit": limit, "offset": offset}
        if repo:
            params["repo"] = repo
        rows = self._read(
            where + self._SEARCH_RETURN[kind] + _PAGE_ORDER + "SKIP $offset LIMIT $limit",
            params)
        count_params: dict[str, Any] = {"t": text}
        if repo:
            count_params["repo"] = repo
        total = self._count(where + "RETURN count(n) AS total", count_params)
        return _page(rows, total, limit, offset)

    def find_code(self, text: str, limit: int = 25, offset: int = 0) -> list[dict]:
        """Locate File / Class / Method nodes. Thin wrapper over :meth:`search_codebase`."""
        return self.search_codebase(text, kind="code", limit=limit, offset=offset)["rows"]

    def find_code_page(self, text: str, limit: int = 25, offset: int = 0) -> dict:
        """:meth:`find_code` plus ``total`` / ``hasMore``."""
        return self.search_codebase(text, kind="code", limit=limit, offset=offset)

    _FIND_TABLE_WHERE = "MATCH (n) WHERE (n:Table OR n:Column) AND toString(n.name) CONTAINS $t "

    def find_table(self, text: str, limit: int = 25, offset: int = 0) -> list[dict]:
        cypher = (
            self._FIND_TABLE_WHERE +
            "RETURN labels(n) AS labels, n.name AS name, n.table AS table, n.database AS database "
            + _PAGE_ORDER + "SKIP $offset LIMIT $limit"
        )
        return self._read(cypher, {"t": text, "limit": _clamp_limit(limit),
                                   "offset": _clamp_offset(offset)})

    def find_table_page(self, text: str, limit: int = 25, offset: int = 0) -> dict:
        """:meth:`find_table` plus ``total`` / ``hasMore``."""
        limit, offset = _clamp_limit(limit), _clamp_offset(offset)
        rows = self.find_table(text, limit, offset)
        total = self._count(self._FIND_TABLE_WHERE + "RETURN count(n) AS total", {"t": text})
        return _page(rows, total, limit, offset)

    def find_procedure(self, text: str, limit: int = 25) -> list[dict]:
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

    # -- higher-level reports ---------------------------------------------
    def _impact_of_table(self, table: str) -> dict:
        """The :meth:`impact_of_column` shape, scoped to one table instead of a column name."""
        cols = self._read(
            "MATCH (c:Column) WHERE toLower(c.table) = toLower($t) "
            "RETURN c.database AS database, c.table AS table, c.name AS column, "
            "c.dataType AS dataType ORDER BY c.database, c.name", {"t": table})
        fks = self._read(
            "MATCH (c1:Column)-[:FOREIGN_KEY]->(c2:Column) "
            "WHERE toLower(c1.table) = toLower($t) OR toLower(c2.table) = toLower($t) "
            "RETURN c1.table AS fromTable, c1.name AS fromColumn, "
            "c2.table AS toTable, c2.name AS toColumn", {"t": table})
        indexes = self._read(
            "MATCH (i:Index) WHERE toLower(i.table) = toLower($t) "
            "RETURN i.database AS database, i.table AS table, i.name AS name, "
            "i.columns AS columns, i.isUnique AS isUnique", {"t": table})
        procs = self._read(
            "MATCH (p:StoredProcedure) WHERE p.definition IS NOT NULL "
            "AND toLower(p.definition) CONTAINS toLower($t) "
            "RETURN p.database AS database, p.name AS procedure "
            "ORDER BY p.database, p.name", {"t": table})
        views = self._read(
            "MATCH (v:View) WHERE v.definition IS NOT NULL "
            "AND toLower(v.definition) CONTAINS toLower($t) "
            "RETURN v.database AS database, v.name AS view", {"t": table})
        return {"columns": cols, "foreignKeys": fks, "indexes": indexes,
                "storedProcedures": procs, "views": views}

    def explain_impact(self, target: str, kind: str = "auto") -> dict:
        """Readable impact report for a column *or* table name.

        Builds on :meth:`impact_of_column` (or its table-scoped twin) and adds the
        two things a reviewer actually asks for next: the views / procedures that
        reach the affected tables through the link passes, and the JPA entities
        mapped onto them (``:Class.mappedTable``, or ``(:Class)-[:MAPS_TO]->(:Table)``).

        ``kind`` is ``"auto"`` (a table of that exact name wins, else it is read as
        a column), ``"column"`` or ``"table"``.

        Findings are bucketed by severity:

        * ``direct`` — the columns themselves, the tables holding them, the
          foreign keys on either endpoint, and the indexes built over them.
          Changing the column breaks these by definition.
        * ``transitive`` — views, stored procedures and entities that merely
          *reference* the name or map to the table. These need review, not
          necessarily surgery; the text matches in particular can be false
          positives (a comment, a similarly-named local alias).
        """
        kind = (kind or "auto").strip().lower()
        if kind not in ("auto", "column", "table"):
            raise ValueError("kind must be one of 'auto', 'column' or 'table'")

        tables = self._read(
            "MATCH (t:Table) WHERE toLower(t.name) = toLower($target) "
            "RETURN t.database AS database, t.schema AS schema, t.name AS name "
            "ORDER BY t.database, t.schema", {"target": target})
        if kind == "auto":
            kind = "table" if tables else "column"

        base = self._impact_of_table(target) if kind == "table" else self.impact_of_column(target)

        # every table the finding touches, lower-cased for case-insensitive joins
        names = {str(r["name"]).lower() for r in tables if r.get("name")}
        names.update(str(r["table"]).lower() for r in base["columns"] if r.get("table"))
        if kind == "table":
            names.add(target.strip().lower())
        table_names = sorted(n for n in names if n)  # never join on '' — it would match everything

        linked: list[dict] = []
        entities: list[dict] = []
        if table_names:
            linked = self._read(
                "MATCH (v:View)-[:BASED_ON|CROSS_DB_REFERENCE]->(t:Table) "
                "WHERE toLower(t.name) IN $tables "
                "RETURN 'View' AS kind, v.database AS database, v.name AS name, t.name AS table "
                "UNION "
                "MATCH (p:StoredProcedure)-[:USES_TABLE]->(t:Table) "
                "WHERE toLower(t.name) IN $tables "
                "RETURN 'StoredProcedure' AS kind, p.database AS database, p.name AS name, "
                "t.name AS table", {"tables": table_names})
            entities = self._read(
                "MATCH (e:Class) WHERE toLower(coalesce(e.mappedTable, '')) IN $tables "
                "RETURN e.repo AS repo, e.fqn AS entity, e.name AS name, "
                "e.mappedTable AS table, 'mappedTable' AS via "
                "UNION "
                "MATCH (e:Class)-[:MAPS_TO]->(t:Table) WHERE toLower(t.name) IN $tables "
                "RETURN e.repo AS repo, e.fqn AS entity, e.name AS name, "
                "t.name AS table, 'MAPS_TO' AS via", {"tables": table_names})

        views = _merge_evidence(base["views"], "view", linked, "View")
        procs = _merge_evidence(base["storedProcedures"], "procedure", linked, "StoredProcedure")

        direct = {"tables": tables, "columns": base["columns"],
                  "foreignKeys": base["foreignKeys"], "indexes": base["indexes"]}
        transitive = {"views": views, "storedProcedures": procs, "entities": entities}
        counts = {key: len(rows) for key, rows in
                  list(direct.items()) + list(transitive.items())}
        counts["direct"] = sum(len(rows) for rows in direct.values())
        counts["transitive"] = sum(len(rows) for rows in transitive.values())
        return {
            "target": target, "kind": kind, "tables": table_names,
            "direct": direct, "transitive": transitive, "counts": counts,
            "summary": _impact_summary(target, kind, table_names, counts),
        }

    def find_dead_code(self, repo: str, days: int = 180, limit: int = 200) -> dict:
        """Files and classes in `repo` that look unused: **candidates, not proof**.

        A candidate is a file (or class) that satisfies *both* tests:

        1. no ``:Commit`` has ``CHANGED`` it within the last `days` days — including
           files no ingested commit ever touched, which report ``lastChanged: null``;
        2. nothing else in the same repo points at it — no ``:Class`` living in a
           *different* file of this repo ``IMPORTS`` a class the file declares.
           Import edges land on the external stub node for an fqn, so the match is
           made on ``fqn``, which bridges the stub and the declaring class.

        This proves *absence of evidence*, never absence of use. Reflection,
        dependency injection, Spring/CDI wiring by name, service loaders, dynamic
        imports, template and configuration references, entry points invoked by a
        framework, callers in other repos, and anything outside the ingested commit
        window all keep code alive while staying invisible here. It is also blind to
        call graphs: a class used *without* being imported (same package in Java,
        wildcard imports) looks dead. Treat the output as a review queue.
        """
        days = max(0, int(days))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        params = {"repo": repo, "cutoff": cutoff, "limit": _clamp_limit(limit)}

        files = self._read(
            "MATCH (f:File) WHERE f.repo = $repo "
            "OPTIONAL MATCH (commit:Commit)-[:CHANGED]->(f) "
            "WITH f, max(substring(coalesce(commit.authoredAt, commit.committedAt, ''), 0, 10)) "
            "AS lastChanged "
            "WHERE lastChanged IS NULL OR lastChanged = '' OR lastChanged < $cutoff "
            "OPTIONAL MATCH (f)-[:CONTAINS_CLASS]->(cls:Class) "
            "OPTIONAL MATCH (importerFile:File)-[:CONTAINS_CLASS]->(:Class)-[:IMPORTS]->(ref:Class) "
            "WHERE ref.fqn = cls.fqn AND importerFile.repo = $repo AND importerFile <> f "
            "WITH f, lastChanged, collect(DISTINCT cls.fqn) AS classes, "
            "count(DISTINCT importerFile) AS importers "
            "WHERE importers = 0 "
            "RETURN f.path AS path, f.repo AS repo, lastChanged, classes "
            "ORDER BY lastChanged, path LIMIT $limit", params)

        classes = self._read(
            "MATCH (f:File)-[:CONTAINS_CLASS]->(cls:Class) "
            "WHERE f.repo = $repo AND coalesce(cls.external, false) = false "
            "OPTIONAL MATCH (commit:Commit)-[:CHANGED]->(f) "
            "WITH f, cls, max(substring(coalesce(commit.authoredAt, commit.committedAt, ''), 0, 10)) "
            "AS lastChanged "
            "WHERE lastChanged IS NULL OR lastChanged = '' OR lastChanged < $cutoff "
            "OPTIONAL MATCH (importerFile:File)-[:CONTAINS_CLASS]->(:Class)-[:IMPORTS]->(ref:Class) "
            "WHERE ref.fqn = cls.fqn AND importerFile.repo = $repo AND importerFile <> f "
            "WITH f, cls, lastChanged, count(DISTINCT importerFile) AS importers "
            "WHERE importers = 0 "
            "RETURN cls.fqn AS fqn, cls.name AS name, f.path AS path, "
            "coalesce(cls.repo, f.repo) AS repo, lastChanged, cls.language AS language "
            "ORDER BY lastChanged, fqn LIMIT $limit", params)

        files = [_normalise_last_changed(r) for r in files]
        classes = [_normalise_last_changed(r) for r in classes]
        counts = {"files": len(files), "classes": len(classes)}
        summary = (
            f"{repo}: {_plural(counts['files'], 'file')} and "
            f"{_plural(counts['classes'], 'class', 'classes')} untouched since {cutoff} "
            f"({days}d) with no in-repo importer — candidates for review, not deletion."
        )
        return {"repo": repo, "days": days, "cutoff": cutoff, "files": files,
                "classes": classes, "counts": counts, "summary": summary,
                "caveat": DEAD_CODE_CAVEAT}

    def blast_radius_of_file(self, path: str) -> dict:
        """What breaks if `path` changes: its classes, their methods, and their callers.

        One traversal: file → declared classes → methods, and back up from every
        class that ``IMPORTS`` one of them to the file declaring it. Import edges
        land on the external stub node for an fqn, so callers are matched by
        ``fqn`` rather than by node identity. Free functions attached straight to
        the file (``CONTAINS_METHOD``, used by the Go/Python parsers) are not
        listed — only methods owned by a declared class.
        """
        rows = self._read(
            "MATCH (f:File) WHERE f.path = $path OR f.id = $path "
            "OPTIONAL MATCH (f)-[:CONTAINS_CLASS]->(cls:Class) "
            "OPTIONAL MATCH (cls)-[:HAS_METHOD]->(m:Method) "
            "OPTIONAL MATCH (callerFile:File)-[:CONTAINS_CLASS]->(caller:Class)-[:IMPORTS]->(ref:Class) "
            "WHERE ref.fqn = cls.fqn AND callerFile <> f "
            "RETURN f.path AS file, f.repo AS repo, cls.fqn AS class, cls.name AS className, "
            "cls.language AS language, "
            "collect(DISTINCT {name: m.name, visibility: m.visibility, line: m.lineNumber}) AS methods, "
            "collect(DISTINCT {file: callerFile.path, class: caller.fqn, repo: caller.repo}) "
            "AS callers "
            "ORDER BY class", {"path": path})

        classes: list[dict] = []
        caller_files: dict[str, dict] = {}
        method_count = 0
        for row in rows:
            if not row.get("class"):
                continue  # a file with no declared classes still returns one null row
            methods = _clean_rows(row.get("methods"), "name")
            callers = _clean_rows(row.get("callers"), "file", "class")
            method_count += len(methods)
            for caller in callers:
                key = str(caller.get("file") or caller.get("class"))
                caller_files.setdefault(key, {"file": caller.get("file"),
                                              "repo": caller.get("repo"), "classes": []})
                if caller.get("class") and caller["class"] not in caller_files[key]["classes"]:
                    caller_files[key]["classes"].append(caller["class"])
            classes.append({"class": row["class"], "name": row.get("className"),
                            "language": row.get("language"), "methods": methods,
                            "callers": callers})

        found = bool(rows)
        repo = rows[0].get("repo") if rows else None
        counts = {"classes": len(classes), "methods": method_count,
                  "callerFiles": len(caller_files)}
        if not found:
            summary = f"No :File node matches {path!r} — check the path (it is stored repo-relative)."
        else:
            summary = (
                f"{path}: {_plural(counts['classes'], 'class', 'classes')} declaring "
                f"{_plural(counts['methods'], 'method')}, referenced by "
                f"{_plural(counts['callerFiles'], 'file')}."
            )
        return {"file": path, "found": found, "repo": repo, "classes": classes,
                "callers": sorted(caller_files.values(), key=lambda c: str(c.get("file") or "")),
                "counts": counts, "summary": summary}


def _normalise_last_changed(row: dict) -> dict:
    """Cypher's ``max()`` over a commit-less file yields ``''``; report that as null."""
    out = dict(row)
    if not out.get("lastChanged"):
        out["lastChanged"] = None
    return out


def _merge_evidence(text_rows: list[dict], name_key: str,
                    linked: list[dict], kind: str) -> list[dict]:
    """Fold text matches and link-pass edges into one list, recording how each was found."""
    merged: dict[tuple, dict] = {}
    for row in text_rows or []:
        key = (row.get("database"), row.get(name_key))
        merged[key] = {"database": row.get("database"), name_key: row.get(name_key),
                       "via": ["definition"]}
    for row in linked or []:
        if row.get("kind") != kind:
            continue
        key = (row.get("database"), row.get("name"))
        entry = merged.setdefault(key, {"database": row.get("database"),
                                        name_key: row.get("name"), "via": []})
        if "link" not in entry["via"]:
            entry["via"].append("link")
    return sorted(merged.values(), key=lambda r: (str(r.get("database") or ""), str(r.get(name_key) or "")))


def _impact_summary(target: str, kind: str, tables: list[str], counts: dict[str, int]) -> str:
    where = f" across {_plural(len(tables), 'table')}" if tables else ""
    direct = ", ".join([
        _plural(counts.get("columns", 0), "column"),
        _plural(counts.get("foreignKeys", 0), "foreign key"),
        _plural(counts.get("indexes", 0), "index", "indexes"),
    ])
    transitive = ", ".join([
        _plural(counts.get("views", 0), "view"),
        _plural(counts.get("storedProcedures", 0), "stored procedure"),
        _plural(counts.get("entities", 0), "JPA entity", "JPA entities"),
    ])
    return (f"{target} ({kind}){where}: direct impact — {direct}; "
            f"transitive impact — {transitive}. Transitive hits found by text match "
            f"need review; they can be false positives.")


class LazyGraph:
    """A :class:`GraphQuery` that connects on first use rather than at startup.

    Connecting eagerly meant an unreachable database killed the server process
    before it ever spoke MCP, and the client showed only "server exited" with no
    hint as to why. Deferring it means the client always connects and always
    lists the tools; an unreachable graph is answered as a readable error to
    whoever asked. Because a failed attempt leaves the connection unset, simply
    starting Neo4j is enough -- no client restart.
    """

    def __init__(self, settings: Neo4jSettings):
        # Import the driver now; connect later. These two halves look alike and
        # are not: the import is cheap and cannot fail for any reason the user
        # can act on, while the connection is slow and fails whenever the
        # database is down. Only the second is worth deferring.
        #
        # Deferring both deadlocks. The first `import neo4j` executed from
        # inside a *running* MCP server never returns -- the tool call hangs
        # forever rather than erroring, which is worse than the eager connect
        # this replaced. Reproduced on Windows/CPython 3.11 with mcp 1.x; moving
        # the connect to a worker thread does not help, and pre-importing here
        # fixes it completely.
        import neo4j  # noqa: F401  — see above; must not be moved into connect()

        self._settings = settings
        self._graph: GraphQuery | None = None

    def connect(self) -> GraphQuery:
        if self._graph is None:
            from ..core.errors import neo4j_advice
            try:
                self._graph = GraphQuery.connect(self._settings)
            except Exception as exc:
                advice = neo4j_advice(exc, self._settings)
                if advice is None:
                    raise
                raise RuntimeError(advice) from exc
        return self._graph

    def close(self) -> None:
        if self._graph is not None:
            self._graph.close()
            self._graph = None

    def __getattr__(self, name: str):
        # Only reached for names that are not real attributes, i.e. the query
        # methods the tools call. Connecting here is what makes it lazy.
        return getattr(self.connect(), name)


def build_server(settings: Neo4jSettings | None = None, graph: Any = None):
    """Construct a FastMCP server with graph tools bound to a lazy connection.

    ``graph`` injects a ready-made query object instead of connecting, mirroring
    the ``connect=`` seam :func:`graphforge.ui.server.route` already uses, so the
    tools can be exercised without a database or the MCP runtime.
    """
    from mcp.server.fastmcp import FastMCP  # lazy: only needed to actually serve

    settings = settings or load_settings().neo4j
    gq = graph if graph is not None else LazyGraph(settings)
    server = FastMCP("graphforge")

    def _json(obj: Any) -> str:
        return json.dumps(obj, indent=2, default=str)

    @server.tool()
    def get_schema(ttl: float | None = None, refresh: bool = False) -> str:
        """Return graph labels, relationship types, and node counts per label.

        Cached for `ttl` seconds (default 60), so the answer can be up to that far
        behind a concurrent ingest. Pass refresh=true to force a fresh read, or
        ttl=0 to bypass the cache entirely.
        """
        return _json(gq.get_schema(ttl, refresh=refresh))

    @server.tool()
    def read_cypher(query: str, limit: int = 200) -> str:
        """Run a READ-ONLY Cypher query against the knowledge graph and return rows as JSON.

        Writes, LOAD CSV, USE, SHOW, multi-statement queries, and procedures
        outside the allowlist (db.labels, db.relationshipTypes, db.propertyKeys)
        are rejected before a transaction is opened.
        """
        return _json(gq.read_cypher(query, limit=limit))

    @server.tool()
    def search_nodes(label: str, prop: str, value: str, limit: int = 25, offset: int = 0) -> str:
        """Substring-search nodes of `label` where property `prop` contains `value`.

        Paged: returns {rows, total, hasMore, limit, offset}; ask for the next page
        with offset = offset + limit while hasMore is true.
        """
        return _json(gq.search_nodes_page(label, prop, value, limit, offset))

    @server.tool()
    def node_neighbors(node_id: str, limit: int = 50) -> str:
        """Return the immediate neighbours of the node with the given `id`."""
        return _json(gq.node_neighbors(node_id, limit))

    @server.tool()
    def find_code(text: str, limit: int = 25, offset: int = 0) -> str:
        """Find File / Class / Method nodes whose name, fqn, or path contains `text`.

        Case-insensitive. Paged: returns {rows, total, hasMore, limit, offset}.
        """
        return _json(gq.find_code_page(text, limit, offset))

    @server.tool()
    def search_codebase(text: str, kind: str = "code", repo: str = "",
                        limit: int = 25, offset: int = 0) -> str:
        """Search the graph for code and/or schema nodes matching `text`.

        Case-insensitive. `kind` is 'code' (File / Class / Method; the default),
        'schema' (Table / Column / StoredProcedure), or 'all'. Optional `repo`
        limits code hits to one repository name. Paged: returns
        {rows, total, hasMore, limit, offset}.
        """
        return _json(gq.search_codebase(text, kind, repo, limit, offset))

    @server.tool()
    def find_table(text: str, limit: int = 25, offset: int = 0) -> str:
        """Find Table / Column nodes whose name contains `text`.

        Paged: returns {rows, total, hasMore, limit, offset}.
        """
        return _json(gq.find_table_page(text, limit, offset))

    @server.tool()
    def find_procedure(text: str, limit: int = 25) -> str:
        """Find StoredProcedure nodes whose name or SQL body contains `text`."""
        return _json(gq.find_procedure(text, limit))

    @server.tool()
    def impact_of_column(column: str) -> str:
        """Blast radius of a column change: matching columns across schemas, foreign
        keys, indexes, and stored procedures / views whose body references it."""
        return _json(gq.impact_of_column(column))

    @server.tool()
    def explain_impact(target: str, kind: str = "auto") -> str:
        """Impact report for a column or table name, grouped by severity.

        `direct` holds the tables, columns, foreign keys and indexes that break by
        definition; `transitive` holds the views, stored procedures and JPA entities
        that merely reference them and need review. `kind` is 'auto', 'column' or
        'table'. Also returns a one-line `summary`.
        """
        return _json(gq.explain_impact(target, kind))

    @server.tool()
    def find_dead_code(repo: str, days: int = 180, limit: int = 200) -> str:
        """Files/classes in `repo` with no commit in `days` days and no in-repo importer.

        CANDIDATES ONLY — this is absence of evidence, not proof. Reflection,
        dependency injection, configuration-driven wiring, framework entry points
        and callers in other repos are all invisible here. Review before deleting.
        """
        return _json(gq.find_dead_code(repo, days, limit))

    @server.tool()
    def blast_radius_of_file(path: str) -> str:
        """What a change to `path` can reach: the classes it declares, their methods,
        and the files whose classes import them."""
        return _json(gq.blast_radius_of_file(path))

    return server


def run(settings: Neo4jSettings | None = None) -> None:
    """Start the MCP server on stdio (blocks)."""
    build_server(settings).run()
