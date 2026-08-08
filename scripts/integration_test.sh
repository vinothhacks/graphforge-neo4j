#!/usr/bin/env bash
# =============================================================================
# graphforge end-to-end integration test
#
# Exercises the real CLI against real services - no fakes, no mocks:
#
#   1. wait for Neo4j and PostgreSQL to actually answer queries
#   2. seed PostgreSQL with a tiny schema: 2 tables + FK + view + function
#   3. build a throwaway git repository containing a JPA entity mapped to one
#      of those tables
#   4. graphforge init -> db -> git -> link
#   5. assert, via `graphforge verify`, that the expected labels and node
#      counts landed, and (via Bolt) that the link passes produced edges
#
# Usage:
#   docker compose -f docker-compose.ci.yml up -d
#   ./scripts/integration_test.sh
#   docker compose -f docker-compose.ci.yml down -v
#
# Everything is overridable, so this also runs against a hand-rolled stack:
#   GF_IT_PG_URL         postgres connection URL      (default: the compose one)
#   GF_IT_NEO4J_URI      bolt URI                     (default: bolt://127.0.0.1:7687)
#   GF_IT_NEO4J_USER / GF_IT_NEO4J_PASSWORD / GF_IT_NEO4J_DATABASE
#   GF_IT_GRAPHFORGE     graphforge entry point       (default: graphforge)
#   GF_IT_PYTHON         python used for the probes   (default: python3)
#   GF_IT_WAIT_SECONDS   readiness timeout per service (default: 180)
#   GF_IT_KEEP_TMP       set to 1 to keep the fixture repo for debugging
#
# The probes and the seeding use psycopg2 / the neo4j driver, both of which are
# already hard dependencies of graphforge. That keeps the script free of any
# docker/psql/cypher-shell requirement, so it works with `docker compose`, with
# GitHub Actions `services:`, or against a developer's local servers.
# =============================================================================
set -euo pipefail

# --- configuration -----------------------------------------------------------
PG_URL="${GF_IT_PG_URL:-postgresql://graphforge:graphforge-ci-password@127.0.0.1:5432/shopdb}"
NEO4J_URI="${GF_IT_NEO4J_URI:-bolt://127.0.0.1:7687}"
NEO4J_USER="${GF_IT_NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${GF_IT_NEO4J_PASSWORD:-graphforge-ci-password}"
NEO4J_DATABASE="${GF_IT_NEO4J_DATABASE:-neo4j}"
GF="${GF_IT_GRAPHFORGE:-graphforge}"
PY="${GF_IT_PYTHON:-python3}"
WAIT_SECONDS="${GF_IT_WAIT_SECONDS:-180}"
REPO_NAME="shop-service"

export PG_URL NEO4J_URI NEO4J_USER NEO4J_PASSWORD NEO4J_DATABASE

# Passed to every graphforge invocation so the run never depends on a stray
# .env file or on inherited environment.
GF_ARGS=(
  --neo4j-uri "$NEO4J_URI"
  --neo4j-user "$NEO4J_USER"
  --neo4j-password "$NEO4J_PASSWORD"
  --neo4j-database "$NEO4J_DATABASE"
)

FAILURES=0
CHECKED=0
WORKDIR=""

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  if [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ] && [ "${GF_IT_KEEP_TMP:-0}" != "1" ]; then
    rm -rf "$WORKDIR"
  elif [ -n "$WORKDIR" ]; then
    printf '    fixture kept at %s\n' "$WORKDIR"
  fi
}
trap cleanup EXIT

