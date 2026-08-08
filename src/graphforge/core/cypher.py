"""Graph operations expressed as parameterized Cypher.

An :class:`Operation` carries a parameterized Cypher statement plus its params.
The Neo4j driver executes it directly (safe, no string-injection). For
``--emit-cypher`` (script) output the same operation is rendered to literal
Cypher via :meth:`Operation.to_script`, so the generated ``.cypher`` file can be
replayed in cypher-shell or the Neo4j Browser.

Using MERGE everywhere keeps ingestion idempotent: re-running an import updates
existing nodes instead of duplicating them.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Literal rendering (only used for script output; the driver uses parameters)
# ---------------------------------------------------------------------------

def escape_cypher_string(value: str | None) -> str:
    """Escape a Python string for use inside a single-quoted Cypher literal."""
    if value is None:
        return ""
    value = value.replace("\\", "\\\\")
    value = value.replace("'", "\\'")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    value = value.replace("\t", "\\t")
    return value


def lit(value: Any) -> str:
    """Render a Python value as a Cypher literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(lit(v) for v in value) + "]"
    if isinstance(value, Mapping):
        inner = ", ".join(f"{k}: {lit(v)}" for k, v in value.items())
        return "{" + inner + "}"
    return "'" + escape_cypher_string(str(value)) + "'"


# ---------------------------------------------------------------------------
# Operation model
# ---------------------------------------------------------------------------

@dataclass
class Operation:
    """A single parameterized Cypher statement plus its parameters."""

    cypher: str
    params: dict[str, Any] = field(default_factory=dict)
    comment: str = ""

    def to_script(self) -> str:
        """Render as standalone literal Cypher (for .cypher file output)."""
        statement = self.cypher
        # Substitute longest parameter names first so `$p_id` is not clobbered
        # while replacing `$p_identifier`.
        for name in sorted(self.params, key=len, reverse=True):
            pattern = re.compile(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])")
            statement = pattern.sub(lambda _m, v=self.params[name]: lit(v), statement)
        prefix = f"// {self.comment}\n" if self.comment else ""
        return f"{prefix}{statement};"


@dataclass(frozen=True)
class NodeRef:
    """Identifies a node by label and a MERGE key (one or more properties)."""

    label: str
    key: Mapping[str, Any]


def _ident(name: str) -> str:
    """Validate a property/label identifier (defence-in-depth for f-strings)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def _label(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"unsafe label: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def merge_node(
    label: str,
    key: Mapping[str, Any],
    props: Mapping[str, Any] | None = None,
    comment: str = "",
) -> Operation:
    """MERGE a node on ``key`` and SET the remaining ``props``."""
    _label(label)
    key_frag = ", ".join(f"{_ident(k)}: $k_{k}" for k in key)
    params: dict[str, Any] = {f"k_{k}": v for k, v in key.items()}
    cypher = f"MERGE (n:{label} {{{key_frag}}})"
    if props:
        set_frag = ", ".join(f"n.{_ident(k)} = $p_{k}" for k in props)
        params.update({f"p_{k}": v for k, v in props.items()})
        cypher += f"\nSET {set_frag}"
    return Operation(cypher, params, comment)


def set_label(node: NodeRef, label: str, comment: str = "") -> Operation:
    """MATCH a node by its key and add a secondary label (e.g. :Class:Entity)."""
    _label(node.label)
    _label(label)
    key_frag = ", ".join(f"{_ident(k)}: $k_{k}" for k in node.key)
    params = {f"k_{k}": v for k, v in node.key.items()}
    cypher = f"MATCH (n:{node.label} {{{key_frag}}})\nSET n:{label}"
    return Operation(cypher, params, comment)


def merge_rel(
    start: NodeRef,
    rel_type: str,
    end: NodeRef,
    props: Mapping[str, Any] | None = None,
    comment: str = "",
) -> Operation:
    """MATCH two nodes and MERGE a ``rel_type`` relationship between them."""
    _label(start.label)
    _label(end.label)
    _label(rel_type)
    a_frag = ", ".join(f"{_ident(k)}: $a_{k}" for k in start.key)
    b_frag = ", ".join(f"{_ident(k)}: $b_{k}" for k in end.key)
    params: dict[str, Any] = {f"a_{k}": v for k, v in start.key.items()}
    params.update({f"b_{k}": v for k, v in end.key.items()})
    cypher = (
        f"MATCH (a:{start.label} {{{a_frag}}})\n"
        f"MATCH (b:{end.label} {{{b_frag}}})\n"
        f"MERGE (a)-[r:{rel_type}]->(b)"
    )
    if props:
        set_frag = ", ".join(f"r.{_ident(k)} = $r_{k}" for k in props)
        params.update({f"r_{k}": v for k, v in props.items()})
        cypher += f"\nSET {set_frag}"
    return Operation(cypher, params, comment)
