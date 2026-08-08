// ===========================================================================
// Question: "What breaks if I change this column?"
//
// One traversal that collects everything downstream of a column name: the
// tables that declare it, its foreign-key partners, the indexes that include
// it, and the views / stored procedures wired to its table by `graphforge
// link`. The classic use is sizing a "widen this column" or "rename this
// column" migration before you write it.
//
// Matching is by column NAME (case-insensitively), so the same logical column
// is caught across every schema and database you have ingested — which is
// usually the point, and occasionally a false positive (`id`, `name`, `status`
// live everywhere). Narrow with the $table variant at the bottom.
//
// Parameters: $column   e.g. :param column => 'customer_id'
// Requires:   `graphforge db`; the views/procedures column needs `graphforge link`
// The MCP tools `impact_of_column` and `explain_impact` wrap this with a
// severity split and a plain-English summary.
// ===========================================================================

MATCH (c:Column)
WHERE toLower(c.name) = toLower($column)
OPTIONAL MATCH (c)-[:FOREIGN_KEY]-(fk:Column)
OPTIONAL MATCH (t:Table)-[:HAS_COLUMN]->(c)
OPTIONAL MATCH (t)-[:HAS_INDEX]->(ix:Index) WHERE c.name IN ix.columns
OPTIONAL MATCH (t)<-[:USES_TABLE|BASED_ON]-(user)
RETURN t.database AS database,
       t.schema   AS schema,
       t.name     AS table,
       c.dataType AS dataType,
       c.isNullable AS isNullable,
       collect(DISTINCT fk.table + '.' + fk.name) AS foreignKeyPartners,
       collect(DISTINCT ix.name)                  AS indexes,
       collect(DISTINCT user.name)                AS viewsAndProcedures
ORDER BY database, schema, table;


// --- Variant: scope to one table ------------------------------------------
// MATCH (t:Table {name: $table})-[:HAS_COLUMN]->(c:Column)
// WHERE toLower(c.name) = toLower($column)
// ...same OPTIONAL MATCHes as above...


// --- Variant: all the way through to the code ------------------------------
// Adds the JPA entities mapped to the affected table and the files that declare
// them, so the answer names source files a human can open.
//
// MATCH (c:Column) WHERE toLower(c.name) = toLower($column)
// MATCH (t:Table)-[:HAS_COLUMN]->(c)
// OPTIONAL MATCH (t)<-[:MAPS_TO]-(e:Entity)<-[:CONTAINS_CLASS]-(f:File)
// RETURN t.database AS database, t.name AS table, c.dataType AS dataType,
//        collect(DISTINCT e.fqn)  AS entities,
//        collect(DISTINCT f.path) AS files
// ORDER BY database, table;


// --- Variant: raw SQL-text search, no link pass needed ---------------------
// Slower (it scans every stored definition) but works before `graphforge link`.
//
// MATCH (p:StoredProcedure)
// WHERE toLower(p.definition) CONTAINS toLower($column)
// RETURN p.database AS database, p.name AS procedure ORDER BY database, procedure;
