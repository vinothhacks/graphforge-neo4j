// ============================================================
//  graphforge — VDS (Virtual Data Service) catalog schema
//    VDSService-HAS_QUERY->VDSQuery-USES_TABLE->Table
//    VDSService-HAS_WHERE_FIELD->VDSWhereField-REFERENCES_TABLE->Table
//  Optional / application-specific. USES_TABLE / REFERENCES_TABLE only
//  attach where a matching :Table already exists in the graph.
// ============================================================

CREATE CONSTRAINT uniq_vdsservice_id IF NOT EXISTS FOR (n:VDSService)    REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_vdsquery_id   IF NOT EXISTS FOR (n:VDSQuery)      REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_vdswf_id      IF NOT EXISTS FOR (n:VDSWhereField) REQUIRE n.id IS UNIQUE;

CREATE INDEX idx_vdsservice_name IF NOT EXISTS FOR (n:VDSService)    ON (n.serviceName);
CREATE INDEX idx_vdsservice_sid  IF NOT EXISTS FOR (n:VDSService)    ON (n.sid);
CREATE INDEX idx_vdswf_table     IF NOT EXISTS FOR (n:VDSWhereField) ON (n.tableName);
