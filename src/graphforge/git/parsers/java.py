"""Lightweight structural parser for Java source.

Regex-based (no full AST) — fast and dependency-free. It extracts the package,
imports, type declarations (class/interface/enum with extends/implements),
a best-effort list of methods, and — for knowledge-graph enrichment —
per-class annotations *with their arguments*, a framework **stereotype**
(Entity / ManagedBean / EJBBean), and the JPA **mappedTable** name.
"""
from __future__ import annotations

import re
from typing import Any

_PACKAGE = re.compile(r"^package\s+([\w.]+)\s*;")
_IMPORT = re.compile(r"^import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;")
_TYPE = re.compile(
    r"\b(?P<kind>class|interface|enum)\s+(?P<name>\w+)"
    r"(?:\s+extends\s+(?P<extends>[\w.]+))?"
    r"(?:\s+implements\s+(?P<impl>[\w.,\s]+?))?\s*[\{<]"
)
_METHOD = re.compile(
    r"^(?P<mods>(?:public|private|protected|static|final|abstract|synchronized|native|default|\s)*)"
    r"(?P<ret>[\w<>\[\],.\?\s]+?)\s+(?P<name>\w+)\s*\([^;{]*\)\s*(?:throws [\w,\s.]+)?\s*[{;]"
)
_ANNOTATION = re.compile(r"@(\w+)\s*(?:\((?P<args>.*?)\))?")
_NAME_ARG = re.compile(r'(?:name\s*=\s*)?"([^"]+)"')
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "else",
             "do", "synchronized", "try", "assert"}

# annotation simple-name -> semantic stereotype label
_STEREOTYPES = {
    "Entity": "Entity",
    "ManagedBean": "ManagedBean", "Named": "ManagedBean",
    "Stateless": "EJBBean", "Stateful": "EJBBean", "Singleton": "EJBBean",
    "MessageDriven": "EJBBean",
}


def extract(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "package": "", "imports": [], "classes": [], "interfaces": [],
        "enums": [], "methods": [], "annotations": [],
    }
    in_block_comment = False
    pending: list[dict[str, str]] = []  # annotations awaiting the next declaration

    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if line.startswith("/*"):
            if "*/" not in line:
                in_block_comment = True
            continue
        if line.startswith("//") or not line:
            continue

        m = _PACKAGE.match(line)
        if m:
            result["package"] = m.group(1)
            continue

        m = _IMPORT.match(line)
        if m:
            result["imports"].append({"fqn": m.group(1), "line": i})
            continue

        if line.startswith("@"):
            # consume one or more leading annotations; a declaration may follow
            # them on the SAME line (e.g. `@Entity public class X {`)
            while line.startswith("@"):
                am = _ANNOTATION.match(line)
                if not am:
                    break
                pending.append({"name": am.group(1), "args": (am.group("args") or "").strip(), "line": str(i)})
                result["annotations"].append({"name": am.group(1), "line": i})
                line = line[am.end():].strip()
            if not line:
                continue  # annotations sat alone on their line
            # otherwise fall through: the rest of the line is a declaration

        tm = _TYPE.search(line)
        if tm:
            kind = tm.group("kind")
            info = {
                "name": tm.group("name"),
                "type": kind,
                "line": i,
                "visibility": _visibility(line),
                "isAbstract": "abstract " in line,
                "isFinal": "final " in line,
                "annotations": [a["name"] for a in pending],
                "stereotype": _stereotype(pending),
                "mappedTable": _mapped_table(pending, tm.group("name")),
            }
            if tm.group("extends"):
                info["extends"] = tm.group("extends")
            if tm.group("impl"):
                info["implements"] = [x.strip() for x in tm.group("impl").split(",") if x.strip()]
            bucket = {"class": "classes", "interface": "interfaces", "enum": "enums"}[kind]
            result[bucket].append(info)
            pending = []
            continue

        mm = _METHOD.match(line)
        if mm and mm.group("name") not in _KEYWORDS and mm.group("ret").strip() not in _KEYWORDS:
            result["methods"].append({
                "name": mm.group("name"),
                "line": i,
                "returnType": mm.group("ret").strip(),
                "visibility": _visibility(line),
            })
        pending = []  # any other code line breaks the annotation→declaration binding

    return result


def _visibility(line: str) -> str:
    if "public " in line:
        return "public"
    if "private " in line:
        return "private"
    if "protected " in line:
        return "protected"
    return "package"


def _stereotype(pending: list[dict[str, str]]) -> str:
    for ann in pending:
        if ann["name"] in _STEREOTYPES:
            return _STEREOTYPES[ann["name"]]
    return ""


def _mapped_table(pending: list[dict[str, str]], class_name: str) -> str:
    """Return the JPA table this class maps to, or '' if it is not an entity.

    @Table(name="X") wins; otherwise an @Entity defaults to the class name.
    """
    has_entity = any(a["name"] == "Entity" for a in pending)
    for ann in pending:
        if ann["name"] == "Table" and ann["args"]:
            match = _NAME_ARG.search(ann["args"])
            if match:
                return match.group(1)
    return class_name if has_entity else ""
