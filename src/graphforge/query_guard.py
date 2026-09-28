"""Read-only Cypher gate used by MCP ``read_cypher`` and ``POST /api/query``.

Security is **not** a substring match on the raw query text (that false-positives
on ``n.name = 'CREATE'`` and ``:Create``), and it is no longer a set of regexes
over masked text either: the masker, the regexes and Neo4j each had their own
idea of where a string or a quoted name ended, and every disagreement was a way
to hide code as data. Every check here runs on the tokens of one small lexer
that follows Neo4j's rules: a backslash escapes the next character inside
``'...'`` and ``"..."`` (a doubled quote is two strings, not an escape), a doubled
backtick is a backtick inside a quoted name, ``//`` runs to the next carriage
return or line feed, and ``/* */`` does not nest. An unterminated literal, or a
character Cypher has no use for, is a denial rather than a guess.

Layers:

1. Procedure allowlist — deny-by-default, including every ``apoc.*``. A ``CALL``
   that does not open a subquery must be followed by a dotted name (plain or
   backtick-quoted segments, comments allowed between them), and that name must
   be allowlisted.
2. Function namespaces — Neo4j will not load a user-defined function into the
   root namespace, so a namespaced call is plugin code (``apoc.cypher.run*``
   runs nested Cypher) unless the namespace is one of Neo4j's own. Un-namespaced
   functions (``count``, ``toLower``) are always built in.
3. Clause denies — write clauses anywhere; ``LOAD CSV`` anywhere; ``USE``, ``SHOW``
   and the other administration commands wherever a clause can begin (past any
   ``EXPLAIN`` / ``PROFILE`` / ``CYPHER ...`` prefix, after ``UNION`` or ``NEXT``,
   at the top of a subquery or just past its importing ``WITH``, at each branch
   of a conditional ``WHEN ... THEN ... ELSE`` query); a second statement. A
   keyword used as a name — ``n.show``, ``(show:Show)``, ``{load: 1}`` — is not a
   clause.
4. Hard cap on query length and on a literal ``LIMIT``.
5. Callers then run the query in a Neo4j **read** transaction (timeout).

Not enforced here: this is a lexer, not a parser, so an admitted query can still
be one Neo4j rejects; and a ``LIMIT`` written as a parameter or an expression is
not evaluated (callers truncate the rows instead). Where the lexer cannot tell a
clause from a value it assumes a clause, so a few reads are over-denied: in a
conditional query every ``THEN`` / ``ELSE`` counts as a branch, even one in a
``CASE`` whose value is a variable called ``start``. The read transaction is the
backstop for anything layers 1–4 miss.

A denied query must not open a connection (layers 1–4 run first).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

#: Procedures ``read_cypher`` / ``POST /api/query`` may invoke. Unknown = denied.
READ_PROCEDURES = frozenset(
    {
        "db.labels",
        "db.relationshiptypes",
        "db.propertykeys",
    }
)

#: Namespaces of Neo4j's own functions: ``date.truncate``, ``datetime.fromepoch``,
#: ``duration.between``, ``point.distance``, ``vector.similarity.cosine`` and the
#: ``*.statement`` / ``*.transaction`` / ``*.realtime`` clocks. ``db`` and ``graph``
#: are left out on purpose: the first is where procedures live, and
#: ``graph.byName`` only means something inside ``USE``.
BUILTIN_FUNCTION_NAMESPACES = frozenset(
    {
        "date",
        "datetime",
        "localdatetime",
        "localtime",
        "time",
        "duration",
        "point",
        "vector.similarity",
    }
)

MAX_QUERY_CHARS = 8000
MAX_READ_LIMIT = 500
READ_TX_TIMEOUT_SECONDS = 30.0

_DENIED_PREFIX = "read-only query: "

#: Clauses that change the graph or the schema. ``INSERT`` is GQL's ``CREATE``.
_WRITE_KEYWORDS = frozenset(
    {"CREATE", "MERGE", "SET", "REMOVE", "DELETE", "DETACH", "DROP", "INSERT"}
)
#: Administration commands. Neo4j only accepts them at the start of a statement.
_ADMIN_KEYWORDS = frozenset(
    {
        "SHOW",
        "TERMINATE",
        "ALTER",
        "RENAME",
        "GRANT",
        "DENY",
        "REVOKE",
        "START",
        "STOP",
        "ENABLE",
        "DEALLOCATE",
        "REALLOCATE",
        "DRYRUN",
    }
)
#: Keywords after which a new query part — and so a ``USE`` — may begin.
_PART_SEPARATORS = frozenset({"UNION", "NEXT"})
#: Keywords that begin a branch of a conditional query, and only of one: in a
#: ``CASE`` expression they are followed by a value.
_BRANCH_SEPARATORS = frozenset({"THEN", "ELSE"})
#: Keywords after which ``{`` opens a subquery body rather than a map.
_SUBQUERY_OPENERS = frozenset({"CALL", "EXISTS", "COUNT", "COLLECT"})

# Token kinds. The names double as the wording of an "unterminated ..." denial.
_SPACE = "whitespace"
_COMMENT = "comment"
_STRING = "string literal"
_QUOTED = "quoted identifier"
_PARAM = "parameter"
_NUMBER = "number"
_IDENT = "identifier"
_PUNCT = "punctuation"
_UNKNOWN = "unknown"

_TRIVIA = frozenset({_SPACE, _COMMENT})
#: What :func:`mask_query` blanks: everything the user writes as data, not code.
_DATA = frozenset({_STRING, _COMMENT, _QUOTED})
_NAMES = frozenset({_IDENT, _QUOTED})
_OPTION_VALUES = _NAMES | {_NUMBER, _STRING}

#: Unicode dashes and angle brackets Neo4j accepts in relationship arrows, by code
#: point because some of them (the soft hyphen) are invisible in source.
_ARROW_CODE_POINTS = (
    *(0x00AD, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D),
    *(0x27E8, 0x3008, 0xFE64, 0xFF1C, 0x27E9, 0x3009, 0xFE65, 0xFF1E),
)
#: Operator and bracket characters Cypher uses. Anything else outside a literal is
#: a character the guard cannot vouch for, so it is refused.
_PUNCTUATION = frozenset("()[]{}<>=+-*/%^.,:;|!&?$~") | {chr(c) for c in _ARROW_CODE_POINTS}


class _Token(NamedTuple):
    kind: str
    text: str
    #: False for a string, comment or quoted identifier that runs off the end.
    closed: bool = True


def check_read_query(cypher: str, limit: int | None = None) -> str | None:
    """Return a denial reason, or ``None`` if layers 1–4 accept the query."""
    text = cypher if isinstance(cypher, str) else ""
    if not text.strip():
        return _DENIED_PREFIX + "empty query"
    if len(text) > MAX_QUERY_CHARS:
        return _DENIED_PREFIX + f"query too long (max {MAX_QUERY_CHARS} characters)"

    tokens = _tokenize(text)
    for tok in tokens:
        if not tok.closed:
            return _DENIED_PREFIX + f"unterminated {tok.kind}"
        if tok.kind == _UNKNOWN:
            return _DENIED_PREFIX + f"unexpected character {tok.text!r}"

    statement = _single_statement(tokens)
    if statement is None:
        return _DENIED_PREFIX + "multi-statement queries are not allowed"
    code = [tok for tok in statement if tok.kind not in _TRIVIA]
    start = _statement_start(code)
    if start >= len(code):
        return _DENIED_PREFIX + "empty query"

    return (
        _denied_clause(code, start)
        or _denied_procedure(code)
        or _denied_function(code)
        or _denied_limit(code, limit)
    )


def is_denied(cypher: str, limit: int | None = None) -> bool:
    """True when layers 1–4 reject the query (no connection should be opened)."""
    return check_read_query(cypher, limit) is not None


def clamp_read_limit(limit: int | None, default: int = 200) -> int:
    try:
        n = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_READ_LIMIT))


def mask_query(cypher: str) -> str:
    """Blank strings, comments and backtick-quoted identifiers, keeping every offset.

    Built on the guard's own lexer, so a keyword regex run over the result sees
    the code Neo4j sees and none of the data: a ``LIMIT`` inside a string or a
    quoted name is gone, a real one is where it was. Length and line breaks are
    preserved; an unterminated literal is blanked to the end.
    """
    return "".join(_blank(tok.text) if tok.kind in _DATA else tok.text for tok in _tokenize(cypher))


# -- checks over the statement's code tokens (trivia already removed) -------------


def _denied_clause(code: list[_Token], start: int) -> str | None:
    """``USE`` / administration commands where a clause begins; ``LOAD CSV`` and writes anywhere."""
    for i in _clause_starts(code, start):
        word = _keyword_at(code, i)
        if not word or _is_name(code, i):
            continue
        if word == "USE":
            return _DENIED_PREFIX + "USE <database> is not allowed"
        if word in _ADMIN_KEYWORDS:
            return _DENIED_PREFIX + f"{word} is not allowed"
    for i, tok in enumerate(code):
        word = _keyword(tok)
        if not word or _is_name(code, i):
            continue
        if word == "LOAD" and _keyword_at(code, i + 1) == "CSV":
            return _DENIED_PREFIX + "LOAD CSV is not allowed"
        if word in _WRITE_KEYWORDS:
            return _DENIED_PREFIX + f"{word} is not allowed"
    return None


def _denied_procedure(code: list[_Token]) -> str | None:
    """Deny by default: every ``CALL`` opens a subquery or names an allowlisted procedure."""
    for i, tok in enumerate(code):
        if _keyword(tok) != "CALL" or _is_name(code, i) or _opens_subquery(code, i + 1):
            continue
        segments, _ = _dotted_name(code, i + 1)
        if not segments:
            return _DENIED_PREFIX + "CALL target is not a plain procedure name"
        name = ".".join(segments)
        if name.lower() not in READ_PROCEDURES:
            return _DENIED_PREFIX + f"procedure {name} is not allowlisted"
    return None


def _denied_function(code: list[_Token]) -> str | None:
    """Deny a namespaced function call unless the namespace is Neo4j's own."""
    for i, tok in enumerate(code):
        if tok.kind not in _NAMES or _punct_at(code, i - 1, "."):
            continue  # not the first segment of a name
        if _keyword_at(code, i - 1) == "CALL" and not _is_name(code, i - 1):
            continue  # a procedure name, already held to the allowlist
        segments, end = _dotted_name(code, i)
        if not _punct_at(code, end, "("):
            continue
        # Split on every dot, including one inside a quoted segment: `apoc.x`(...)
        # must not count as a root-namespace call.
        name = ".".join(segments)
        namespace = name.rpartition(".")[0]
        if "." in name and namespace.lower() not in BUILTIN_FUNCTION_NAMESPACES:
            return _DENIED_PREFIX + f"function {name} is not allowed (plugin function)"
    return None


