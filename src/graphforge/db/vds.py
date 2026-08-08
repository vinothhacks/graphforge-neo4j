"""Optional importer for a VDS (Virtual Data Service) / query-service catalog.

Some applications describe named data services in catalog tables: a service
catalog, a per-service query definition, and a where-field configuration. This
importer reads those three tables and turns each service into
``VDSService -HAS_QUERY-> VDSQuery`` and ``VDSService -HAS_WHERE_FIELD-> VDSWhereField``,
linking to existing :Table nodes via ``USES_TABLE`` / ``REFERENCES_TABLE`` (by name,
only where the Table already exists). All table and column names are configurable
via :class:`VdsConfig`, so the importer can target any similarly-shaped catalog.

Row-fetching (`ingest`) is separated from graph-mapping (`write_rows`) so the
mapping is unit-testable without a live database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core.cypher import NodeRef, Operation, merge_node, merge_rel
from ..core.neo4j_writer import Neo4jWriter, load_schema


@dataclass
class VdsConfig:
    service_table: str = "vdsservicecatalog"
    query_table: str = "querydetails"
    wherefield_table: str = "wherefieldconfig"
    sid: str = "sid"
    service_name: str = "servicename"
    query: str = "query"
    coretable: str = "coretable"
    groupby: str = "groupby"
    orderby: str = "orderby"
    tablename: str = "tablename"
    columnname: str = "columnname"
    fieldname: str = "fieldname"


def _db_id(engine: str, host: str, name: str) -> str:
    return f"{engine}://{host}/{name}"


class VdsIngestor:
    def __init__(self, writer: Neo4jWriter, config: Optional[VdsConfig] = None):
        self.writer = writer
        self.cfg = config or VdsConfig()

    def apply_schema(self) -> None:
        self.writer.apply_schema(load_schema("vds_schema.cypher"))

    # -- live fetch --------------------------------------------------------
    def _fetch(self, conn) -> List[dict]:
        c = self.cfg
        sql = (
            f"SELECT sc.{c.sid} AS sid, sc.{c.service_name} AS servicename, "
            f"qd.{c.query} AS query, qd.{c.coretable} AS coretable, "
            f"qd.{c.groupby} AS groupby, qd.{c.orderby} AS orderby, "
            f"wh.{c.tablename} AS tablename, wh.{c.columnname} AS columnname, "
            f"wh.{c.fieldname} AS fieldname "
            f"FROM {c.service_table} sc "
            f"LEFT JOIN {c.query_table} qd ON sc.{c.sid} = qd.{c.sid} "
            f"LEFT JOIN {c.wherefield_table} wh ON wh.{c.sid} = qd.{c.sid} "
            f"ORDER BY sc.{c.sid}"
        )
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def ingest(self, engine: str, host: str, port: int, user: str, password: str,
               database: str, driver: Optional[str] = None) -> Dict[str, int]:
        from . import get_extractor

        ex = get_extractor(engine, host=host, port=port, user=user,
                           password=password, driver=driver)
        conn = ex.connect(database)
        try:
            rows = self._fetch(conn)
        finally:
            conn.close()
        return self.write_rows(rows, engine, host, database)

    # -- pure mapping ------------------------------------------------------
    def write_rows(self, rows: List[dict], engine: str, host: str, database: str) -> Dict[str, int]:
        db_id = _db_id(engine, host, database)
        ops: List[Operation] = []
        services, queries, wheres = set(), set(), set()

        for r in rows:
            if r.get("sid") is None:
                continue
            sid = str(r["sid"])
            svc_id = f"vds://{db_id}/{sid}"
            if svc_id not in services:
                services.add(svc_id)
                ops.append(merge_node("VDSService", {"id": svc_id}, {
                    "sid": sid, "serviceName": r.get("servicename", ""), "database": database,
                }, comment=f"VDS service {sid}"))

            if r.get("query"):
                q_id = f"vdsq://{db_id}/{sid}"
                if q_id not in queries:
                    queries.add(q_id)
                    ops.append(merge_node("VDSQuery", {"id": q_id}, {
                        "sid": sid, "query": str(r["query"])[:10000],
                        "coreTable": r.get("coretable", ""),
                        "groupBy": r.get("groupby", ""), "orderBy": r.get("orderby", ""),
                        "database": database,
                    }))
                    ops.append(merge_rel(NodeRef("VDSService", {"id": svc_id}), "HAS_QUERY",
                                         NodeRef("VDSQuery", {"id": q_id})))
                    if r.get("coretable"):
                        ops.append(Operation(
                            "MATCH (q:VDSQuery {id: $q}) MATCH (t:Table) "
                            "WHERE t.database = $db AND toLower(t.name) = toLower($ct) "
                            "MERGE (q)-[:USES_TABLE]->(t)",
                            {"q": q_id, "db": database, "ct": r["coretable"]}))

            if r.get("tablename") and r.get("columnname"):
                wf_key = f"{sid}_{r.get('tablename')}_{r.get('columnname')}_{r.get('fieldname')}"
                wf_id = f"vdsw://{db_id}/{wf_key}"
                if wf_id not in wheres:
                    wheres.add(wf_id)
                    ops.append(merge_node("VDSWhereField", {"id": wf_id}, {
                        "sid": sid, "tableName": r.get("tablename", ""),
                        "columnName": r.get("columnname", ""),
                        "fieldName": r.get("fieldname", ""), "database": database,
                    }))
                    ops.append(merge_rel(NodeRef("VDSService", {"id": svc_id}), "HAS_WHERE_FIELD",
                                         NodeRef("VDSWhereField", {"id": wf_id})))
                    ops.append(Operation(
                        "MATCH (w:VDSWhereField {id: $w}) MATCH (t:Table) "
                        "WHERE t.database = $db AND toLower(t.name) = toLower($tn) "
                        "MERGE (w)-[:REFERENCES_TABLE]->(t)",
                        {"w": wf_id, "db": database, "tn": r["tablename"]}))

        self.writer.write(ops, desc="vds")
        return {"services": len(services), "queries": len(queries), "whereFields": len(wheres)}
