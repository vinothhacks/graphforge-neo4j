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

Requires **Python 3.10+** and the `git` CLI.

```bash
git clone <your-fork>
cd graphforge
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,all]"
pre-commit install                                   # optional but recommended
pytest
ruff check src tests
```

Most of the test suite runs with no database at all — the writer's `emit` and
`dry-run` modes exist partly so the whole pipeline is testable offline. For an
end-to-end run against real services, see
[`scripts/integration_test.sh`](scripts/integration_test.sh) and
[`docker-compose.ci.yml`](docker-compose.ci.yml); for somewhere to poke at by
hand, [`examples/docker-compose.yml`](examples/docker-compose.yml) brings up
Neo4j plus a pre-seeded PostgreSQL.

Design rationale and the decision log live in [`docs/DESIGN.md`](docs/DESIGN.md).
Read the ADRs before proposing a change to something they cover — they record
what each decision costs, which is usually the part worth arguing with.

---

## Good first issues

Three self-contained pieces of work, each valuable and none requiring much
context.

### 1. Add a language parser

The scanner records `:File` nodes for **41 extensions**, but only four languages
get class-level structure. Kotlin, C#, Ruby, PHP, Rust, Scala and Swift are all
one module away.

Everything you need is in [`src/graphforge/git/parsers/`](src/graphforge/git/parsers/).
The four existing parsers are the template — pick whichever is closest to your
language and copy its shape:

| Template | Good starting point for |
|----------|------------------------|
| [`java.py`](src/graphforge/git/parsers/java.py) | C#, Kotlin, Scala — brace languages with modifiers and annotations |
| [`python.py`](src/graphforge/git/parsers/python.py) | Ruby — indentation/keyword-delimited, decorators |
| [`typescript.py`](src/graphforge/git/parsers/typescript.py) | anything with ES-style `import`/`export` |
| [`golang.py`](src/graphforge/git/parsers/golang.py) | Rust, Swift — `type`/`struct`/`impl` declarations, receiver methods |

The contract:

1. Expose exactly `extract(lines: list[str]) -> dict` returning the shared keys —
   `package`, `imports`, `classes`, `interfaces`, `enums`, `methods`,
   `annotations`. Extra keys are allowed (`typescript.py` adds `exports`) but
   ignored by the ingestor.
2. Register the module in `PARSERS` in
   [`git/scan.py`](src/graphforge/git/scan.py) (keyed by `:File.type`), and add
   the extension to `SUPPORTED_EXTENSIONS` in
   [`git/parsers/generic.py`](src/graphforge/git/parsers/generic.py).
3. If your language's FQN is not "directory-dotted", add a branch to
   `scan.namespace_for()`.
4. Add a test modelled on `tests/test_python_parser.py` /
   `tests/test_go_parser.py`, plus a case in `tests/test_multilang_scan.py`.

Keep it regex/line-based — see ADR 3 in `docs/DESIGN.md` for why. A parser that
returns partial structure for a file that does not compile is doing its job; one
that raises takes the whole scan with it.

### 2. Flip the last non-blocking CI job to blocking

One job in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) still runs
with `continue-on-error: true`, because its configuration has not been verified
end to end. Making it blocking is a complete contribution:

- **`lint` (ruff) and `types` (mypy) are now blocking.** Both were advisory
  while their configuration had never been run against the tree; both are now
  clean and enforced. If a rule turns out to be more noise than signal, move it
  to `ignore` with a *reason comment* — the existing entries all have one, and a
  new one without it will be asked for in review. `ruff format` has still never
  been run over this tree, so that reformat must land as its own isolated commit
  with no behaviour change in it.
- **`integration`.** `scripts/integration_test.sh` was authored with no docker
  and no network. Its shell, embedded Python and Java fixture were validated
  statically, but a green run is unproven. Dispatch it manually
  (`workflow_dispatch`), fix what it finds, then delete the flag.

In every case the change is the same one line: delete `continue-on-error: true`.
Getting there is the work.

### 3. Keep the dashboard screenshots current

`docs/img/dashboard.png` and `docs/img/dashboard-empty.png` are committed and
embedded in the README. If you change the dashboard's layout, retake them —
about ten minutes with the playground compose stack.

[`docs/img/README.md`](docs/img/README.md) has the full recipe: what to ingest
first, how to frame the canvas and the Cypher console, sizing, and the
**check-for-secrets** step (the dashboard masks passwords but still shows your
URIs, database names and repository names — use the throwaway playground values,
not your employer's). Save the file, swap the paragraph under `### Screenshots`
in the README for an `<img>` tag, and that is the PR.

---

## Adding a new database engine

1. Subclass `graphforge.db.base.SchemaExtractor`.
2. Implement the dialect-specific queries (`list_tables`, `list_columns`,
   `list_indexes`, `list_views`, `list_procedures`, `list_foreign_keys`).
3. Register it in `graphforge.db.get_extractor`.
4. Add unit tests that assert the emitted graph operations for a mocked cursor.

## Adding a new code parser

See [Good first issues → Add a language parser](#1-add-a-language-parser) above
for the full contract and which existing parser to copy.

## Releasing to PyPI

Publishing is automated via **Trusted Publishing** (OIDC — no stored token). The
tag decides the index:

| Tag | Goes to | Environment |
|-----|---------|-------------|
| `v0.2.0-rc1`, `v0.2.0-alpha1`, `v0.2.0-beta1` | **TestPyPI** | `testpypi` |
| `v0.2.0` | **PyPI** | `pypi` |

1. One-time, on **both** https://pypi.org and https://test.pypi.org: add a
   trusted publisher for the project `graphforge-neo4j` pointing at GitHub owner
   `vinothhacks`, repo `graphforge-neo4j`, workflow `release.yml`, environment
   `pypi` / `testpypi` respectively. Create the two GitHub environments with the
   same names. See https://docs.pypi.org/trusted-publishers/
2. Bump `version` in `pyproject.toml` (and the fallback literal in
   `src/graphforge/__init__.py`), then move the `[Unreleased]` section of
   `CHANGELOG.md` under the new `## [x.y.z]` heading. The release workflow reads
   that section with `scripts/changelog_section.py` and uses it as the GitHub
   Release body, falling back to `Unreleased` if the rename has not happened yet.
3. Tag and push:
   ```bash
   git tag v0.2.0-rc1 && git push origin v0.2.0-rc1   # rehearse on TestPyPI first
   git tag v0.2.0     && git push origin v0.2.0
   ```
   The `Release` workflow builds the sdist + wheel, runs `twine check --strict`,
   publishes, and attaches a GitHub Release with the dist files.

Install a release candidate to check it:

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ graphforge-neo4j
```

(The extra index is required — the runtime dependencies only exist on real PyPI.)

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
- [ ] Notable changes added to `CHANGELOG.md` under `[Unreleased]`
- [ ] New CLI flags appear in the README's command tables and in `--help`
- [ ] Any new file under `examples/` is referenced from `examples/README.md`