def _denied_limit(code: list[_Token], limit: int | None) -> str | None:
    if limit is not None:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = MAX_READ_LIMIT
        if n > MAX_READ_LIMIT:
            return _DENIED_PREFIX + f"limit exceeds {MAX_READ_LIMIT}"
    for i, tok in enumerate(code):
        if _keyword(tok) != "LIMIT" or _is_name(code, i) or _kind_at(code, i + 1) != _NUMBER:
            continue
        value = _integer(code[i + 1].text)
        if value is not None and value > MAX_READ_LIMIT:
            return _DENIED_PREFIX + f"LIMIT exceeds {MAX_READ_LIMIT}"
    return None


def _single_statement(tokens: list[_Token]) -> list[_Token] | None:
    """Tokens before the first ``;``, or ``None`` when another statement follows it.

    Only whitespace and further ``;`` may trail. A trailing comment counts as
    more: callers strip ``;`` off the end and append a ``LIMIT``, so a comment
    there would leave the ``;`` stranded mid-query. Any whitespace is accepted,
    carriage returns and tabs included, so a caller that appends must strip all
    of it, not just spaces and line feeds.
    """
    for i, tok in enumerate(tokens):
        if _is_punct(tok, ";"):
            rest = tokens[i + 1 :]
            if all(t.kind == _SPACE or _is_punct(t, ";") for t in rest):
                return tokens[:i]
            return None
    return tokens


