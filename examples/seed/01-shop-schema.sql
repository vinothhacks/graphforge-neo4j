-- ===========================================================================
-- graphforge playground seed  —  a small but realistic "shop" schema
--
-- Loaded automatically by examples/docker-compose.yml: the postgres image runs
-- every *.sql in /docker-entrypoint-initdb.d once, on FIRST start of an empty
-- data volume. To re-run it after editing:
--
--     docker compose -f examples/docker-compose.yml down -v   # -v drops the volume
--     docker compose -f examples/docker-compose.yml up -d
--
-- Deliberate properties, so the graph is interesting rather than merely valid:
--   * every table name is >= 4 characters — the SQL-text link passes ignore
--     shorter identifiers (see --min-table-name-len, default 4)
--   * a foreign-key fan-in on `customers` and `products`, so FK-hotspot
--     queries return something
--   * a view and two functions whose bodies NAME tables, so `graphforge link`
--     has BASED_ON / USES_TABLE edges to create
--   * table names that a JPA @Table annotation would plausibly use, so the
--     MAPS_TO pass has a target if you also ingest a Java repo
--
-- These credentials are throwaway and public. This stack binds to localhost
-- and is meant to be destroyed; never copy these values anywhere real.
-- ===========================================================================

BEGIN;

CREATE TABLE customers (
    customer_id   SERIAL PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,
    display_name  VARCHAR(120) NOT NULL,
    country_code  CHAR(2)      NOT NULL DEFAULT 'GB',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE products (
    product_id    SERIAL PRIMARY KEY,
    sku           VARCHAR(64)  NOT NULL UNIQUE,
    product_name  VARCHAR(200) NOT NULL,
    unit_price    NUMERIC(10,2) NOT NULL,
    discontinued  BOOLEAN      NOT NULL DEFAULT false
);

CREATE TABLE orders (
    order_id      SERIAL PRIMARY KEY,
    customer_id   INTEGER      NOT NULL REFERENCES customers (customer_id),
    order_status  VARCHAR(32)  NOT NULL DEFAULT 'PENDING',
    placed_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    total_amount  NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INTEGER      NOT NULL REFERENCES orders (order_id),
    product_id    INTEGER      NOT NULL REFERENCES products (product_id),
    quantity      INTEGER      NOT NULL DEFAULT 1,
    line_total    NUMERIC(12,2) NOT NULL
);

CREATE TABLE shipments (
    shipment_id   SERIAL PRIMARY KEY,
    order_id      INTEGER      NOT NULL REFERENCES orders (order_id),
    carrier_name  VARCHAR(80)  NOT NULL,
    tracking_ref  VARCHAR(120),
    shipped_at    TIMESTAMPTZ
);

-- Indexes: give :Index nodes and the column-blast-radius recipe something to find.
CREATE INDEX idx_orders_customer  ON orders (customer_id);
CREATE INDEX idx_orders_status    ON orders (order_status, placed_at);
CREATE INDEX idx_items_order      ON order_items (order_id);
CREATE INDEX idx_items_product    ON order_items (product_id);
CREATE INDEX idx_shipments_order  ON shipments (order_id);

-- A view: `graphforge link --based-on` reads VIEW_DEFINITION and connects this
-- to `orders` and `customers` as whole tokens.
CREATE VIEW active_orders AS
SELECT o.order_id,
       o.order_status,
       o.placed_at,
       o.total_amount,
       c.customer_id,
       c.display_name,
       c.email
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.order_status <> 'CANCELLED';

-- A second view, reading a different table set, so BASED_ON is not a single edge.
CREATE VIEW product_revenue AS
SELECT p.product_id,
       p.sku,
       p.product_name,
       sum(i.line_total) AS revenue
FROM products p
JOIN order_items i ON i.product_id = p.product_id
GROUP BY p.product_id, p.sku, p.product_name;

-- Routines: INFORMATION_SCHEMA.ROUTINES exposes the body, which is what
-- `graphforge link --uses-table` and the MCP `find_procedure` tool read.
CREATE FUNCTION order_total_for_customer(p_customer_id INTEGER)
RETURNS NUMERIC AS $$
    SELECT coalesce(sum(total_amount), 0)
    FROM orders
    WHERE customer_id = p_customer_id;
$$ LANGUAGE sql STABLE;

CREATE FUNCTION recalculate_order_total(p_order_id INTEGER)
RETURNS NUMERIC AS $$
DECLARE
    v_total NUMERIC(12,2);
BEGIN
    SELECT coalesce(sum(line_total), 0) INTO v_total
    FROM order_items
    WHERE order_id = p_order_id;

    UPDATE orders SET total_amount = v_total WHERE order_id = p_order_id;
    RETURN v_total;
END;
$$ LANGUAGE plpgsql;

-- A handful of rows. graphforge does NOT ingest row data, but they make
-- `--sample-rows` return non-zero approxRows / approxCardinality.
INSERT INTO customers (email, display_name, country_code) VALUES
    ('ada@example.invalid',    'Ada Lovelace',   'GB'),
    ('grace@example.invalid',  'Grace Hopper',   'US'),
    ('alan@example.invalid',   'Alan Turing',    'GB');

INSERT INTO products (sku, product_name, unit_price) VALUES
    ('SKU-0001', 'Mechanical keyboard', 129.00),
    ('SKU-0002', 'Trackball mouse',      59.50),
    ('SKU-0003', 'Standing desk mat',    45.00);

INSERT INTO orders (customer_id, order_status, total_amount) VALUES
    (1, 'SHIPPED',   188.50),
    (2, 'PENDING',    45.00),
    (3, 'CANCELLED', 129.00);

INSERT INTO order_items (order_id, product_id, quantity, line_total) VALUES
    (1, 1, 1, 129.00),
    (1, 2, 1,  59.50),
    (2, 3, 1,  45.00),
    (3, 1, 1, 129.00);

INSERT INTO shipments (order_id, carrier_name, tracking_ref, shipped_at) VALUES
    (1, 'Royal Mail', 'RM-000000001', now());

COMMIT;