# --- 0. preflight ------------------------------------------------------------
log "Preflight"
command -v "$PY"  >/dev/null 2>&1 || die "python interpreter '$PY' not found"
command -v git    >/dev/null 2>&1 || die "git not found (needed to build the fixture repo)"
command -v "$GF"  >/dev/null 2>&1 || die "'$GF' not on PATH - run: pip install -e '.[dev]'"
"$PY" -c 'import psycopg2' >/dev/null 2>&1 || die "psycopg2 missing - run: pip install -e ."
"$PY" -c 'import neo4j'    >/dev/null 2>&1 || die "neo4j driver missing - run: pip install -e ."
info "graphforge: $("$GF" --version)"
info "postgres  : ${PG_URL%%:*}://...@${PG_URL##*@}"
info "neo4j     : $NEO4J_URI (database $NEO4J_DATABASE)"

# --- 1. wait for the services ------------------------------------------------
probe_postgres() {
  "$PY" - <<'PYEOF'
import os
import psycopg2
conn = psycopg2.connect(os.environ["PG_URL"], connect_timeout=3)
cur = conn.cursor()
cur.execute("SELECT 1")
cur.fetchone()
conn.close()
PYEOF
}

probe_neo4j() {
  "$PY" - <<'PYEOF'
import os
from neo4j import GraphDatabase
driver = GraphDatabase.driver(
    os.environ["NEO4J_URI"],
    auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    connection_timeout=5,
)
with driver.session(database=os.environ["NEO4J_DATABASE"]) as session:
    session.run("RETURN 1").consume()
driver.close()
PYEOF
}

wait_for() {
  local label="$1"; shift
  local deadline=$(( SECONDS + WAIT_SECONDS ))
  info "waiting for $label ..."
  until "$@" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      printf '\n--- last %s probe output ---\n' "$label" >&2
      "$@" >&2 || true
      die "timed out after ${WAIT_SECONDS}s waiting for $label"
    fi
    sleep 2
  done
  info "$label is ready"
}

log "1/6  Waiting for services"
wait_for "PostgreSQL" probe_postgres
wait_for "Neo4j" probe_neo4j

# --- 2. seed PostgreSQL ------------------------------------------------------
# Table names are all >= 4 characters on purpose: the SQL-text link passes
# ignore shorter identifiers (link.passes.DEFAULT_MIN_NAME_LEN).
log "2/6  Seeding PostgreSQL (2 tables + FK + view + function)"
"$PY" - <<'PYEOF'
import os
import psycopg2

DDL = """
DROP VIEW IF EXISTS active_orders;
DROP FUNCTION IF EXISTS order_total_for_customer(integer);
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id          serial PRIMARY KEY,
    email       varchar(255) NOT NULL UNIQUE,
    full_name   varchar(255),
    created_at  timestamp NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id           serial PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers (id),
    status       varchar(32) NOT NULL DEFAULT 'NEW',
    total_amount numeric(12,2) NOT NULL DEFAULT 0,
    placed_at    timestamp NOT NULL DEFAULT now()
);

CREATE INDEX orders_customer_idx ON orders (customer_id);

-- Feeds the BASED_ON link pass: names both tables in its definition.
CREATE VIEW active_orders AS
    SELECT orders.id, orders.status, orders.total_amount, customers.email
    FROM orders
    JOIN customers ON customers.id = orders.customer_id
    WHERE orders.status <> 'CANCELLED';

-- Feeds the USES_TABLE link pass: the routine body names `orders`.
CREATE FUNCTION order_total_for_customer(p_customer_id integer)
RETURNS numeric
LANGUAGE plpgsql
AS $body$
DECLARE
    v_total numeric;
BEGIN
    SELECT COALESCE(SUM(total_amount), 0) INTO v_total
    FROM orders
    WHERE customer_id = p_customer_id;
    RETURN v_total;
END;
$body$;

INSERT INTO customers (email, full_name) VALUES
    ('ada@example.com', 'Ada Lovelace'),
    ('alan@example.com', 'Alan Turing');
INSERT INTO orders (customer_id, status, total_amount) VALUES
    (1, 'NEW', 42.00),
    (2, 'SHIPPED', 17.50);
"""

