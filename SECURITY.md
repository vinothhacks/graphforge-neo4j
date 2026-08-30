# Security

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/vinothhacks/graphforge-neo4j/security/advisories/new).
Please do not open a public issue for anything exploitable.

Include what you did, what happened, and what you expected. A query that the read
guard should have refused, or a request the dashboard should have rejected, is
enough on its own — a working exploit is not required, and a short reproduction
is more useful than a long one.

## What graphforge is trying to protect

**The graph is read-only over MCP and over the web console.** `graphforge.query_guard`
is the single implementation behind both `read_cypher` and `POST /api/query`. It
masks strings, comments and quoted identifiers, then refuses write clauses,
`LOAD CSV`, `USE`, `SHOW`, multi-statement queries, and any `LIMIT` above 500.
Procedures are deny-by-default against a three-entry allowlist, and a `CALL`
whose target cannot be resolved is refused rather than passed through. The query
then runs inside a Neo4j read transaction with a timeout, so a defeated guard
still cannot write. See [ADR 9](docs/DESIGN.md) for why it is built this way and
what the first version got wrong.

A query that the guard admits and that writes, or that reaches the network or
the filesystem, is a vulnerability. Please report it.

**The dashboard is a local tool with no authentication.** The browser is the only
thing between it and any page you have open, so:

- Ingest endpoints do not exist unless the server is bound to loopback.
- Anything that can change the graph must carry a per-run token served inside the
  page, which a cross-origin script cannot read.
- A foreign `Origin` is refused (a cross-origin `fetch` with
  `Content-Type: text/plain` is a *simple* request and gets no preflight).
- A loopback-bound server refuses any `Host` that is not loopback, which is what
  stops DNS rebinding.

`graphforge ui --host 0.0.0.0` is supported but exposes your configuration —
`/api/status` reports the Neo4j URI, database names, repository names and
hostnames — and disables ingest entirely. The command warns when you do it.

**Credentials are not displayed and not passed on a command line.** Passwords are
masked in every dashboard payload and scrubbed from ingest job logs before they
reach the page. Git credentials go to the `git` subprocess through a short-lived
credential-helper file rather than in the remote URL or in argv, and git's own
output is scrubbed before it is logged or raised.

## What is out of scope

- **Anything you can already do with your own database credentials.** graphforge
  runs as you. If you can write to the graph with `cypher-shell`, being able to
  write to it another way is not a privilege escalation.
- **A password in your own `.env`.** `.env` is gitignored and never committed;
  keeping it readable only by you is your machine's job.
- **`graphforge ui --host 0.0.0.0` exposing configuration.** That is documented,
  warned about at startup, and the reason ingest is disabled on a public bind.

## Running it safely

- Give graphforge a **read-only database account** for `graphforge db`. It only
  ever reads `INFORMATION_SCHEMA` and system catalogues.
- Keep the dashboard on loopback.
- Do not enable APOC procedures you do not need. The bundled `docker-compose.yml`
  installs APOC but deliberately does *not* mark it unrestricted: unrestricted
  grants `apoc.load.jdbc` and `apoc.load.json`, which are outbound network and
  filesystem access, to anything that reaches the server — including Neo4j
  Browser, which graphforge's guard does not sit in front of.
