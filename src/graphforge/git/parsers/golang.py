"""Lightweight structural parser for Go source.

Regex/line based (no go/ast), so a file that does not compile still yields
partial structure instead of aborting the scan. It extracts the package clause,
imports (single and grouped ``import ( … )``), structs, interfaces, named type
declarations, and funcs — including methods with a receiver.

The returned dict mirrors :mod:`graphforge.git.parsers.java` — package, imports,
classes, interfaces, enums, methods, annotations — so ``git.ingest`` maps every
language through one code path. Go has no annotations, so that key stays empty;
``const ( … iota )`` blocks are surfaced as ``enums``.

Bucketing rules:
    ``type X struct``    -> ``classes`` (with ``type: "struct"``)
    ``type X interface`` -> ``interfaces``
    ``type X Underlying``-> ``classes`` (with ``type: "type"``)
"""
from __future__ import annotations

import contextlib
import re
from typing import Any

_PACKAGE = re.compile(r"^package\s+(?P<name>\w+)")
_IMPORT_ONE = re.compile(r'^import\s+(?:(?P<alias>[\w.]+)\s+)?"(?P<path>[^"]+)"')
_IMPORT_OPEN = re.compile(r"^import\s*\(")
_IMPORT_ENTRY = re.compile(r'^(?:(?P<alias>[\w.]+)\s+)?"(?P<path>[^"]+)"')
_TYPE_OPEN = re.compile(r"^type\s*\(\s*$")
_CONST_OPEN = re.compile(r"^const\s*\(")
_IOTA = re.compile(r"^\w+(?:\s*,\s*\w+)*\s+(?P<type>\w+)\s*=\s*iota\b")
_TYPE_DECL = re.compile(
    r"^(?:type\s+)?(?P<name>\w+)(?:\[[^\]]*\])?\s+(?P<kind>struct|interface)\s*\{")
_TYPE_ALIAS = re.compile(
    r"^(?:type\s+)?(?P<name>\w+)(?:\[[^\]]*\])?\s*=?\s+(?P<under>[\w\[\]*.]+)\s*$")
_METHOD = re.compile(
    r"^func\s*\(\s*(?:(?P<recv>\w+)\s+)?(?P<rtype>[\[\]*\w.]+)\s*\)\s*"
    r"(?P<name>\w+)\s*(?:\[[^\]]*\])?\s*\(")
_FUNC = re.compile(r"^func\s+(?P<name>\w+)\s*(?:\[[^\]]*\])?\s*\(")
_EMBEDDED = re.compile(r"^(?P<ptr>\*)?(?P<name>(?:\w+\.)?[A-Z]\w*)\s*(?:`[^`]*`)?\s*$")
_IFACE_METHOD = re.compile(r"^(?P<name>\w+)\s*\((?P<args>[^)]*)\)\s*(?P<ret>.*)$")
_IFACE_EMBED = re.compile(r"^(?P<name>(?:\w+\.)?[A-Z]\w*)\s*$")

_KEYWORDS = {"if", "for", "switch", "select", "return", "go", "defer", "range",
             "case", "default", "else", "func", "var", "const", "type", "struct",
             "interface", "map", "chan", "package", "import"}


def extract(lines: list[str]) -> dict[str, Any]:
    """Return the structural summary of a Go source file. Never raises."""
    result: dict[str, Any] = {
        "package": "", "imports": [], "classes": [], "interfaces": [],
        "enums": [], "methods": [], "annotations": [],
    }
    # a malformed file must never abort a repo scan
    with contextlib.suppress(Exception):
        _extract_into(lines, result)
    return result


# ---------------------------------------------------------------------------
def _extract_into(lines: list[str], result: dict[str, Any]) -> None:
    in_block_comment = False
    in_imports = False
    in_types = False
    in_consts = False
    body: dict[str, Any] | None = None    # open struct / interface declaration
    seen_enums: set[str] = set()

    for i, raw in enumerate(lines, 1):
        code, in_block_comment = _strip(raw, in_block_comment)
        line = code.strip()
        if not line:
            continue

        if in_imports:
            if line.startswith(")"):
                in_imports = False
                continue
            m = _IMPORT_ENTRY.match(line)
            if m:
                result["imports"].append(_import(m, i))
            continue

        if in_consts:
            if line.startswith(")"):
                in_consts = False
                continue
            m = _IOTA.match(line)
            if m and m.group("type") not in seen_enums and m.group("type") not in _KEYWORDS:
                seen_enums.add(m.group("type"))
                _promote_enum(result, m.group("type"), i)
            continue

        if body is not None:
            if line.startswith("}"):
                body = None
                continue
            _body_member(result, body, line, i)
            continue

        pm = _PACKAGE.match(line)
        if pm:
            result["package"] = pm.group("name")
            continue

        if _IMPORT_OPEN.match(line):
            in_imports = True
            continue
        m = _IMPORT_ONE.match(line)
        if m:
            result["imports"].append(_import(m, i))
            continue

        if _CONST_OPEN.match(line):
            in_consts = True
            continue
        if _TYPE_OPEN.match(line):
            in_types = True
            continue
        if in_types and line.startswith(")"):
            in_types = False
            continue

        if in_types or line.startswith("type "):
            decl = _type_declaration(result, line, i)
            if decl is not None:
                body = decl
                continue
            if _TYPE_ALIAS.match(line) and (in_types or line.startswith("type ")):
                am = _TYPE_ALIAS.match(line)
                if am.group("name") not in _KEYWORDS:
                    info = _type_info(am.group("name"), "type", i)
                    info["extends"] = am.group("under")
                    result["classes"].append(info)
            continue

        mm = _METHOD.match(line)
        if mm:
            result["methods"].append(_method(
                mm.group("name"), i, line, _base_type(mm.group("rtype")),
                receiver=(mm.group("recv") or ""), args_at=mm.end() - 1))
            continue

        fm = _FUNC.match(line)
        if fm and fm.group("name") not in _KEYWORDS:
            result["methods"].append(_method(fm.group("name"), i, line, "",
                                             args_at=fm.end() - 1))
            continue


