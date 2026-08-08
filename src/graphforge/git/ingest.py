"""Orchestrate Git -> graph: resolve repo, scan structure, read history, write.

Structure and history both create :File nodes keyed on ``repo/relpath`` so the
two facets connect through shared files without any cross-linking to the DB
subgraph.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from ..core.config import GitSettings
from ..core.cypher import NodeRef, Operation, lit, merge_node, merge_rel, set_label
from ..core.neo4j_writer import Neo4jWriter, load_schema
from . import history as history_mod
from . import scan as scan_mod
from .clone import GitHandler

log = logging.getLogger("graphforge.git.ingest")

try:  # progress bar is optional
    from tqdm import tqdm

    def _progress(it, **kw):
        return tqdm(it, **kw)
except ImportError:  # pragma: no cover
    def _progress(it, **kw):
        return it


# ---- deterministic id builders -------------------------------------------
def _repo_id(repo: str) -> str: return repo
def _module_id(repo: str, key: str) -> str: return f"{repo}/{key or '.'}"
def _package_id(repo: str, mkey: str, pkg: str) -> str: return f"{repo}/{mkey or '.'}/{pkg}"
def _file_id(repo: str, relpath: str) -> str: return f"{repo}/{relpath}"
def _class_id(repo: str, fqn: str) -> str: return f"{repo}/{fqn}"
def _ext_class_id(fqn: str) -> str: return f"ext/{fqn}"
def _method_id(repo: str, owner_fqn: str, name: str) -> str: return f"{repo}/{owner_fqn}#{name}"
def _dep_id(g: str, a: str, v: str) -> str: return f"{g}:{a}:{v}"
def _commit_id(repo: str, h: str) -> str: return f"{repo}@{h}"
def _author_id(email: str, name: str) -> str: return (email or name).lower()
def _branch_id(repo: str, name: str) -> str: return f"{repo}#branch/{name}"
def _tag_id(repo: str, name: str) -> str: return f"{repo}#tag/{name}"


class GitIngestor:
    def __init__(self, writer: Neo4jWriter, settings: GitSettings):
        self.writer = writer
        self.gs = settings
        self.handler = GitHandler(
            settings.repo_dir, settings.username, settings.password, settings.token
        )

    # -- public ------------------------------------------------------------
    def apply_schema(self) -> None:
        self.writer.apply_schema(load_schema("git_schema.cypher"))

    def ingest(
        self,
        repo_specs: list[dict],
        include_lines: bool = False,
        with_structure: bool = True,
        with_history: bool = True,
        replace: bool = False,
        since_commit: str = "",
    ) -> dict[str, int]:
        stats = {"repos": 0, "files": 0, "commits": 0}
        failures = []
        for spec in _progress(repo_specs, desc="repos", unit="repo"):
            name = spec.get("name") or spec.get("url") or spec.get("path") or "?"
            try:
                s = self.ingest_repo(spec, include_lines, with_structure, with_history,
                                     replace, since_commit)
                stats["repos"] += 1
                stats["files"] += s.get("files", 0)
                stats["commits"] += s.get("commits", 0)
            except Exception as exc:  # noqa: BLE001  # one bad repo must not abort the batch
                log.error("failed to ingest repo %s: %s", name, exc)
                failures.append(name)
        if failures:
            stats["failed"] = len(failures)
            log.warning("%d repo(s) failed: %s", len(failures), ", ".join(failures))
        return stats

    def clear_repo(self, repo: str) -> int:
        """Delete a repository's existing subgraph (idempotent re-ingest)."""
        r = lit(repo)
        statements = [
            # Line can be high-volume -> delete in batched sub-transactions.
            f"MATCH (l:Line {{repo:{r}}}) CALL {{ WITH l DETACH DELETE l }} "
            f"IN TRANSACTIONS OF 10000 ROWS",
            f"MATCH (n:Method {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Class {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:File {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Package {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Module {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Commit {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Branch {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Tag {{repo:{r}}}) DETACH DELETE n",
            f"MATCH (n:Repository) WHERE n.id = {r} DETACH DELETE n",
        ]
        return self.writer.run_statements(statements, desc=f"{repo}: clear")

    def ingest_repo(
        self,
        spec: dict,
        include_lines: bool = False,
        with_structure: bool = True,
        with_history: bool = True,
        replace: bool = False,
        since_commit: str = "",
    ) -> dict[str, int]:
        path = self.handler.resolve(spec, self.gs.default_branch)
        repo = spec.get("name") or os.path.basename(os.path.normpath(path))
        log.info("Ingesting repo '%s' from %s", repo, path)

        if replace:
            self.clear_repo(repo)
        since = "" if replace else self._resolve_since(repo, path, since_commit)
        if since:
            log.info("Incremental ingest of '%s' from %s..HEAD", repo, since[:12])

        self.writer.write([
            merge_node("Repository", {"id": _repo_id(repo)}, {
                "name": repo,
                "url": spec.get("url", ""),
                "path": path,
                "defaultBranch": spec.get("branch") or self.gs.default_branch,
                "status": "ingesting",
            }, comment=f"Repository {repo}")
        ], desc=f"{repo}: repo")

        stats = {"files": 0, "commits": 0}
        if with_structure:
            only = history_mod.changed_paths(path, since) if since else None
            stats["files"] = self._ingest_structure(repo, path, include_lines, only)
        if with_history:
            stats["commits"] = self._ingest_history(repo, path, since)

        # record completion status + counts + the HEAD we ingested up to, so the
        # next run can continue with --since-commit auto
        head = history_mod.head_commit(path)
        self.writer.write([Operation(
            "MATCH (r:Repository {id: $id}) "
            "SET r.status = $status, r.lastIngestedAt = $at, r.files = $files, "
            "r.commits = $commits, r.lastCommit = $lastCommit",
            {"id": _repo_id(repo), "status": "completed",
             "at": datetime.now(timezone.utc).isoformat(),
             "files": stats["files"], "commits": stats["commits"],
             "lastCommit": head},
            comment=f"{repo}: status")], desc=f"{repo}: status")
        return stats

    # -- incremental -------------------------------------------------------
    def _resolve_since(self, repo: str, path: str, since_commit: str) -> str:
        """Turn ``--since-commit`` into a sha ('auto' reads :Repository.lastCommit)."""
        if not since_commit:
            return ""
        sha = self.stored_last_commit(repo) if since_commit == "auto" else since_commit
        if not sha:
            log.info("no stored lastCommit for '%s'; running a full ingest", repo)
            return ""
        if not history_mod.commit_exists(path, sha):
            log.warning("commit %s not found in '%s'; running a full ingest", sha[:12], repo)
            return ""
        if sha == history_mod.head_commit(path):
            log.info("'%s' is already at %s; nothing new to ingest", repo, sha[:12])
        return sha

    def stored_last_commit(self, repo: str) -> str:
        """Read :Repository.lastCommit from the graph (push mode only)."""
        if self.writer.mode != "push":
            return ""
        try:
            driver = self.writer._driver_connect()  # noqa: SLF001  # read-back has no public writer API
            with driver.session(database=self.writer.settings.database) as session:
                rec = session.run(
                    "MATCH (r:Repository {id: $id}) RETURN r.lastCommit AS c",
                    id=_repo_id(repo)).single()
            return (rec["c"] or "") if rec else ""
        except Exception as exc:  # noqa: BLE001  # unreadable state must not block ingest
            log.warning("could not read lastCommit for '%s': %s", repo, exc)
            return ""

    # -- structure ---------------------------------------------------------
    def _ingest_structure(self, repo: str, path: str, include_lines: bool,
                          only_paths: set[str] | None = None) -> int:
        data = scan_mod.scan_repo(path, repo, include_lines, only_paths)
        modules: list[scan_mod.Module] = data["modules"]
        files: list[scan_mod.ScannedFile] = data["files"]
        module_name_by_key = {m.key: m.name for m in modules}

        # modules + dependencies
        mod_ops: list[Operation] = []
        for m in modules:
            mid = _module_id(repo, m.key)
            pom = m.pom or {}
            mod_ops.append(merge_node("Module", {"id": mid}, {
                "name": m.name, "key": m.key, "repo": repo,
                "groupId": pom.get("groupId", ""),
                "artifactId": pom.get("artifactId", m.name),
                "version": pom.get("version", ""),
                "packaging": pom.get("packaging", ""),
                "description": pom.get("name", ""),
            }))
            mod_ops.append(merge_rel(
                NodeRef("Repository", {"id": _repo_id(repo)}), "HAS_MODULE",
                NodeRef("Module", {"id": mid})))
            for dep in pom.get("dependencies", []):
                if not dep.get("artifactId"):
                    continue
                did = _dep_id(dep.get("groupId", ""), dep["artifactId"], dep.get("version", ""))
                mod_ops.append(merge_node("Dependency", {"id": did}, {
                    "groupId": dep.get("groupId", ""),
                    "artifactId": dep["artifactId"],
                    "version": dep.get("version", ""),
                }))
                mod_ops.append(merge_rel(
                    NodeRef("Module", {"id": mid}), "DEPENDS_ON",
                    NodeRef("Dependency", {"id": did}), {"scope": dep.get("scope", "compile")}))
        self.writer.write(mod_ops, desc=f"{repo}: modules")

        # packages (unique per module+package)
        pkg_seen = set()
        pkg_ops: list[Operation] = []
        for f in files:
            if f.type == "java" and f.package:
                mkey = f.module_key
                marker = (mkey, f.package)
                if marker in pkg_seen:
                    continue
                pkg_seen.add(marker)
                pid = _package_id(repo, mkey, f.package)
                pkg_ops.append(merge_node("Package", {"id": pid}, {
                    "name": f.package, "module": module_name_by_key.get(mkey, mkey),
                    "repo": repo, "shortName": f.package.split(".")[-1],
                }))
                pkg_ops.append(merge_rel(
                    NodeRef("Module", {"id": _module_id(repo, mkey)}), "HAS_PACKAGE",
                    NodeRef("Package", {"id": pid})))
        self.writer.write(pkg_ops, desc=f"{repo}: packages")

        # files (+ classes, methods, imports, optional lines) — flush per file
        for f in files:
            self.writer.write(
                self._file_ops(repo, f, module_name_by_key, include_lines),
                desc=f"{repo}: {f.name}",
            )
        return len(files)

    def _file_ops(self, repo, f: scan_mod.ScannedFile, module_name_by_key, include_lines) -> list[Operation]:
        fid = _file_id(repo, f.relpath)
        ops: list[Operation] = [
            merge_node("File", {"id": fid}, {
                "path": f.relpath, "name": f.name, "repo": repo,
                "module": module_name_by_key.get(f.module_key, f.module_key),
                "type": f.type, "extension": f.extension,
                "totalLines": f.total_lines, "hash": f.hash,
            }, comment=f"File {f.relpath}")
        ]
        # attach file to its package (java) or directly to its module
        if f.type == "java" and f.package:
            ops.append(merge_rel(
                NodeRef("Package", {"id": _package_id(repo, f.module_key, f.package)}),
                "CONTAINS_FILE", NodeRef("File", {"id": fid})))
        else:
            ops.append(merge_rel(
                NodeRef("Module", {"id": _module_id(repo, f.module_key)}),
                "CONTAINS_FILE", NodeRef("File", {"id": fid})))

        if f.structure:
            ops += self._structure_ops(repo, fid, f)
        if include_lines and f.lines:
            ops += self._line_ops(fid, repo, f)
        return ops

    def _structure_ops(self, repo, fid, f) -> list[Operation]:
        """Class / Method / import operations for any parsed language.

        Every parser in ``git.parsers`` returns the same dict shape, so Java,
        Python, TypeScript/JavaScript and Go all flow through this one path.
        """
        ops: list[Operation] = []
        ns = f.namespace
        owned_fqns: list[str] = []
        fqn_by_name: dict[str, str] = {}
        for kind_key, decl_type in (("classes", "class"), ("interfaces", "interface"), ("enums", "enum")):
            for c in f.structure.get(kind_key, []):
                fqn = f"{ns}.{c['name']}" if ns else c["name"]
                owned_fqns.append(fqn)
                fqn_by_name.setdefault(c["name"], fqn)
                cid = _class_id(repo, fqn)
                ops.append(merge_node("Class", {"id": cid}, {
                    "name": c["name"], "fqn": fqn, "repo": repo, "module": f.module_key,
                    "type": c.get("type") or decl_type, "visibility": c.get("visibility", "public"),
                    "isAbstract": c.get("isAbstract", False), "isFinal": c.get("isFinal", False),
                    "lineNumber": c.get("line", 0), "external": False,
                    "language": f.type,
                    "stereotype": c.get("stereotype", ""), "mappedTable": c.get("mappedTable", ""),
                }))
                ops.append(merge_rel(NodeRef("File", {"id": fid}), "CONTAINS_CLASS",
                                     NodeRef("Class", {"id": cid})))
                # promote framework classes to a semantic label (Entity/Component/…)
                if c.get("stereotype"):
                    ops.append(set_label(NodeRef("Class", {"id": cid}), c["stereotype"]))
                if c.get("extends"):
                    ecid = _ext_class_id(c["extends"])
                    ops.append(merge_node("Class", {"id": ecid},
                                          {"name": c["extends"].split(".")[-1], "fqn": c["extends"], "external": True}))
                    ops.append(merge_rel(NodeRef("Class", {"id": cid}), "EXTENDS",
                                         NodeRef("Class", {"id": ecid})))
                for iface in c.get("implements", []):
                    icid = _ext_class_id(iface)
                    ops.append(merge_node("Class", {"id": icid},
                                          {"name": iface.split(".")[-1], "fqn": iface, "external": True}))
                    ops.append(merge_rel(NodeRef("Class", {"id": cid}), "IMPLEMENTS",
                                         NodeRef("Class", {"id": icid})))
        ops += self._method_ops(repo, fid, f, owned_fqns, fqn_by_name)
        # imports: file's owned types IMPORT external fqns
        for imp in f.structure.get("imports", []):
            icid = _ext_class_id(imp["fqn"])
            ops.append(merge_node("Class", {"id": icid},
                                  {"name": imp["fqn"].split(".")[-1], "fqn": imp["fqn"], "external": True}))
            for owner in owned_fqns:
                ops.append(merge_rel(NodeRef("Class", {"id": _class_id(repo, owner)}), "IMPORTS",
                                     NodeRef("Class", {"id": icid})))
        return ops

    def _method_ops(self, repo, fid, f, owned_fqns, fqn_by_name) -> list[Operation]:
        """Attach methods to their declaring type (Python class, Go receiver, …).

        The Java parser reports no owner, so it keeps its historical behaviour of
        binding every method to the file's first type. The newer parsers do name
        an owner, so their free functions (Go package funcs, Python module-level
        defs) hang off the :File node instead of being misattributed or dropped.
        """
        ops: list[Operation] = []
        for meth in f.structure.get("methods", []):
            owner = fqn_by_name.get(meth.get("owner", ""), "")
            if not owner and f.type == "java":
                owner = owned_fqns[0] if owned_fqns else ""
            if not owner:
                if f.type == "java":
                    continue  # unchanged Java behaviour: no type, no methods
                mid = _method_id(repo, f.relpath, meth["name"])
                ops.append(merge_node("Method", {"id": mid}, {
                    "name": meth["name"], "owner": "", "repo": repo,
                    "returnType": meth.get("returnType", ""),
                    "visibility": meth.get("visibility", "public"),
                    "lineNumber": meth.get("line", 0), "language": f.type,
                }))
                ops.append(merge_rel(NodeRef("File", {"id": fid}), "CONTAINS_METHOD",
                                     NodeRef("Method", {"id": mid})))
                continue
            mid = _method_id(repo, owner, meth["name"])
            ops.append(merge_node("Method", {"id": mid}, {
                "name": meth["name"], "owner": owner, "repo": repo,
                "returnType": meth.get("returnType", ""),
                "visibility": meth.get("visibility", "public"),
                "lineNumber": meth.get("line", 0), "language": f.type,
            }))
            ops.append(merge_rel(NodeRef("Class", {"id": _class_id(repo, owner)}), "HAS_METHOD",
                                 NodeRef("Method", {"id": mid})))
        return ops

    def _line_ops(self, fid, repo, f) -> list[Operation]:
        ops: list[Operation] = []
        for ln in f.lines:
            ops.append(merge_node("Line", {"fileId": fid, "number": ln["number"]},
                                  {"content": ln["content"], "type": ln["type"], "repo": repo}))
        for i in range(len(f.lines) - 1):
            ops.append(merge_rel(
                NodeRef("Line", {"fileId": fid, "number": f.lines[i]["number"]}), "NEXT_LINE",
                NodeRef("Line", {"fileId": fid, "number": f.lines[i + 1]["number"]})))
        return ops

    # -- history -----------------------------------------------------------
    def _ingest_history(self, repo: str, path: str, since: str = "") -> int:
        h = history_mod.extract_history(path, self.gs.history_limit, since)

        author_ops = [
            merge_node("Author", {"id": aid}, {"name": a["name"], "email": a["email"]})
            for aid, a in h.authors.items()
        ]
        self.writer.write(author_ops, desc=f"{repo}: authors")

        commit_ops: list[Operation] = []
        for c in h.commits:
            cid = _commit_id(repo, c["hash"])
            commit_ops.append(merge_node("Commit", {"id": cid}, {
                "hash": c["hash"], "short": c["short"], "message": c["message"],
                "authoredAt": c["authoredAt"], "committedAt": c["committedAt"],
                "insertions": c["insertions"], "deletions": c["deletions"],
                "filesChanged": c["filesChanged"], "parentCount": len(c["parents"]),
            }, comment=f"Commit {c['short']}"))
            commit_ops.append(merge_rel(NodeRef("Repository", {"id": _repo_id(repo)}),
                                        "HAS_COMMIT", NodeRef("Commit", {"id": cid})))
            commit_ops.append(merge_rel(
                NodeRef("Author", {"id": _author_id(c["authorEmail"], c["authorName"])}),
                "AUTHORED", NodeRef("Commit", {"id": cid})))
            for p in c["parents"]:
                pid = _commit_id(repo, p)
                commit_ops.append(merge_node("Commit", {"id": pid}))  # stub if beyond range
                commit_ops.append(merge_rel(NodeRef("Commit", {"id": cid}), "PARENT",
                                            NodeRef("Commit", {"id": pid})))
            for fc in c["files"]:
                fid = _file_id(repo, fc["path"])
                commit_ops.append(merge_node("File", {"id": fid},
                                             {"path": fc["path"], "name": os.path.basename(fc["path"]), "repo": repo}))
                commit_ops.append(merge_rel(NodeRef("Commit", {"id": cid}), "CHANGED",
                                            NodeRef("File", {"id": fid}), {
                                                "changeType": fc["changeType"],
                                                "insertions": fc["insertions"],
                                                "deletions": fc["deletions"]}))
            if len(commit_ops) >= 2000:
                self.writer.write(commit_ops, desc=f"{repo}: commits")
                commit_ops = []
        if commit_ops:
            self.writer.write(commit_ops, desc=f"{repo}: commits")

        ref_ops: list[Operation] = []
        for b in h.branches:
            bid = _branch_id(repo, b["name"])
            ref_ops.append(merge_node("Branch", {"id": bid}, {"name": b["name"], "repo": repo}))
            ref_ops.append(merge_rel(NodeRef("Repository", {"id": _repo_id(repo)}),
                                     "HAS_BRANCH", NodeRef("Branch", {"id": bid})))
            ccid = _commit_id(repo, b["commit"])
            ref_ops.append(merge_node("Commit", {"id": ccid}))
            ref_ops.append(merge_rel(NodeRef("Branch", {"id": bid}), "POINTS_TO",
                                     NodeRef("Commit", {"id": ccid})))
        for t in h.tags:
            tid = _tag_id(repo, t["name"])
            ref_ops.append(merge_node("Tag", {"id": tid}, {"name": t["name"], "repo": repo}))
            ref_ops.append(merge_rel(NodeRef("Repository", {"id": _repo_id(repo)}),
                                     "HAS_TAG", NodeRef("Tag", {"id": tid})))
            ccid = _commit_id(repo, t["commit"])
            ref_ops.append(merge_node("Commit", {"id": ccid}))
            ref_ops.append(merge_rel(NodeRef("Tag", {"id": tid}), "TAGS",
                                     NodeRef("Commit", {"id": ccid})))
        self.writer.write(ref_ops, desc=f"{repo}: refs")
        return len(h.commits)
