# Feature audit

Four-way diff of what README claims, what [`BUILD_PLAN.md`](../BUILD_PLAN.md) promised, what `src/graphforge` does, and whether a test names the behaviour. Status values: `SHIPPED_TESTED` (test id named) / `SHIPPED_UNTESTED` / `PARTIAL` / `MISSING` / `DOC_ONLY` / `FIXTURE_ONLY`.

Code wins over README. Numbers: **10** CLI commands, **12** MCP tools.

## VDS (first target)

`graphforge vds` is **implemented**, not vestigial.

| README | BUILD_PLAN | Code | Test | Status |
|--------|------------|------|------|--------|
| Optional VDS/query catalog; README used to say “see below” with no section (fixed) | `cli.py` lists `vds`; `db/vds.py` | [`cmd_vds`](../src/graphforge/cli.py) + [`VdsIngestor`](../src/graphforge/db/vds.py): flags `--engine/--host/--port/--user/--password/--database/--driver` plus `--emit/--dry-run/--no-schema`. Reads `vdsservicecatalog` ⋈ `querydetails` ⋈ `wherefieldconfig`. Writes `VDSService`/`VDSQuery`/`VDSWhereField`. | `tests/test_enrichment.py::test_vds_write_rows` (mapping/`--emit` only). Live `_fetch` untested. | `PARTIAL` |

There is no extra CLI flag for catalog table names; they live on `VdsConfig` defaults. Do not invent docs for flags that do not exist.

## Commands

| Row | README | BUILD_PLAN | Code | Test | Status |
|-----|--------|------------|------|------|--------|
| `init` | yes | yes | `cmd_init` | `test_apply_schema_emit` | `SHIPPED_TESTED` |
| `git` | yes | yes | `cmd_git` | `test_incremental_git.py`, `test_git_history.py`, `test_multilang_scan.py`, `test_e2e_critical.py::test_ingest_twice_no_dupes` | `SHIPPED_TESTED` |
| `db` | yes | yes | `cmd_db` | `test_db_mapping.py`, `test_url_and_discovery.py`, `test_db_sampling.py` | `SHIPPED_TESTED` |
| `vds` | yes (now accurate) | yes | `cmd_vds` | mapping only | `PARTIAL` |
| `link` | yes | yes | `cmd_link` | `test_link_tokens.py`, `test_link_passes_render` | `SHIPPED_TESTED` |
| `status` | yes | yes | `cmd_status` | none (CLI) | `SHIPPED_UNTESTED` |
| `verify` | yes | yes | `cmd_verify` | none (CLI) | `SHIPPED_UNTESTED` |
| `search` | yes | no (post-plan) | `cmd_search` | `test_cli_search.py` | `SHIPPED_TESTED` |
| `ui` | yes | yes | `cmd_ui` | `test_ui.py`, `test_ui_api.py` | `SHIPPED_TESTED` |
| `mcp` | yes | yes | `cmd_mcp` | `test_build_server_registers_every_documented_tool` | `SHIPPED_TESTED` |

## Shared flags

| Flag | README | Code | Test | Status |
|------|--------|------|------|--------|
| `--emit` | yes | `_add_writer_opts` on init/git/db/vds/link | `test_emit_mode`, `test_offline_matrix.py` | `SHIPPED_TESTED` |
| `--dry-run` | yes | same | `test_dry_run_counts_only`, `test_offline_matrix.py` | `SHIPPED_TESTED` |
| `--no-schema` | yes | same | offline matrix | `SHIPPED_TESTED` |
| `--replace` | was listed as shared; **git+db only** (README fixed) | `p_git`, `p_db` | `test_replace_wins_over_since_commit` (emit); live `tests/test_e2e_critical.py::test_replace_removes` | `SHIPPED_TESTED` |
| `--env` | yes | `_add_common` | none dedicated | `SHIPPED_UNTESTED` |
| `--neo4j-uri/-user/-password/-database` | yes | `_add_common` | none dedicated | `SHIPPED_UNTESTED` |

## git flags

| Flag | Code | Test | Status |
|------|------|------|--------|
| `--name` / `--branch` | yes | parser via incremental CLI | `SHIPPED_TESTED` (`test_cli_exposes_since_commit` adjacent) |
| `--since-commit {sha\|auto}` | yes | `test_since_commit_round_trip`, `test_auto_without_a_stored_watermark_is_a_full_ingest`, `test_unknown_sha_falls_back_to_a_full_ingest`; live `test_e2e_full.py::test_incremental_and_degrades` | `SHIPPED_TESTED` |
| `--since DAYS` | `gitlab_group_repos(..., since_days=)` | none | `SHIPPED_UNTESTED` |
| `--lines` | yes | none CLI | `SHIPPED_UNTESTED` |
| `--no-structure` / `--no-history` | yes | none | `SHIPPED_UNTESTED` |

## db flags

| Flag | Code | Test | Status |
|------|------|------|--------|
| `--url` | `parse_db_url` | `test_parse_db_url_*` | `SHIPPED_TESTED` |
| `--engine/--host/--port/--user/--password/--databases` | yes | `test_cli_sample_rows_flows_into_the_source` (partial) | `PARTIAL` |
| `--schemas` | yes | `test_explicit_schema_filter_is_honoured` | `SHIPPED_TESTED` |
| `--driver` | yes | none | `SHIPPED_UNTESTED` |
| `--sample-rows` | yes | `test_db_sampling.py` | `SHIPPED_TESTED` |

