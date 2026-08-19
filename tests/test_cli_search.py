"""CLI: `graphforge search` is on the parser and refuses an empty query."""
from __future__ import annotations

from graphforge.cli import build_parser, main


def test_search_subcommand_is_on_the_parser():
    args = build_parser().parse_args(
        ["search", "Foo", "--kind", "all", "--repo", "svc", "--limit", "5"])
    assert args.query == "Foo"
    assert args.kind == "all" and args.repo == "svc" and args.limit == 5
    assert args.func.__name__ == "cmd_search"


def test_search_defaults_to_code_kind():
    args = build_parser().parse_args(["search", "booking"])
    assert args.kind == "code" and args.repo == "" and args.limit == 25


def test_search_rejects_an_empty_query():
    assert main(["search"]) == 2
    assert main(["search", "   "]) == 2
