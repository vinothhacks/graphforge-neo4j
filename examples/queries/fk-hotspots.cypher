// ===========================================================================
// Question: "Which tables is the schema actually built around?"
//
// Ranks tables by how many OTHER tables hold a foreign key pointing at them.
// These are the load-bearing walls: the tables you cannot rename, re-key or
// re-shard cheaply, and the ones whose migrations need the most rehearsal.
//
// The inverse (tables with zero incoming references) is just as useful — see
// the variant at the bottom for likely-orphaned tables.
//
// Requires: `graphforge db`
// Reads:    (:Table)-[:REFERENCES]->(:Table)
// ===========================================================================

MATCH (t:Table)<-[:REFERENCES]-(other:Table)
RETURN t.database AS database,
       t.schema   AS schema,
       t.name     AS table,
       count(DISTINCT other) AS incomingTables,
       collect(DISTINCT other.name)[0..10] AS referencedBy
ORDER BY incomingTables DESC
LIMIT 20;


// --- Variant: column-level detail ------------------------------------------
// Which exact columns the foreign keys land on — the ones whose data type you
// cannot widen without a coordinated migration.
//
// MATCH (child:Column)-[:FOREIGN_KEY]->(parent:Column)
// RETURN parent.database AS database, parent.table AS table, parent.name AS column,
//        count(DISTINCT child) AS referencingColumns,
//        collect(DISTINCT child.table + '.' + child.name)[0..10] AS referencedBy
// ORDER BY referencingColumns DESC LIMIT 20;


// --- Variant: probably-orphaned tables -------------------------------------
// No incoming FK, no outgoing FK, and (if you ran `graphforge link`) nothing in
// the code maps to it. Review candidates, not a delete list.
//
// MATCH (t:Table)
// WHERE NOT (t)<-[:REFERENCES]-() AND NOT (t)-[:REFERENCES]->()
//   AND NOT (t)<-[:MAPS_TO|USES_TABLE|BASED_ON]-()
// RETURN t.database AS database, t.schema AS schema, t.name AS table
// ORDER BY database, schema, table;


// --- Variant: hotspots weighted by size ------------------------------------
// Only meaningful after `graphforge db --sample-rows N`, which is what writes
// :Table.approxRows. Big AND heavily referenced is where migrations hurt.
//
// MATCH (t:Table)<-[:REFERENCES]-(other:Table)
// WHERE t.approxRows IS NOT NULL
// RETURN t.name AS table, t.approxRows AS approxRows,
//        count(DISTINCT other) AS incomingTables
// ORDER BY incomingTables DESC, approxRows DESC LIMIT 20;