conn = psycopg2.connect(os.environ["PG_URL"])
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(DDL)
    cur.execute("SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    tables = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM information_schema.views WHERE table_schema = 'public'")
    views = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM information_schema.routines "
                "WHERE routine_schema = 'public' AND routine_definition IS NOT NULL")
    routines = cur.fetchone()[0]
conn.close()
print(f"    seeded: {tables} tables, {views} view(s), {routines} routine(s) with a readable body")
if tables < 2 or views < 1 or routines < 1:
    raise SystemExit("seeding did not produce the expected objects")
PYEOF

# --- 3. fixture git repository ----------------------------------------------
log "3/6  Building the fixture git repository"
WORKDIR="$(mktemp -d)"
FIXTURE="$WORKDIR/$REPO_NAME"
mkdir -p "$FIXTURE/src/main/java/com/example/shop"

cat > "$FIXTURE/pom.xml" <<'XMLEOF'
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>shop-service</artifactId>
  <version>1.0.0</version>
  <packaging>jar</packaging>
  <name>Shop Service</name>
  <dependencies>
    <dependency>
      <groupId>javax.persistence</groupId>
      <artifactId>javax.persistence-api</artifactId>
      <version>2.2</version>
      <scope>provided</scope>
    </dependency>
  </dependencies>
</project>
XMLEOF

# @Table(name = "orders") is what the MAPS_TO pass keys on.
cat > "$FIXTURE/src/main/java/com/example/shop/Order.java" <<'JAVAEOF'
package com.example.shop;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

@Entity
@Table(name = "orders")
public class Order {

    @Id
    private Long id;

    @Column(name = "status")
    private String status;

    public Long getId() {
        return id;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }
}
JAVAEOF

cat > "$FIXTURE/src/main/java/com/example/shop/Customer.java" <<'JAVAEOF'
package com.example.shop;

import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

@Entity
@Table(name = "customers")
public class Customer {

    @Id
    private Long id;

    private String email;

    public String getEmail() {
        return email;
    }
}
JAVAEOF

cat > "$FIXTURE/report.py" <<'PYFIXEOF'
"""Tiny reporting helper - exercises the Python structural parser."""


class OrderReport:
    """Summarises orders for a customer."""

    def total(self, rows):
        return sum(row["total_amount"] for row in rows)

    def count(self, rows):
        return len(rows)
PYFIXEOF

cat > "$FIXTURE/README.md" <<'MDEOF'
# shop-service (integration fixture)

Disposable repository used by scripts/integration_test.sh.
MDEOF

git -C "$FIXTURE" init -q
git -C "$FIXTURE" symbolic-ref HEAD refs/heads/main
git -C "$FIXTURE" add -A
git -C "$FIXTURE" \
    -c user.email="ci@graphforge.invalid" \
    -c user.name="graphforge CI" \
    commit -q -m "Initial commit: shop service entities"
git -C "$FIXTURE" tag v1.0.0
info "fixture repo at $FIXTURE ($(git -C "$FIXTURE" rev-parse --short HEAD))"

# --- 4. run the pipeline -----------------------------------------------------
log "4/6  graphforge init"
"$GF" init "${GF_ARGS[@]}"

log "4/6  graphforge db --url <postgres>"
"$GF" db --url "$PG_URL" --replace "${GF_ARGS[@]}"

log "4/6  graphforge git <fixture>"
"$GF" git "$FIXTURE" --name "$REPO_NAME" --replace "${GF_ARGS[@]}"

log "4/6  graphforge link"
"$GF" link "${GF_ARGS[@]}"

# --- 5. assert on `graphforge verify` ----------------------------------------
log "5/6  graphforge verify"
VERIFY_OUT="$WORKDIR/verify.txt"
"$GF" verify "${GF_ARGS[@]}" | tee "$VERIFY_OUT"

count_of() {
  awk -v want="$1" '$1 == want { print $2; found = 1 } END { if (!found) print 0 }' "$VERIFY_OUT"
}

