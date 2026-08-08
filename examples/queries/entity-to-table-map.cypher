// ===========================================================================
// Question: "Where does the code meet the database?"
//
// Lists every JPA entity and the table it maps to, plus the file the entity is
// declared in so you can jump straight there. This is the single most useful
// artefact of `graphforge link`: one place to see the whole ORM boundary.
//
// MAPS_TO is an EXACT match between :Class:Entity.mappedTable (read from the
// @Table / @Entity annotation) and :Table.name — no heuristics, no guessing.
//
// Requires: `graphforge git` + `graphforge db` + `graphforge link`
// Reads:    (:Class:Entity)-[:MAPS_TO]->(:Table)
// ===========================================================================

MATCH (e:Entity)-[:MAPS_TO]->(t:Table)
OPTIONAL MATCH (f:File)-[:CONTAINS_CLASS]->(e)
RETURN e.fqn      AS entity,
       f.path     AS file,
       t.database AS database,
       t.schema   AS schema,
       t.name     AS table
ORDER BY table, entity;


// --- Variant: tables with no entity ---------------------------------------
// Reached by SQL only (or by a service graphforge has not ingested).
//
// MATCH (t:Table) WHERE NOT (t)<-[:MAPS_TO]-()
// RETURN t.database AS database, t.name AS table
// ORDER BY database, table;


// --- Variant: entities with no table --------------------------------------
// The annotation names a table that is not in any ingested database — a typo,
// a table dropped upstream, or simply a database you have not loaded yet.
//
// MATCH (e:Entity) WHERE e.mappedTable <> '' AND NOT (e)-[:MAPS_TO]->()
// RETURN e.fqn AS entity, e.mappedTable AS expectedTable, e.repo AS repo
// ORDER BY entity;


// --- Variant: full "who touches this table" report -------------------------
// Entity + views + stored procedures + the humans who last edited the entity.
// This is the query the README's "why a graph" pitch is built on.
//
// MATCH (t:Table {name: $table})
// OPTIONAL MATCH (t)<-[:MAPS_TO]-(e:Entity)<-[:CONTAINS_CLASS]-(f:File)
// OPTIONAL MATCH (t)<-[:BASED_ON]-(v:View)
// OPTIONAL MATCH (t)<-[:USES_TABLE]-(p:StoredProcedure)
// OPTIONAL MATCH (a:Author)-[:AUTHORED]->(c:Commit)-[:CHANGED]->(f)
// RETURN t.name AS table,
//        collect(DISTINCT e.fqn)  AS entities,
//        collect(DISTINCT v.name) AS views,
//        collect(DISTINCT p.name) AS procedures,
//        collect(DISTINCT a.name) AS recentEditors;
