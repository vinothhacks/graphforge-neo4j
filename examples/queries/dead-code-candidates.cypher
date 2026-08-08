// ===========================================================================
// Question: "What can we probably delete?"
//
// Files in one repository that satisfy BOTH tests:
//   1. no ingested commit has changed them since the cutoff date (files no
//      commit ever touched come back with lastChanged = null), and
//   2. nothing else in the same repo imports a class they declare.
//
// *** CANDIDATES ONLY — THIS IS NOT PROOF. ***
// It demonstrates absence of evidence, never absence of use. Reflection,
// dependency injection, Spring/CDI wiring by name, service loaders, dynamic
// imports, template and config references, framework entry points, callers in
// other repositories, and anything outside the ingested commit window all keep
// code alive while staying completely invisible here. A class used without an
// import (same package in Java, a wildcard import) also looks dead.
//
// Treat the output as a review queue, and confirm by hand before deleting.
// The MCP tool `find_dead_code` runs this same logic and attaches the caveat
// to every answer.
//
// Parameters: $repo   e.g. :param repo => 'my-service'
//             cutoff  edit the literal below (ISO-8601 date, string compare)
// Requires:   `graphforge git` with BOTH facets (structure + history)
// ===========================================================================

MATCH (f:File)
WHERE f.repo = $repo
OPTIONAL MATCH (c:Commit)-[:CHANGED]->(f)
WITH f, max(substring(coalesce(c.authoredAt, c.committedAt, ''), 0, 10)) AS lastChanged
WHERE lastChanged IS NULL OR lastChanged = '' OR lastChanged < '2025-01-01'
OPTIONAL MATCH (f)-[:CONTAINS_CLASS]->(cls:Class)
OPTIONAL MATCH (importer:File)-[:CONTAINS_CLASS]->(:Class)-[:IMPORTS]->(ref:Class)
  WHERE ref.fqn = cls.fqn AND importer.repo = f.repo AND importer <> f
WITH f, lastChanged,
     collect(DISTINCT cls.fqn) AS classes,
     count(DISTINCT importer)  AS importers
WHERE importers = 0
RETURN f.path AS path,
       lastChanged,
       classes
ORDER BY lastChanged, path
LIMIT 50;


// --- Note on the fqn join --------------------------------------------------
// IMPORTS edges land on the EXTERNAL stub node created for an imported fqn, not
// on the declaring :Class node, so the match above joins on `ref.fqn = cls.fqn`
// rather than on node identity. Matching by identity silently returns nothing.


// --- Variant: class-level instead of file-level ----------------------------
// MATCH (f:File)-[:CONTAINS_CLASS]->(cls:Class)
// WHERE f.repo = $repo AND coalesce(cls.external, false) = false
// OPTIONAL MATCH (c:Commit)-[:CHANGED]->(f)
// WITH f, cls, max(substring(coalesce(c.authoredAt, c.committedAt, ''), 0, 10)) AS lastChanged
// WHERE lastChanged IS NULL OR lastChanged < '2025-01-01'
// OPTIONAL MATCH (importer:File)-[:CONTAINS_CLASS]->(:Class)-[:IMPORTS]->(ref:Class)
//   WHERE ref.fqn = cls.fqn AND importer.repo = f.repo AND importer <> f
// WITH cls, f, lastChanged, count(DISTINCT importer) AS importers
// WHERE importers = 0
// RETURN cls.fqn AS fqn, cls.language AS language, f.path AS path, lastChanged
// ORDER BY lastChanged, fqn LIMIT 50;
