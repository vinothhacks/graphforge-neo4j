// ===========================================================================
// Question: "Who actually knows this code?"
//
// Ranks authors by the number of DISTINCT files they have ever touched, which
// is a far better proxy for breadth of knowledge than a raw commit count (one
// person merging a thousand times still only knows what they edited).
//
// Use it to find a reviewer for an unfamiliar area, or to spot a bus factor of
// one before it becomes a bus factor of zero.
//
// Requires: `graphforge git` (history facet, i.e. NOT --no-history)
// Reads:    (:Author)-[:AUTHORED]->(:Commit)-[:CHANGED]->(:File)
// ===========================================================================

MATCH (a:Author)-[:AUTHORED]->(:Commit)-[:CHANGED]->(f:File)
RETURN a.name  AS author,
       a.email AS email,
       count(DISTINCT f) AS files
ORDER BY files DESC
LIMIT 20;


// --- Variant: scope it to one repository ----------------------------------
// MATCH (a:Author)-[:AUTHORED]->(:Commit)-[:CHANGED]->(f:File)
// WHERE f.repo = 'my-service'
// RETURN a.name AS author, count(DISTINCT f) AS files
// ORDER BY files DESC LIMIT 20;


// --- Variant: owners of one directory -------------------------------------
// Who to ask about a specific subtree, most recent contribution first.
//
// MATCH (a:Author)-[:AUTHORED]->(c:Commit)-[:CHANGED]->(f:File)
// WHERE f.path STARTS WITH 'src/main/java/com/acme/billing/'
// RETURN a.name AS author,
//        count(DISTINCT f) AS files,
//        max(c.authoredAt) AS lastTouched
// ORDER BY files DESC LIMIT 10;
