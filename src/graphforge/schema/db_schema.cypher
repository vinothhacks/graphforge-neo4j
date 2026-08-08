// ============================================================
//  graphforge — Database knowledge-graph schema
//    Database-HAS_SCHEMA->Schema-HAS_TABLE->Table-HAS_COLUMN->Column
//    Table-HAS_INDEX->Index   Schema-HAS_VIEW->View   Schema-HAS_PROCEDURE->StoredProcedure
//    Column-FOREIGN_KEY->Column   Table-REFERENCES->Table
//  MySQL has no schema layer, so a synthetic Schema mirrors the database name.
//  Every node is MERGE-keyed on a deterministic `id`.
// ============================================================

CREATE CONSTRAINT uniq_database_id  IF NOT EXISTS FOR (n:Database)        REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_schema_id    IF NOT EXISTS FOR (n:Schema)          REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_table_id     IF NOT EXISTS FOR (n:Table)           REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_column_id    IF NOT EXISTS FOR (n:Column)          REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_index_id     IF NOT EXISTS FOR (n:Index)           REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_view_id      IF NOT EXISTS FOR (n:View)            REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_proc_id      IF NOT EXISTS FOR (n:StoredProcedure) REQUIRE n.id IS UNIQUE;

CREATE INDEX idx_database_name IF NOT EXISTS FOR (n:Database)        ON (n.name);
CREATE INDEX idx_table_name    IF NOT EXISTS FOR (n:Table)          ON (n.name);
CREATE INDEX idx_table_db      IF NOT EXISTS FOR (n:Table)          ON (n.database);
CREATE INDEX idx_column_name   IF NOT EXISTS FOR (n:Column)         ON (n.name);
CREATE INDEX idx_column_table  IF NOT EXISTS FOR (n:Column)         ON (n.table);
CREATE INDEX idx_view_name     IF NOT EXISTS FOR (n:View)           ON (n.name);
CREATE INDEX idx_proc_name     IF NOT EXISTS FOR (n:StoredProcedure) ON (n.name);