def _statement_start(code: list[_Token]) -> int:
    """Index of the first clause, past ``EXPLAIN``, ``PROFILE`` and ``CYPHER`` prefixes.

    ``CYPHER`` takes an optional version (``5``, ``25``) and ``key=value`` options
    (``runtime=slotted``), in any order with the other two.
    """
    i = 0
    while True:
        word = _keyword_at(code, i)
        if word in ("EXPLAIN", "PROFILE"):
            i += 1
        elif word == "CYPHER":
            i += 1
            if _kind_at(code, i) == _NUMBER:
                i += 1
            while (
                _kind_at(code, i) in _NAMES
                and _punct_at(code, i + 1, "=")
                and _kind_at(code, i + 2) in _OPTION_VALUES
            ):
                i += 3
        else:
            return i


def _clause_starts(code: list[_Token], start: int) -> list[int]:
    """Where a clause can begin: the head of every query part, and past its importing ``WITH``.

    A part is the statement, what follows ``UNION`` / ``NEXT``, a subquery body,
    or a ``{ ... }`` standing where a part begins. ``THEN`` / ``ELSE`` begin one
    only in a conditional query: in a ``CASE`` they are followed by a value, which
    may be a variable called ``start``. Counting ``CASE`` / ``END`` cannot tell the
    two apart, because Neo4j accepts both as variable names (``(case)-->(end)``).
    So the test is per brace level: once a part there begins with ``WHEN``, every
    ``THEN`` / ``ELSE`` at that level counts, a ``CASE``'s included. That can only
    over-report.
    """
    starts: set[int] = set()
    conditional = [False]  # per brace level: has a part there begun with WHEN?

    def begin(j: int) -> None:
        for k in (j, _past_importing_with(code, j)):
            starts.add(k)
            if _keyword_at(code, k) == "WHEN" and not _is_name(code, k):
                conditional[-1] = True

    begin(start)
    for i in range(start, len(code)):
        tok = code[i]
        word = "" if _is_name(code, i) else _keyword(tok)
        if word in _PART_SEPARATORS:
            nxt = i + 1
            if word == "UNION" and _keyword_at(code, nxt) in ("ALL", "DISTINCT"):
                nxt += 1
            begin(nxt)
        elif word in _BRANCH_SEPARATORS and conditional[-1]:
            begin(i + 1)
        elif _is_punct(tok, "{"):
            opens_part = (
                i in starts
                or _keyword_at(code, i - 1) in _SUBQUERY_OPENERS
                or _punct_at(code, i - 1, ")")
            )
            conditional.append(False)
            if opens_part:
                begin(i + 1)
        elif _is_punct(tok, "}") and len(conditional) > 1:
            conditional.pop()
    return sorted(starts)


