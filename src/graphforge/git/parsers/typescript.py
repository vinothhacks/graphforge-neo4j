"""Lightweight structural parser for TypeScript / JavaScript source.

Covers ``.ts`` ``.tsx`` ``.js`` ``.jsx``. Regex/line based (no TS compiler), so a
file with syntax errors or an unsupported dialect still yields partial structure
instead of aborting the scan.

The returned dict mirrors :mod:`graphforge.git.parsers.java` — package, imports,
classes, interfaces, enums, methods, annotations — plus an extra ``exports`` key.
Decorators (``@Component`` …) land in ``annotations`` so downstream graph code is
language-agnostic.

Bucketing rules:
    ``class X``                -> ``classes``
    ``interface X`` / ``type`` -> ``interfaces``
    ``enum X``                 -> ``enums``
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

_MODS = r"(?:export\s+)?(?:default\s+)?(?:declare\s+)?"

_IMPORT_FROM = re.compile(
    r"^import\s+(?:type\s+)?(?P<what>[^'\"]*?)\s*from\s*['\"](?P<mod>[^'\"]+)['\"]"
)
_IMPORT_BARE = re.compile(r"^import\s*['\"](?P<mod>[^'\"]+)['\"]")
_EXPORT_FROM = re.compile(
    r"^export\s+(?:type\s+)?(?P<what>[^'\"]*?)\s*from\s*['\"](?P<mod>[^'\"]+)['\"]"
)
_REQUIRE = re.compile(
    r"^(?:const|let|var)\s+(?P<what>[\w{}\s,:*]+?)\s*=\s*require\s*\(\s*['\"](?P<mod>[^'\"]+)['\"]"
)

_CLASS = re.compile(
    _MODS + r"(?:abstract\s+)?class\s+(?P<name>\w+)\s*(?:<[^>]*>)?"
    r"(?:\s+extends\s+(?P<extends>[\w$.]+)\s*(?:<[^>]*>)?)?"
    r"(?:\s+implements\s+(?P<impl>[^{]+?))?\s*\{"
)
_INTERFACE = re.compile(
    _MODS + r"interface\s+(?P<name>\w+)\s*(?:<[^>]*>)?"
    r"(?:\s+extends\s+(?P<extends>[^{]+?))?\s*\{"
)
_ENUM = re.compile(_MODS + r"(?:const\s+)?enum\s+(?P<name>\w+)\s*\{")
_TYPE_ALIAS = re.compile(_MODS + r"type\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*=")
_FUNCTION = re.compile(_MODS + r"(?:async\s+)?function\s*\*?\s*(?P<name>\w+)\s*(?:<[^>]*>)?\s*\(")
_ARROW_CONST = re.compile(
    _MODS + r"(?:const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*(?P<rhs>.+)$"
)
_METHOD = re.compile(
    r"^(?P<mods>(?:public|private|protected|readonly|static|abstract|override|async|get|set|\*|\s)*)"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\((?P<args>[^;]*?)\)\s*"
    r"(?::\s*(?P<ret>[^{;]+?))?\s*(?P<tail>[{;])"
)
_DECORATOR = re.compile(r"^@(?P<name>[\w$.]+)\s*(?:\((?P<args>.*)\))?\s*$")
_EXPORT_NAMES = re.compile(
    r"^export\s+(?:default\s+)?(?:declare\s+)?"
    r"(?:abstract\s+)?(?:async\s+)?"
    r"(?:class|interface|enum|type|function|const|let|var)\s+(?P<name>\w+)"
)
_EXPORT_LIST = re.compile(r"^export\s*\{(?P<names>[^}]*)\}")
_NAME_OPTION = re.compile(r"""name\s*:\s*['"]([^'"]+)['"]""")
_FIRST_STRING = re.compile(r"""['"]([^'"]+)['"]""")

_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "function",
    "do",
    "else",
    "try",
    "new",
    "typeof",
    "await",
    "yield",
    "class",
    "interface",
    "enum",
    "import",
    "export",
    "throw",
    "delete",
    "case",
    "with",
    "of",
    "in",
    "super",
}

# decorator simple-name -> semantic stereotype (must be a valid Cypher label)
_STEREOTYPES = {
    "Component": "Component",
    "Directive": "Component",
    "Pipe": "Component",
    "Injectable": "Service",
    "Service": "Service",
    "NgModule": "NgModule",
    "Module": "NgModule",
    "Entity": "Entity",
    "ViewEntity": "Entity",
    "Controller": "Controller",
    "RestController": "Controller",
}


def extract(lines: list[str]) -> dict[str, Any]:
    """Return the structural summary of a TS/JS source file. Never raises."""
    result: dict[str, Any] = {
        "package": "",
        "imports": [],
        "classes": [],
        "interfaces": [],
        "enums": [],
        "methods": [],
        "annotations": [],
        "exports": [],
    }
    # a malformed file must never abort a repo scan
    with contextlib.suppress(Exception):
        _extract_into(lines, result)
    return result


# ---------------------------------------------------------------------------
def _extract_into(lines: list[str], result: dict[str, Any]) -> None:
    in_block_comment = False
    pending: list[dict[str, str]] = []  # decorators awaiting a declaration
    stack: list[dict[str, Any]] = []  # open type bodies, by brace depth
    depth = 0
    dec_buf: list[str] = []  # a decorator whose args span lines
    dec_line = 0

    for i, raw in enumerate(lines, 1):
        # `code` has string bodies blanked (keywords in strings must not parse as
        # declarations); `text` keeps them, so module paths and decorator options
        # remain readable.
        code, text, in_block_comment = _strip(raw, in_block_comment)
        line, quoted = code.strip(), text.strip()
        if not line:
            continue

        while stack and depth <= stack[-1]["depth"]:
            stack.pop()
        owner = stack[-1]["name"] if stack else ""
        at_member_level = bool(stack) and depth == stack[-1]["depth"] + 1

        if dec_buf or quoted.startswith("@"):
            if not dec_buf:
                dec_line = i
            dec_buf.append(quoted)
            depth += _depth_delta(code)
            joined = " ".join(dec_buf)
            if _paren_delta(joined) > 0:
                continue
            dec_buf = []
            _record_decorator(result, joined, dec_line, owner, pending)
            continue

        _handle_line(result, line, quoted, i, depth, owner, at_member_level, pending, stack)
        pending = []  # a non-decorator line ends the decorator -> declaration bond
        depth += _depth_delta(code)


def _record_decorator(
    result: dict[str, Any], joined: str, line: int, owner: str, pending: list[dict[str, str]]
) -> None:
    dm = _DECORATOR.match(joined)
    if not dm:
        return
    name = dm.group("name")
    pending.append({"name": name, "args": (dm.group("args") or "").strip()})
    result["annotations"].append({"name": name, "line": line, "owner": owner})


def _handle_line(
    result,
    line: str,
    quoted: str,
    i: int,
    depth: int,
    owner: str,
    at_member_level: bool,
    pending: list[dict[str, str]],
    stack: list[dict[str, Any]],
) -> str:
    if _record_imports(result, quoted, i):
        return "import"

    _record_export(result, line)

    cm = _CLASS.search(line)
    if cm:
        info = _type_info(cm.group("name"), "class", i, line, pending)
        if cm.group("extends"):
            info["extends"] = cm.group("extends")
        impl = _split_types(cm.group("impl") or "")
        if impl:
            info["implements"] = impl
        result["classes"].append(info)
        stack.append({"depth": depth, "name": info["name"]})
        return "class"

    im = _INTERFACE.search(line)
    if im:
        info = _type_info(im.group("name"), "interface", i, line, pending)
        bases = _split_types(im.group("extends") or "")
        if bases:
            info["extends"] = bases[0]
            if len(bases) > 1:
                info["implements"] = bases[1:]
        result["interfaces"].append(info)
        stack.append({"depth": depth, "name": info["name"]})
        return "interface"

    em = _ENUM.search(line)
    if em:
        result["enums"].append(_type_info(em.group("name"), "enum", i, line, pending))
        stack.append({"depth": depth, "name": em.group("name")})
        return "enum"

    tm = _TYPE_ALIAS.match(line)
    if tm:
        result["interfaces"].append(_type_info(tm.group("name"), "type", i, line, pending))
        return "type"

    fm = _FUNCTION.search(line)
    if fm:
        result["methods"].append(_method(fm.group("name"), i, line, "", owner, pending))
        return "function"

    am = _ARROW_CONST.match(line)
    if am and _is_function_rhs(am.group("rhs")):
        result["methods"].append(_method(am.group("name"), i, line, "", owner, pending))
        return "arrow"

    if at_member_level:
        mm = _METHOD.match(line)
        if mm and mm.group("name") not in _KEYWORDS:
            result["methods"].append(
                _method(mm.group("name"), i, line, (mm.group("ret") or "").strip(), owner, pending)
            )
            return "method"
    return ""


# ---- helpers --------------------------------------------------------------
def _strip(raw: str, in_block_comment: bool) -> tuple[str, str, bool]:
    """Remove comments; return (code-with-blanked-strings, code-with-strings, state)."""
    blanked: list[str] = []
    kept: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if in_block_comment:
            j = raw.find("*/", i)
            if j == -1:
                return "".join(blanked), "".join(kept), True
            i, in_block_comment = j + 2, False
            continue
        two = raw[i : i + 2]
        if two == "//":
            break
        if two == "/*":
            in_block_comment = True
            i += 2
            continue
        ch = raw[i]
        if ch in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if raw[j] == "\\":
                    j += 2
                    continue
                if raw[j] == ch:
                    break
                j += 1
            blanked.append("''")
            kept.append(raw[i : j + 1])
            i = j + 1
            continue
        blanked.append(ch)
        kept.append(ch)
        i += 1
    return "".join(blanked), "".join(kept), in_block_comment


def _depth_delta(code: str) -> int:
    return code.count("{") - code.count("}")


def _paren_delta(code: str) -> int:
    return code.count("(") - code.count(")")


def _split_types(text: str) -> list[str]:
    """Split an extends/implements clause on top-level commas."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        elif ch == "," and depth <= 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    # drop generic arguments so graph identities stay stable (Repo<Order> -> Repo)
    return [re.sub(r"<.*", "", p).strip() for p in parts if p.strip()]


