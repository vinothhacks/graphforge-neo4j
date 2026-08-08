"""Lightweight structural parser for Python source.

Regex/line based (no ``ast``) so that syntactically broken or Python-2 files
still yield partial structure instead of blowing up the whole scan. It extracts
the module docstring, imports (``import x`` and ``from x import y``), classes
with their bases, functions/methods (including ``async def``) and decorators.

The returned dict mirrors :mod:`graphforge.git.parsers.java` exactly — package,
imports, classes, interfaces, enums, methods, annotations — so ``git.ingest``
maps every language through one code path. Decorators land in ``annotations``
(the Java parser's key for the same concept).

Bucketing rules:
    ``class X(Enum)``               -> ``enums``
    ``class X(Protocol)`` / ``ABC`` -> ``interfaces``
    everything else                 -> ``classes``
"""
from __future__ import annotations

import contextlib
import re
from typing import Any

_CLASS = re.compile(r"^class\s+(?P<name>\w+)\s*(?:\((?P<bases>.*?)\))?\s*:")
_DEF = re.compile(r"^(?P<async>async\s+)?def\s+(?P<name>\w+)\s*\(")
_DECORATOR = re.compile(r"^@\s*(?P<name>[\w.]+)\s*(?:\((?P<args>.*)\))?\s*$")
_IMPORT = re.compile(r"^import\s+(?P<body>.+)$")
_FROM_IMPORT = re.compile(r"^from\s+(?P<mod>[.\w]+)\s+import\s+(?P<names>.+)$")
_ARROW = re.compile(r"\)\s*->\s*")
_TABLENAME = re.compile(r"""^__tablename__\s*=\s*['"]([^'"]+)['"]""")
_DB_TABLE = re.compile(r"""^db_table\s*=\s*['"]([^'"]+)['"]""")
_TRIPLE_START = re.compile(r"^(?:[rRbBuUfF]{0,3})(?P<q>\"\"\"|''')")

# Bases that make a class an "enum" / "interface" rather than a plain class.
_ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum"}
_INTERFACE_BASES = {"Protocol", "ABC", "ABCMeta"}
# Bases that mark an ORM-mapped entity (SQLAlchemy / Django / Flask-SQLAlchemy).
_ENTITY_BASES = {"Model", "Base", "DeclarativeBase"}
# Decorator simple-name -> semantic stereotype (must be a valid Cypher label).
_STEREOTYPES = {"dataclass": "DataClass", "define": "DataClass", "attrs": "DataClass"}


def extract(lines: list[str]) -> dict[str, Any]:
    """Return the structural summary of a Python source file. Never raises."""
    result: dict[str, Any] = {
        "package": "", "imports": [], "classes": [], "interfaces": [],
        "enums": [], "methods": [], "annotations": [], "docstring": "",
    }
    # a malformed file must never abort a repo scan
    with contextlib.suppress(Exception):
        _extract_into(lines, result)
    return result


