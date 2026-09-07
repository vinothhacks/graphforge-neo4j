"""Pytest markers: e2e suites stay skipped unless explicitly enabled."""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--e2e-critical", action="store_true", default=False, help="run live Neo4j CLI/HTTP tests"
    )
    parser.addoption(
        "--e2e-full", action="store_true", default=False, help="run playground Neo4j+Postgres tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e_critical: live Neo4j, HTTP/CLI only (no browser)")
    config.addinivalue_line(
        "markers", "e2e_full: playground Neo4j + Postgres + graphforge MCP stdio"
    )


def pytest_collection_modifyitems(config, items):
    critical = config.getoption("--e2e-critical") or os.getenv("GF_E2E_CRITICAL") == "1"
    full = config.getoption("--e2e-full") or os.getenv("GF_E2E_FULL") == "1"
    skip_c = pytest.mark.skip(reason="e2e_critical (pass --e2e-critical or GF_E2E_CRITICAL=1)")
    skip_f = pytest.mark.skip(reason="e2e_full (pass --e2e-full or GF_E2E_FULL=1)")
    for item in items:
        if "e2e_critical" in item.keywords and not critical:
            item.add_marker(skip_c)
        if "e2e_full" in item.keywords and not full:
            item.add_marker(skip_f)