def _visibility(line: str) -> str:
    if "private " in line:
        return "private"
    if "protected " in line:
        return "protected"
    return "public"


def _type_info(
    name: str, kind: str, line_no: int, line: str, pending: list[dict[str, str]]
) -> dict[str, Any]:
    decorators = [d["name"] for d in pending]
    return {
        "name": name,
        "type": kind,
        "line": line_no,
        "visibility": "public" if "export" in line else _visibility(line),
        "isAbstract": "abstract " in line,
        "isFinal": False,
        "isExported": line.lstrip().startswith("export"),
        "annotations": decorators,
        "stereotype": _stereotype(pending),
        "mappedTable": _mapped_table(pending, name),
    }


def _method(
    name: str, line_no: int, line: str, ret: str, owner: str, pending: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "name": name,
        "line": line_no,
        "returnType": ret,
        "visibility": _visibility(line),
        "isAsync": "async " in line,
        "isStatic": "static " in line,
        "owner": owner,
        "annotations": [d["name"] for d in pending],
    }


def _is_function_rhs(rhs: str) -> bool:
    """True when a `const x = …` right-hand side declares a function."""
    rhs = rhs.strip()
    if rhs.startswith(("function", "async function")):
        return True
    if "=>" not in rhs:
        return False
    head = rhs.split("=>", 1)[0]
    return bool(re.match(r"^(?:async\s+)?(?:\(|<|[A-Za-z_$][\w$]*\s*$)", head.strip()))