def _past_importing_with(code: list[_Token], i: int) -> int:
    """Index just past an importing ``WITH`` at ``code[i]``, else ``i``.

    A ``CALL { }`` body may import variables before its ``USE``, and Neo4j lets
    only simple references do it. Each item is ``*`` or a variable, in any number
    of parentheses, optionally ``AS`` a name: ``WITH *, (a), b AS b``.
    """
    if _keyword_at(code, i) != "WITH" or _is_name(code, i):
        return i
    j = i + 1
    while True:
        if _punct_at(code, j, "*"):
            j += 1
        else:
            depth = 0
            while _punct_at(code, j, "("):
                j, depth = j + 1, depth + 1
            if _kind_at(code, j) not in _NAMES:
                return i
            j += 1
            while depth and _punct_at(code, j, ")"):
                j, depth = j + 1, depth - 1
            if depth:
                return i
        if _keyword_at(code, j) == "AS" and _kind_at(code, j + 1) in _NAMES:
            j += 2
        if not _punct_at(code, j, ","):
            return j
        j += 1


def _opens_subquery(code: list[_Token], i: int) -> bool:
    """True when ``CALL`` opens a subquery: ``{``, or a scope clause ``(a, b) {`` / ``(*) {``."""
    if _punct_at(code, i, "("):
        i += 1
        if _punct_at(code, i, "*"):
            i += 1
        else:
            while _kind_at(code, i) in _NAMES:
                i += 1
                if not _punct_at(code, i, ","):
                    break
                i += 1
        if not _punct_at(code, i, ")"):
            return False
        i += 1
    return _punct_at(code, i, "{")


def _dotted_name(code: list[_Token], i: int) -> tuple[list[str], int]:
    """Read ``a.b.c`` from ``code[i]``; any segment may be backtick-quoted.

    Returns the segments (none when ``code[i]`` is not a name) and the index just
    past the name.
    """
    segments: list[str] = []
    while _kind_at(code, i) in _NAMES:
        tok = code[i]
        segments.append(tok.text[1:-1].replace("``", "`") if tok.kind == _QUOTED else tok.text)
        i += 1
        if not (_punct_at(code, i, ".") and _kind_at(code, i + 1) in _NAMES):
            break
        i += 1
    return segments, i


def _is_name(code: list[_Token], i: int) -> bool:
    """True when a keyword-shaped token is a name, as in ``n.show``, ``{show: 1}``, ``(show:Show)``.

    Neo4j reads any word after ``.`` or ``:`` as a property, label or type, and no
    clause keyword is ever followed by ``:``.
    """
    return _punct_at(code, i - 1, ".") or _punct_at(code, i - 1, ":") or _punct_at(code, i + 1, ":")


def _keyword(tok: _Token) -> str:
    """The word upper-cased, or ``""``. Keywords are ASCII and never backtick-quoted."""
    return tok.text.upper() if tok.kind == _IDENT and tok.text.isascii() else ""


def _keyword_at(code: list[_Token], i: int) -> str:
    return _keyword(code[i]) if 0 <= i < len(code) else ""


def _kind_at(code: list[_Token], i: int) -> str | None:
    return code[i].kind if 0 <= i < len(code) else None


def _punct_at(code: list[_Token], i: int, text: str) -> bool:
    return 0 <= i < len(code) and _is_punct(code[i], text)


def _is_punct(tok: _Token, text: str) -> bool:
    return tok.kind == _PUNCT and tok.text == text


