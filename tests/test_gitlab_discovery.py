"""GitLab group discovery: pagination, filtering, and the bound that was missing.

This module had no tests at all. Its loop was `while True` with no page limit and
no check that a page contained anything new, so an API that kept answering with
the same batch -- a caching proxy, a misconfigured gateway -- would spin forever
building an ever-growing list.
"""

from __future__ import annotations

import sys
import types

import pytest

from graphforge.git import discover


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _project(name: str, branch: str | None = "main") -> dict:
    return {
        "path": name,
        "http_url_to_repo": f"https://gitlab.example/g/{name}.git",
        "default_branch": branch,
    }


@pytest.fixture
def fake_requests(monkeypatch):
    """Install a stand-in `requests` module and record every call."""
    calls: list[dict] = []
    module = types.ModuleType("requests")

    def get(url, params=None, headers=None, timeout=None):
        calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        return module.responder(params or {})

    module.get = get
    module.responder = lambda params: _Response([])
    monkeypatch.setitem(sys.modules, "requests", module)
    module.calls = calls
    return module


def test_pages_until_an_empty_batch(fake_requests):
    pages = {1: [_project("a"), _project("b")], 2: [_project("c")], 3: []}
    fake_requests.responder = lambda params: _Response(pages[params["page"]])

    specs = discover.gitlab_group_repos("https://gitlab.example/", "42")
    assert [s["name"] for s in specs] == ["a", "b", "c"]
    assert [c["params"]["page"] for c in fake_requests.calls] == [1, 2, 3]


def test_an_api_that_repeats_a_page_forever_still_terminates(fake_requests):
    """The failure this guards is an infinite loop, so bound the test too."""
    fake_requests.responder = lambda params: _Response([_project("same")])

    specs = discover.gitlab_group_repos("https://gitlab.example", "42")
    assert len(specs) == 1, "a repeated project was added more than once"
    assert len(fake_requests.calls) <= discover.MAX_PAGES


def test_pagination_is_bounded_even_with_endless_distinct_pages(fake_requests, monkeypatch):
    monkeypatch.setattr(discover, "MAX_PAGES", 5)
    fake_requests.responder = lambda params: _Response([_project(f"p{params['page']}")])

    specs = discover.gitlab_group_repos("https://gitlab.example", "42")
    assert len(specs) == 5
    assert len(fake_requests.calls) == 5


def test_the_token_is_sent_as_a_header_not_a_query_parameter(fake_requests):
    fake_requests.responder = lambda params: _Response([])
    discover.gitlab_group_repos("https://gitlab.example", "42", token="glpat-secret")

    call = fake_requests.calls[0]
    assert call["headers"]["PRIVATE-TOKEN"] == "glpat-secret"
    assert "glpat-secret" not in str(call["params"]), "token leaked into the query string"
    assert "glpat-secret" not in call["url"]


def test_since_days_becomes_a_last_activity_cutoff(fake_requests):
    fake_requests.responder = lambda params: _Response([])
    discover.gitlab_group_repos("https://gitlab.example", "42", since_days=7)

    params = fake_requests.calls[0]["params"]
    assert "last_activity_after" in params
    assert params["include_subgroups"] == "true"


def test_no_cutoff_when_since_days_is_zero(fake_requests):
    fake_requests.responder = lambda params: _Response([])
    discover.gitlab_group_repos("https://gitlab.example", "42", since_days=0)
    assert "last_activity_after" not in fake_requests.calls[0]["params"]


def test_a_project_without_a_default_branch_falls_back(fake_requests):
    pages = {1: [_project("a", branch=None)], 2: []}
    fake_requests.responder = lambda params: _Response(pages[params["page"]])

    specs = discover.gitlab_group_repos("https://gitlab.example", "42", default_branch="trunk")
    assert specs[0]["branch"] == "trunk"


def test_a_missing_requests_says_which_extra_to_install(monkeypatch):
    """requests is an extra as of 0.3; discovery is the only thing that needs it."""
    monkeypatch.setitem(sys.modules, "requests", None)
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "requests":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r"graphforge-neo4j\[gitlab\]"):
        discover.gitlab_group_repos("https://gitlab.example", "42")
