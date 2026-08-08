# Contributing to graphforge

Thanks for your interest. This project turns Git repositories and relational
databases into a Neo4j knowledge graph.

## Ground rules

- **Never commit secrets.** No passwords, tokens, connection strings, or
  internal hostnames/IPs in code, tests, fixtures, or docs. Config comes from
  the environment (`.env`, which is gitignored) or from gitignored
  `config/*.json` files. CI runs a secret scan on every PR.
- Keep the core dependency-light. Database drivers and the MCP SDK are
  **optional extras**, imported lazily so the package installs and runs without
  them.

## Dev setup

```bash
git clone <your-fork>
cd graphforge
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,all]"
pytest
ruff check src tests
```

## Adding a new database engine

1. Subclass `graphforge.db.base.SchemaExtractor`.
2. Implement the dialect-specific queries (`list_tables`, `list_columns`,
   `list_indexes`, `list_views`, `list_procedures`, `list_foreign_keys`).
3. Register it in `graphforge.db.get_extractor`.
4. Add unit tests that assert the emitted graph operations for a mocked cursor.

## Adding a new code parser

1. Add a module under `graphforge/git/parsers/` exposing
   `extract(lines: list[str]) -> dict`.
2. Wire its extensions into `graphforge/git/scan.py`.
3. Add a fixture file and a parser test.

## Releasing to PyPI

Publishing is automated via **Trusted Publishing** (OIDC — no stored token):

1. One-time, on PyPI: add a trusted publisher for the project `graphforge-neo4j`
   pointing at GitHub owner `vinothhacks`, repo `graphforge-neo4j`, workflow
   `release.yml`, environment `pypi`. See https://docs.pypi.org/trusted-publishers/
2. Bump `__version__` in `src/graphforge/__init__.py` and update `CHANGELOG.md`.
3. Tag and push:
   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```
   The `Release` workflow builds the sdist + wheel, runs `twine check`, and
   publishes to PyPI.

To publish manually instead:
```bash
pip install build twine
python -m build
twine check dist/*
twine upload dist/*        # needs a PyPI token
```

## Commit / PR checklist

- [ ] `pytest` passes
- [ ] `ruff check` clean
- [ ] No secrets or real hostnames added
- [ ] Public behavior changes are documented in the README