def _stereotype(pending: list[dict[str, str]]) -> str:
    for ann in pending:
        hit = _STEREOTYPES.get(ann["name"].split(".")[-1])
        if hit:
            return hit
    return ""


def _mapped_table(pending: list[dict[str, str]], name: str) -> str:
    """TypeORM: ``@Entity('orders')`` or ``@Entity({ name: 'orders' })``."""
    for ann in pending:
        if ann["name"].split(".")[-1] not in ("Entity", "ViewEntity"):
            continue
        args = ann["args"]
        m = _NAME_OPTION.search(args) or _FIRST_STRING.search(args)
        return m.group(1) if m else name
    return ""


def _record_imports(result: dict[str, Any], line: str, i: int) -> bool:
    for pattern, kind in (
        (_IMPORT_FROM, "import"),
        (_EXPORT_FROM, "re-export"),
        (_REQUIRE, "require"),
    ):
        m = pattern.match(line)
        if m:
            _add_import(result, m.group("mod"), m.group("what"), i, kind)
            if kind == "re-export":
                result["exports"] += _binding_names(m.group("what"))
            return True
    m = _IMPORT_BARE.match(line)
    if m:
        _add_import(result, m.group("mod"), "", i, "side-effect")
        return True
    return False


def _add_import(result: dict[str, Any], module: str, what: str, i: int, kind: str) -> None:
    names = _binding_names(what)
    if not names:
        result["imports"].append(
            {"fqn": module, "module": module, "name": "", "line": i, "kind": kind}
        )
        return
    for name in names:
        result["imports"].append(
            {"fqn": f"{module}/{name}", "module": module, "name": name, "line": i, "kind": kind}
        )


def _binding_names(what: str) -> list[str]:
    """Pull the bound identifiers out of an import/export clause."""
    what = (what or "").strip()
    if not what or what == "*":
        return []
    names: list[str] = []
    for chunk in re.split(r"[{},]", what.replace("* as ", "")):
        token = chunk.strip()
        if not token or token in ("type", "*"):
            continue
        token = re.split(r"\s+as\s+", token)[-1].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", token) and token != "default":
            names.append(token)
    return names


def _record_export(result: dict[str, Any], line: str) -> None:
    m = _EXPORT_NAMES.match(line)
    if m:
        result["exports"].append(m.group("name"))
        return
    m = _EXPORT_LIST.match(line)
    if m:
        result["exports"] += _binding_names(m.group("names"))
    elif re.match(r"^export\s+default\b", line):
        result["exports"].append("default")