def _integer(text: str) -> int | None:
    """Value of an integer literal (``500``, ``1_000``, ``0x1F4``, ``0o764``), else ``None``."""
    digits = text.replace("_", "").lower()
    base = 16 if digits.startswith("0x") else 8 if digits.startswith("0o") else 10
    try:
        return int(digits, base)
    except ValueError:
        return None


def _blank(text: str) -> str:
    return "".join(c if c in "\r\n" else " " for c in text)


# -- the lexer -------------------------------------------------------------------


def _tokenize(text: str) -> list[_Token]:
    """Split Cypher into tokens the way Neo4j's lexer does.

    Joining the token texts gives back ``text`` exactly. Where Neo4j would lex a
    malformed number such as ``1abc`` as one token this splits it, so a keyword
    can only ever be over-reported, never hidden.
    """
    tokens: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch, nxt = text[i], text[i + 1 : i + 2]
        kind, closed = _PUNCT, True
        if ch.isspace():
            kind, end = _SPACE, _scan_while(text, i, str.isspace)
        elif ch == "/" and nxt == "/":
            kind, end = _COMMENT, _scan_while(text, i, lambda c: c not in "\r\n")
        elif ch == "/" and nxt == "*":
            stop = text.find("*/", i + 2)
            kind, end, closed = _COMMENT, (n if stop < 0 else stop + 2), stop >= 0
        elif ch in "'\"":
            kind, (end, closed) = _STRING, _scan_string(text, i)
        elif ch == "`":
            kind, (end, closed) = _QUOTED, _scan_quoted(text, i)
        elif ch == "$" and _is_ident_part(nxt):
            kind, end = _PARAM, _scan_while(text, i + 1, _is_ident_part)
        elif ch == "." and nxt == ".":
            end = i + 2
        elif _is_digit(ch) or (ch == "." and _is_digit(nxt)):
            kind, end = _NUMBER, _scan_number(text, i)
        elif ch.isidentifier():
            kind, end = _IDENT, _scan_while(text, i, _is_ident_part)
        else:
            kind, end = (_PUNCT if ch in _PUNCTUATION else _UNKNOWN), i + 1
        tokens.append(_Token(kind, text[i:end], closed))
        i = end
    return tokens


def _scan_string(text: str, i: int) -> tuple[int, bool]:
    """End of the string literal opened at ``i``, and whether it was closed."""
    quote, j = text[i], i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2  # a backslash escapes whatever follows, the quote included
        elif text[j] == quote:
            return j + 1, True
        else:
            j += 1
    return len(text), False


def _scan_quoted(text: str, i: int) -> tuple[int, bool]:
    """End of the backtick-quoted identifier opened at ``i``. A doubled backtick is an escape."""
    j = i + 1
    while j < len(text):
        if text[j] != "`":
            j += 1
        elif text.startswith("`", j + 1):
            j += 2
        else:
            return j + 1, True
    return len(text), False


def _scan_number(text: str, i: int) -> int:
    """End of the numeric literal at ``i``: decimal, float, exponent, hex or octal."""
    if text[i] == "0" and text[i + 1 : i + 2] in ("x", "X") and _is_hex(text[i + 2 : i + 3]):
        return _scan_while(text, i + 2, lambda c: _is_hex(c) or c == "_")
    if text[i] == "0" and text[i + 1 : i + 2] in ("o", "O") and _is_digit(text[i + 2 : i + 3]):
        return _scan_while(text, i + 2, _is_digit_or_underscore)
    j = _scan_while(text, i, _is_digit_or_underscore)
    if text[j : j + 1] == "." and _is_digit(text[j + 1 : j + 2]):
        j = _scan_while(text, j + 1, _is_digit_or_underscore)
    if text[j : j + 1] in ("e", "E"):
        k = j + 2 if text[j + 1 : j + 2] in ("+", "-") else j + 1
        if _is_digit(text[k : k + 1]):
            j = _scan_while(text, k, _is_digit_or_underscore)
    return j


def _scan_while(text: str, i: int, accept: Callable[[str], bool]) -> int:
    j = i
    while j < len(text) and accept(text[j]):
        j += 1
    return j


def _is_ident_part(ch: str) -> bool:
    return bool(ch) and ("_" + ch).isidentifier()


def _is_digit(ch: str) -> bool:
    return len(ch) == 1 and "0" <= ch <= "9"


def _is_digit_or_underscore(ch: str) -> bool:
    return ch == "_" or _is_digit(ch)


def _is_hex(ch: str) -> bool:
    return len(ch) == 1 and ch in "0123456789abcdefABCDEF"