## link flags

| Flag | Code | Test | Status |
|------|------|------|--------|
| `--maps-to` `--based-on` `--uses-table` `--cross-db` | yes | `test_link_passes_render`, `test_every_text_pass_uses_token_matching` | `SHIPPED_TESTED` (Cypher emit, not live edges) |
| `--min-table-name-len` | yes | `test_min_name_length_defaults_to_four_and_is_configurable`, `test_cli_min_table_name_len_flag` | `SHIPPED_TESTED` |

## Parsers

| Parser | Code | Test | Status |
|--------|------|------|--------|
| java | `PARSERS["java"]` | `test_java_parser.py` incl. `test_malformed_input_never_raises`, `test_java_output_is_unchanged` | `SHIPPED_TESTED` |
| python | yes | `test_python_parser.py` incl. `test_malformed_input_never_raises` | `SHIPPED_TESTED` |
| typescript | yes (+ javascript alias) | `test_typescript_parser.py` | `SHIPPED_TESTED` |
| golang | yes | `test_go_parser.py` | `SHIPPED_TESTED` |
| 41 ext / 32 types | `SUPPORTED_EXTENSIONS` | `test_readme_parser_and_extension_counts` | `SHIPPED_TESTED` |

## Engines

| Engine | Code | Test | Status |
|--------|------|------|--------|
| mysql | `MySQLExtractor` | `test_engine_factory_and_sql`, `test_write_database_emit` | `SHIPPED_TESTED` |
| postgresql | `PostgresExtractor` | same + sampling | `SHIPPED_TESTED` |
| mssql | `MssqlExtractor` | identifier/schema tests; INFORMATION_SCHEMA fixtures | `FIXTURE_ONLY` |

## MCP tools

| Tool | Test | Status |
|------|------|--------|
| `get_schema` | `test_get_schema_*`, `test_get_schema_tool_exposes_ttl_and_refresh` | `SHIPPED_TESTED` |
| `read_cypher` | `test_read_cypher_*`, `test_query_guard.py` | `SHIPPED_TESTED` |
| `search_nodes` | pagination tests | `SHIPPED_TESTED` |
| `node_neighbors` | `test_legacy_positional_calls_return_the_old_shapes` | `SHIPPED_TESTED` |
| `find_code` | `test_find_code_*`, `test_search_codebase_*` | `SHIPPED_TESTED` |
| `search_codebase` | `test_search_codebase_*` | `SHIPPED_TESTED` |
| `find_table` | page tests | `SHIPPED_TESTED` |
| `find_procedure` | legacy shape | `SHIPPED_TESTED` |
| `impact_of_column` | `test_new_tools_return_json_documents` | `SHIPPED_TESTED` |
| `explain_impact` | `test_explain_impact_*` | `SHIPPED_TESTED` |
| `find_dead_code` | `test_find_dead_code_*` | `SHIPPED_TESTED` |
| `blast_radius_of_file` | `test_blast_radius_*` | `SHIPPED_TESTED` |

Stdio live client: `tests/test_e2e_full.py::test_mcp_stdio_tools_pagination_and_caveat` (`SHIPPED_TESTED` when `GF_E2E_FULL=1`).

## Dashboard endpoints

| Endpoint | Test | Status |
|----------|------|--------|
| `GET /api/status` | `test_status_route_masks_passwords` | `SHIPPED_TESTED` |
| `GET /api/schema` | `test_schema_endpoint_shape` | `SHIPPED_TESTED` |
| `GET /api/graph/sample` | `test_graph_sample_*` | `SHIPPED_TESTED` |
| `GET /api/search` | `test_search_*` | `SHIPPED_TESTED` |
| `GET /api/labels/<label>/sample` | `test_label_sample_*` | `SHIPPED_TESTED` |
| `GET /api/node/<id>/neighbors` | `test_node_neighbors_*` | `SHIPPED_TESTED` |
| `POST /api/query` | `test_query_*`, `test_query_guard.py` | `SHIPPED_TESTED` |

Browser UI: Playwright MCP runbook `docs/DASHBOARD_E2E.md` (agent pass).

## Env

| Var | Code | Test | Status |
|-----|------|------|--------|
| `NEO4J_*` | `Neo4jSettings.from_env` | ui mask tests | `PARTIAL` |
| `GF_BATCH_SIZE` | yes | none | `SHIPPED_UNTESTED` |
| `GF_INCLUDE_LINES` | yes | none | `SHIPPED_UNTESTED` |
| `GITLAB_SERVER/GROUP_ID/TOKEN` | `GitSettings` + `gitlab_group_repos` | none | `SHIPPED_UNTESTED` |

## BUILD_PLAN leftovers

| Promise | Status |
|---------|--------|
| Workstream B React dashboard / `--react` | `MISSING` (vanilla is the product) |
| Workstream A drop entire ruff ignore list | `PARTIAL` (lint is green; some ignores remain) |
| Dashboard screenshot `docs/img/dashboard.png` | `SHIPPED_TESTED` (`docs/DASHBOARD_E2E.md#dashboard-screenshot`) |