expect_at_least() {
  local label="$1" minimum="$2" actual
  actual="$(count_of "$label")"
  CHECKED=$(( CHECKED + 1 ))
  if [ "$actual" -lt "$minimum" ]; then
    printf '    \033[31mFAIL\033[0m  :%-18s expected >= %-4s got %s\n' "$label" "$minimum" "$actual"
    FAILURES=$(( FAILURES + 1 ))
  else
    printf '    ok    :%-18s %s (>= %s)\n' "$label" "$actual" "$minimum"
  fi
}

log "6/6  Assertions"
info "database subgraph"
expect_at_least Database        1    # shopdb
expect_at_least Schema          1    # public
expect_at_least Table           2    # customers, orders
expect_at_least Column          9    # 4 + 5
expect_at_least Index           2    # both primary keys (plus orders_customer_idx)
expect_at_least View            1    # active_orders
expect_at_least StoredProcedure 1    # order_total_for_customer

info "code subgraph"
expect_at_least Repository      1
expect_at_least Module          1    # from pom.xml
expect_at_least Dependency      1    # javax.persistence-api
expect_at_least Package         1    # com.example.shop
expect_at_least File            4    # pom.xml, 2 x .java, report.py, README.md
expect_at_least Class           3    # Order, Customer, OrderReport
expect_at_least Method          3
expect_at_least Commit          1
expect_at_least Author          1
expect_at_least Branch          1
expect_at_least Tag             1
expect_at_least Entity          2    # stereotype label on the two JPA classes

# `graphforge verify` only reports node counts, so the link passes - the whole
# point of running db + git against the same graph - are checked over Bolt.
info "link passes (relationships, read over Bolt)"
LINK_OUT="$WORKDIR/links.txt"
"$PY" - > "$LINK_OUT" <<'PYEOF'
import os
from neo4j import GraphDatabase

EXPECTED = {
    "MAPS_TO": 2,      # Order -> orders, Customer -> customers
    "BASED_ON": 2,     # active_orders -> orders, customers
    "USES_TABLE": 1,   # order_total_for_customer -> orders
    "REFERENCES": 1,   # orders -> customers (FK)
    "FOREIGN_KEY": 1,
    "HAS_COLUMN": 9,
}

driver = GraphDatabase.driver(
    os.environ["NEO4J_URI"],
    auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
)
with driver.session(database=os.environ["NEO4J_DATABASE"]) as session:
    for rel_type, minimum in EXPECTED.items():
        record = session.run(
            f"MATCH ()-[r:`{rel_type}`]->() RETURN count(r) AS c"
        ).single()
        print(f"{rel_type} {record['c'] if record else 0} {minimum}")
driver.close()
PYEOF

while read -r rel actual minimum; do
  [ -n "$rel" ] || continue
  CHECKED=$(( CHECKED + 1 ))
  if [ "$actual" -lt "$minimum" ]; then
    printf '    \033[31mFAIL\033[0m  [:%-14s] expected >= %-4s got %s\n' "$rel" "$minimum" "$actual"
    FAILURES=$(( FAILURES + 1 ))
  else
    printf '    ok    [:%-14s] %s (>= %s)\n' "$rel" "$actual" "$minimum"
  fi
done < "$LINK_OUT"

# --- result ------------------------------------------------------------------
if [ "$FAILURES" -ne 0 ]; then
  printf '\n\033[31m%s integration assertion(s) failed.\033[0m\n' "$FAILURES" >&2
  printf 'Full `graphforge verify` output:\n' >&2
  cat "$VERIFY_OUT" >&2
  printf '\nRerun with GF_IT_KEEP_TMP=1 to keep the fixture repository for inspection.\n' >&2
  exit 1
fi

printf '\n\033[32mIntegration test passed: %s assertions over a real Neo4j + PostgreSQL.\033[0m\n' \
  "$CHECKED"