# ---- helpers --------------------------------------------------------------
def _strip(raw: str, in_block_comment: bool) -> tuple[str, bool]:
    """Remove comments. String and rune literals are kept (import paths need them)."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if in_block_comment:
            j = raw.find("*/", i)
            if j == -1:
                return "".join(out), True
            i, in_block_comment = j + 2, False
            continue
        two = raw[i:i + 2]
        if two == "//":
            break
        if two == "/*":
            in_block_comment = True
            i += 2
            continue
        ch = raw[i]
        if ch in ('"', "`", "'"):
            j = i + 1
            while j < n:
                if raw[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if raw[j] == ch:
                    break
                j += 1
            out.append(raw[i:j + 1])
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_block_comment


def _visibility(name: str) -> str:
    """Go exports identifiers that start with an upper-case letter."""
    return "public" if name[:1].isupper() else "private"


def _base_type(text: str) -> str:
    return text.lstrip("*[]").split(".")[-1].strip()


def _import(match: re.Match, line: int) -> dict[str, Any]:
    return {"fqn": match.group("path"), "line": line,
            "alias": (match.group("alias") or "").strip(), "kind": "import"}


def _type_info(name: str, kind: str, line: int) -> dict[str, Any]:
    return {
        "name": name, "type": kind, "line": line,
        "visibility": _visibility(name),
        "isAbstract": kind == "interface", "isFinal": False,
        "annotations": [], "stereotype": "", "mappedTable": "",
    }


def _method(name: str, line: int, code: str, owner: str, receiver: str = "",
            args_at: int = 0) -> dict[str, Any]:
    return {
        "name": name, "line": line, "returnType": _return_type(code, args_at),
        "visibility": _visibility(name), "owner": owner, "receiver": receiver,
        "isAsync": False, "annotations": [],
    }


def _return_type(code: str, args_at: int = 0) -> str:
    """Take the text between the parameter list's closing paren and the body.

    ``args_at`` is the offset of the parameter list's ``(`` so that a method's
    receiver — ``func (s *Store) Get(…) error`` — is not mistaken for it.
    """
    start = code.find("(", args_at)
    if start == -1:
        return ""
    depth = 0
    for idx in range(start, len(code)):
        if code[idx] == "(":
            depth += 1
        elif code[idx] == ")":
            depth -= 1
            if depth == 0:
                return code[idx + 1:].split("{")[0].strip()
    return ""


def _promote_enum(result: dict[str, Any], name: str, line: int) -> None:
    """Record a `const ( X T = iota )` type as an enum, replacing its alias entry."""
    for idx, entry in enumerate(result["classes"]):
        if entry["name"] == name and entry["type"] == "type":
            entry["type"] = "enum"
            result["enums"].append(result["classes"].pop(idx))
            return
    result["enums"].append(_type_info(name, "enum", line))


def _type_declaration(result: dict[str, Any], line: str, i: int) -> dict[str, Any] | None:
    """Handle `type X struct {` / `type X interface {`; return the open body, if any."""
    m = _TYPE_DECL.match(line)
    if not m or m.group("name") in _KEYWORDS:
        return None
    kind = m.group("kind")
    info = _type_info(m.group("name"), kind, i)
    bucket = "interfaces" if kind == "interface" else "classes"
    result[bucket].append(info)
    if line.rstrip().endswith("}"):       # single-line `type X struct{}`
        return None
    return {"kind": kind, "info": info}


def _body_member(result: dict[str, Any], body: dict[str, Any], line: str, i: int) -> None:
    """Record embedded types and interface method signatures."""
    info = body["info"]
    if body["kind"] == "interface":
        em = _IFACE_EMBED.match(line)
        if em:
            _add_base(info, em.group("name"))
            return
        mm = _IFACE_METHOD.match(line)
        if mm and mm.group("name") not in _KEYWORDS:
            result["methods"].append({
                "name": mm.group("name"), "line": i,
                "returnType": (mm.group("ret") or "").strip(),
                "visibility": _visibility(mm.group("name")),
                "owner": info["name"], "receiver": "", "isAsync": False,
                "annotations": [],
            })
        return
    em = _EMBEDDED.match(line)
    if em and em.group("name") not in _KEYWORDS:
        _add_base(info, em.group("name"))


def _add_base(info: dict[str, Any], name: str) -> None:
    """Embedded types are Go's composition; model the first as extends."""
    if "extends" not in info:
        info["extends"] = name
    else:
        info.setdefault("implements", []).append(name)
