// ============================================================
//  graphforge — Git knowledge-graph schema
//  Two facets that share :File nodes:
//    structure : Repository-HAS_MODULE->Module-HAS_PACKAGE->Package
//                -CONTAINS_FILE->File-CONTAINS_CLASS->Class-HAS_METHOD->Method
//                File-CONTAINS_LINE->Line-NEXT_LINE->Line
//                Module-DEPENDS_ON->Dependency
//    history   : Author-AUTHORED->Commit-PARENT->Commit
//                Commit-CHANGED->File   Branch-POINTS_TO->Commit   Tag-TAGS->Commit
//  Every node is MERGE-keyed on a deterministic `id` (Community-compatible).
// ============================================================

// ---- uniqueness constraints (single-property `id`) ----
CREATE CONSTRAINT uniq_repository_id IF NOT EXISTS FOR (n:Repository) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_module_id     IF NOT EXISTS FOR (n:Module)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_package_id    IF NOT EXISTS FOR (n:Package)    REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_file_id       IF NOT EXISTS FOR (n:File)       REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_class_id      IF NOT EXISTS FOR (n:Class)      REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_method_id     IF NOT EXISTS FOR (n:Method)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_dependency_id IF NOT EXISTS FOR (n:Dependency) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_commit_id     IF NOT EXISTS FOR (n:Commit)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_author_id     IF NOT EXISTS FOR (n:Author)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_branch_id     IF NOT EXISTS FOR (n:Branch)     REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT uniq_tag_id        IF NOT EXISTS FOR (n:Tag)        REQUIRE n.id IS UNIQUE;

// ---- lookup indexes ----
CREATE INDEX idx_module_name  IF NOT EXISTS FOR (n:Module)  ON (n.name);
CREATE INDEX idx_package_name IF NOT EXISTS FOR (n:Package) ON (n.name);
CREATE INDEX idx_file_path    IF NOT EXISTS FOR (n:File)    ON (n.path);
CREATE INDEX idx_file_name    IF NOT EXISTS FOR (n:File)    ON (n.name);
CREATE INDEX idx_file_repo    IF NOT EXISTS FOR (n:File)    ON (n.repo);
CREATE INDEX idx_class_name   IF NOT EXISTS FOR (n:Class)   ON (n.name);
CREATE INDEX idx_class_fqn    IF NOT EXISTS FOR (n:Class)   ON (n.fqn);
CREATE INDEX idx_method_name  IF NOT EXISTS FOR (n:Method)  ON (n.name);
CREATE INDEX idx_commit_hash  IF NOT EXISTS FOR (n:Commit)  ON (n.hash);
CREATE INDEX idx_commit_when  IF NOT EXISTS FOR (n:Commit)  ON (n.authoredAt);
CREATE INDEX idx_author_email IF NOT EXISTS FOR (n:Author)  ON (n.email);
CREATE INDEX idx_dep_artifact IF NOT EXISTS FOR (n:Dependency) ON (n.artifactId);

// ---- Line is high-volume and optional; index instead of constrain ----
CREATE INDEX idx_line_file    IF NOT EXISTS FOR (n:Line) ON (n.fileId);
CREATE INDEX idx_line_file_no IF NOT EXISTS FOR (n:Line) ON (n.fileId, n.number);
