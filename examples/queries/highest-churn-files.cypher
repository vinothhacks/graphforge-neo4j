// ===========================================================================
// Question: "What keeps changing?"
//
// Ranks files by churn — total lines inserted + deleted across every commit
// that touched them. Churn concentrated in a handful of files is the cheapest
// signal you have for where refactoring, test coverage and review attention
// actually pay off.
//
// High churn + many distinct authors is the classic coordination hotspot.
// High churn + one author is a knowledge risk.
//
// Requires: `graphforge git` (history facet)
// Reads:    (:Commit)-[:CHANGED {insertions, deletions}]->(:File)
// ===========================================================================

MATCH (:Commit)-[ch:CHANGED]->(f:File)
RETURN f.repo AS repo,
       f.path AS path,
       sum(ch.insertions + ch.deletions) AS churn,
       sum(ch.insertions) AS inserted,
       sum(ch.deletions)  AS deleted,
       count(*) AS commits
ORDER BY churn DESC
LIMIT 20;


// --- Variant: churn + how many people are involved -------------------------
// The coordination-cost view: files many people keep rewriting.
//
// MATCH (a:Author)-[:AUTHORED]->(:Commit)-[ch:CHANGED]->(f:File)
// RETURN f.path AS path,
//        sum(ch.insertions + ch.deletions) AS churn,
//        count(DISTINCT a) AS authors
// ORDER BY churn DESC, authors DESC LIMIT 20;


// --- Variant: recent churn only -------------------------------------------
// `authoredAt` is stored as an ISO-8601 string, so a lexicographic compare is
// a correct date compare. Adjust the cutoff to taste.
//
// MATCH (c:Commit)-[ch:CHANGED]->(f:File)
// WHERE c.authoredAt >= '2025-01-01'
// RETURN f.path AS path, sum(ch.insertions + ch.deletions) AS churn
// ORDER BY churn DESC LIMIT 20;