# ---------------------------------------------------------------------------
def _extract_into(lines: list[str], result: dict[str, Any]) -> None:
    result["docstring"] = _module_docstring(lines)

    pending: list[dict[str, str]] = []   # decorators awaiting a declaration
    stack: list[dict[str, Any]] = []     # enclosing scopes, keyed by indent

    for lineno, code, raw, indent in _logical_lines(lines):
        while stack and indent <= stack[-1]["indent"]:
            stack.pop()
        owner = _nearest_type(stack)

        # ORM table names live in the class body, so handle them before decls.
        if stack:
            table = _table_name(raw.strip())
            if table and _apply_mapped_table(result, stack, table):
                continue

        dm = _DECORATOR.match(code)
        if dm:
            name = dm.group("name")
            pending.append({"name": name, "args": (dm.group("args") or "").strip()})
            result["annotations"].append({"name": name, "line": lineno, "owner": owner})
            continue

        cm = _CLASS.match(code)
        if cm:
            info = _class_info(cm, lineno, pending)
            if info["name"] == "Meta" and stack:
                # Django's `class Meta:` is configuration, not a type of its own
                stack.append(_scope(indent, info["name"], None, -1, stack[-1]))
            else:
                bucket = _bucket_for(info["bases"])
                result[bucket].append(info)
                stack.append(_scope(indent, info["name"], bucket, len(result[bucket]) - 1))
            pending = []
            continue

        dfm = _DEF.match(code)
        if dfm:
            result["methods"].append({
                "name": dfm.group("name"),
                "line": lineno,
                "returnType": _return_type(code),
                "visibility": _visibility(dfm.group("name")),
                "isAsync": bool(dfm.group("async")),
                "owner": owner,
                "annotations": [d["name"] for d in pending],
            })
            stack.append(_scope(indent, dfm.group("name"), None, -1))
            pending = []
            continue

        fim = _FROM_IMPORT.match(code)
        if fim:
            result["imports"] += _from_imports(fim, lineno)
            pending = []
            continue

        im = _IMPORT.match(code)
        if im:
            result["imports"] += _plain_imports(im.group("body"), lineno)
            pending = []
            continue

        pending = []  # any other statement breaks the decorator -> declaration bond


# ---- line normalisation ---------------------------------------------------
def _logical_lines(lines: list[str]):
    """Yield ``(lineno, code, raw, indent)`` with bracket continuations joined.

    ``code`` has comments removed and string bodies blanked (so keywords inside
    docstrings are never parsed as declarations); ``raw`` is the original text of
    the first physical line, kept for value extraction (``__tablename__ = ...``).
    """
    open_triple: str | None = None
    buf_code: list[str] = []
    buf_start = 0
    buf_raw = ""
    buf_indent = 0
    depth = 0
    continued = False

    for lineno, raw in enumerate(lines, 1):
        code, open_triple = _strip(raw, open_triple)
        if not buf_code and not code.strip():
            continued = False
            continue
        if not buf_code:
            buf_start, buf_raw, buf_indent = lineno, raw, _indent_of(raw)
        buf_code.append(code.strip())
        depth += _depth_delta(code)
        continued = code.rstrip().endswith("\\")
        if depth > 0 or continued:
            continue
        text = " ".join(p for p in buf_code if p).replace("\\", " ")
        buf_code = []
        depth = 0
        if text.strip():
            yield buf_start, text.strip(), buf_raw, buf_indent
    if buf_code:
        text = " ".join(p for p in buf_code if p).replace("\\", " ")
        if text.strip():
            yield buf_start, text.strip(), buf_raw, buf_indent


def _depth_delta(code: str) -> int:
    return sum(1 for c in code if c in "([{") - sum(1 for c in code if c in ")]}")


def _indent_of(raw: str) -> int:
    expanded = raw.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _strip(raw: str, open_triple: str | None) -> tuple[str, str | None]:
    """Blank out string literals and comments; track open triple-quoted strings."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if open_triple:
            j = raw.find(open_triple, i)
            if j == -1:
                return "".join(out), open_triple
            i, open_triple = j + 3, None
            continue
        ch = raw[i]
        if ch == "#":
            break
        if raw.startswith('"""', i) or raw.startswith("'''", i):
            open_triple = raw[i:i + 3]
            i += 3
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if raw[j] == "\\":
                    j += 2
                    continue
                if raw[j] == ch:
                    break
                j += 1
            out.append('""')  # placeholder keeps `= "x"` shaped like an assignment
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), open_triple


def _module_docstring(lines: list[str]) -> str:
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _TRIPLE_START.match(stripped)
        if not m:
            return ""
        quote = m.group("q")
        body = stripped[m.end():]
        end = body.find(quote)
        return (body if end == -1 else body[:end]).strip()
    return ""


# ---- declaration helpers --------------------------------------------------
def _scope(indent: int, name: str, bucket: str | None, index: int,
           outer: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"indent": indent, "name": name, "bucket": bucket,
            "index": index, "outer": outer}


