"""Walk a repository, detect build modules, and extract per-file structure.

Paths are stored relative to the repository root (POSIX separators) so that
:File nodes produced here share identity with the :File nodes produced from git
history.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .parsers import generic, java, pom


@dataclass
class Module:
    key: str          # repo-relative dir ("" == repo root); unique within repo
    name: str         # display name (artifactId or dir name)
    root_abs: str
    pom: Optional[dict] = None


@dataclass
class ScannedFile:
    relpath: str
    name: str
    type: str
    extension: str
    total_lines: int
    hash: str
    module_key: str
    package: str = ""
    java: Optional[dict] = None
    lines: List[dict] = field(default_factory=list)


def _hash(path: str) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _read_lines(path: str) -> List[str]:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
    return []


def find_modules(repo_root: str, repo_name: str) -> List[Module]:
    modules: List[Module] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in generic.SKIP_DIRS]
        rel = os.path.relpath(dirpath, repo_root).replace("\\", "/")
        rel = "" if rel == "." else rel
        if "pom.xml" in filenames:
            meta = pom.parse_pom(os.path.join(dirpath, "pom.xml"))
            name = meta.get("artifactId") or (rel.split("/")[-1] if rel else repo_name)
            modules.append(Module(key=rel, name=name, root_abs=dirpath, pom=meta))
        elif "build.gradle" in filenames or "build.gradle.kts" in filenames:
            name = rel.split("/")[-1] if rel else repo_name
            modules.append(Module(key=rel, name=name, root_abs=dirpath))
    if not modules:
        modules.append(Module(key="", name=repo_name, root_abs=repo_root))
    return modules


def _module_for(file_abs: str, modules: List[Module]) -> Module:
    best = modules[0]
    best_len = -1
    file_dir = os.path.dirname(file_abs)
    for mod in modules:
        root = mod.root_abs
        if file_dir == root or file_dir.startswith(root + os.sep):
            if len(root) > best_len:
                best, best_len = mod, len(root)
    return best


def scan_file(file_abs: str, repo_root: str, module_key: str, include_lines: bool) -> ScannedFile:
    rel = os.path.relpath(file_abs, repo_root).replace("\\", "/")
    name = os.path.basename(file_abs)
    ext = os.path.splitext(name)[1].lower()
    ftype = generic.file_type_for(name) or "other"
    raw = _read_lines(file_abs)

    sf = ScannedFile(
        relpath=rel, name=name, type=ftype, extension=ext,
        total_lines=len(raw), hash=_hash(file_abs), module_key=module_key,
    )
    if ftype == "java":
        info = java.extract(raw)
        sf.java = info
        sf.package = info.get("package", "")
    if include_lines:
        sf.lines = [
            {"number": i, "content": line, "type": generic.classify_line(line, ext)}
            for i, line in enumerate(raw, 1)
        ]
    return sf


def scan_repo(repo_root: str, repo_name: str, include_lines: bool = False) -> Dict[str, Any]:
    repo_root = os.path.abspath(repo_root)
    modules = find_modules(repo_root, repo_name)
    files: List[ScannedFile] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in generic.SKIP_DIRS]
        for filename in filenames:
            if filename in generic.SKIP_FILES:
                continue
            if generic.file_type_for(filename) is None:
                continue
            file_abs = os.path.join(dirpath, filename)
            mod = _module_for(file_abs, modules)
            files.append(scan_file(file_abs, repo_root, mod.key, include_lines))

    return {"modules": modules, "files": files}