def _nearest_type(stack: list[dict[str, Any]]) -> str:
    for scope in reversed(stack):
        if scope.get("bucket"):
            return scope["name"]
    return stack[-1]["name"] if stack else ""


def _visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "public"          # dunder protocol methods are part of the API
    if name.startswith("__"):
        return "private"         # name-mangled
    if name.startswith("_"):
        return "protected"       # conventionally internal
    return "public"


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not nested inside brackets."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth <= 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _class_info(match: re.Match, line: int, pending: list[dict[str, str]]) -> dict[str, Any]:
    bases = [b for b in _split_top_level(match.group("bases") or "") if "=" not in b]
    decorators = [d["name"] for d in pending]
    info: dict[str, Any] = {
        "name": match.group("name"),
        "type": "class",
        "line": line,
        "visibility": _visibility(match.group("name")),
        "isAbstract": any(_simple(b) in _INTERFACE_BASES for b in bases),
        "isFinal": any(d.split(".")[-1] == "final" for d in decorators),
        "annotations": decorators,
        "bases": bases,
        "stereotype": _stereotype(bases, decorators),
        "mappedTable": "",
    }
    if bases:
        info["extends"] = bases[0]
        if len(bases) > 1:
            info["implements"] = bases[1:]
    return info


def _simple(name: str) -> str:
    return name.split("[")[0].split(".")[-1].strip()


def _bucket_for(bases: list[str]) -> str:
    simple = {_simple(b) for b in bases}
    if simple & _ENUM_BASES:
        return "enums"
    if simple & _INTERFACE_BASES:
        return "interfaces"
    return "classes"


def _stereotype(bases: list[str], decorators: list[str]) -> str:
    if {_simple(b) for b in bases} & _ENTITY_BASES:
        return "Entity"
    for dec in decorators:
        hit = _STEREOTYPES.get(dec.split(".")[-1])
        if hit:
            return hit
    return ""


def _table_name(stripped: str) -> str:
    """Recognise a SQLAlchemy / Django table-name assignment on a class-body line."""
    for pattern in (_TABLENAME, _DB_TABLE):
        m = pattern.match(stripped)
        if m:
            return m.group(1)
    return ""


def _apply_mapped_table(result: dict[str, Any], stack: list[dict[str, Any]], table: str) -> bool:
    for scope in reversed(stack):
        target = scope.get("outer") or scope
        if target.get("bucket") and target.get("index", -1) >= 0:
            entry = result[target["bucket"]][target["index"]]
            entry["mappedTable"] = table
            entry["stereotype"] = entry.get("stereotype") or "Entity"
            return True
    return False


def _return_type(code: str) -> str:
    """Read the annotation between ``->`` and the signature's closing colon."""
    m = _ARROW.search(code)
    if not m:
        return ""
    depth = 0
    out: list[str] = []
    for ch in code[m.end():]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth <= 0:
            break
        out.append(ch)
    # string bodies are blanked upstream, so `-> "Order"` arrives as `-> ""`
    return "".join(out).replace('""', "").strip()


def _plain_imports(body: str, line: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in _split_top_level(body):
        chunk = part.split(" as ")
        fqn = chunk[0].strip()
        if not fqn:
            continue
        out.append({"fqn": fqn, "line": line, "kind": "import",
                    "alias": chunk[1].strip() if len(chunk) > 1 else ""})
    return out


def _from_imports(match: re.Match, line: int) -> list[dict[str, Any]]:
    module = match.group("mod")
    names = match.group("names").strip().lstrip("(").rstrip(")").strip()
    if names == "*" or not names:
        return [{"fqn": module, "line": line, "kind": "from", "alias": ""}]
    out: list[dict[str, Any]] = []
    for part in _split_top_level(names):
        chunk = part.split(" as ")
        name = chunk[0].strip()
        if not name:
            continue
        sep = "" if module.endswith(".") else "."
        out.append({"fqn": f"{module}{sep}{name}", "line": line, "kind": "from",
                    "alias": chunk[1].strip() if len(chunk) > 1 else ""})
    return out
