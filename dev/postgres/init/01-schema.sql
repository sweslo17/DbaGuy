CREATE SCHEMA sales;

CREATE TYPE public.order_status AS ENUM ('pending', 'paid', 'shipped', 'cancelled');
CREATE DOMAIN public.email AS text CHECK (VALUE ~ '^[^@]+@[^@]+$');
CREATE TYPE public.address AS (street text, city text, zip text);

CREATE TABLE public.customers (
    id bigserial PRIMARY KEY,
    email public.email NOT NULL UNIQUE,
    name text NOT NULL,
    address public.address,
    tags text[] DEFAULT '{}',
    profile jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.customers IS 'Customer master data';
COMMENT ON COLUMN public.customers.email IS 'Login e-mail';

CREATE TABLE sales.orders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES public.customers (id),
    status public.order_status NOT NULL DEFAULT 'pending',
    total numeric(30, 10) NOT NULL,
    placed_at timestamptz NOT NULL DEFAULT now(),
    note text
);
CREATE INDEX orders_customer_idx ON sales.orders (customer_id);

CREATE TABLE sales.events (
    id bigserial,
    occurred_at date NOT NULL,
    payload jsonb,
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE TABLE sales.events_2026 PARTITION OF sales.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

CREATE TABLE public.blobs (id serial PRIMARY KEY, data bytea, big_number numeric, huge_int bigint);
CREATE TABLE public.big_table (id integer PRIMARY KEY, label text NOT NULL, value double precision);
-- Identifiers that need quoting (Review Focus 1)
CREATE TABLE public."Order Items" (id serial PRIMARY KEY, "Select" text, "order" integer);

CREATE VIEW sales.order_summary AS
SELECT c.name, count(o.id) AS orders, sum(o.total) AS total
FROM public.customers c
LEFT JOIN sales.orders o ON o.customer_id = c.id
GROUP BY c.name;

CREATE MATERIALIZED VIEW sales.daily_totals AS
SELECT date_trunc('day', placed_at) AS day, sum(total) AS total FROM sales.orders GROUP BY 1;

CREATE SEQUENCE public.invoice_seq START 1000 INCREMENT 10;

CREATE FUNCTION public.add_numbers(a integer, b integer) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$ SELECT a + b $$;
CREATE FUNCTION public.add_numbers(a numeric, b numeric) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$ SELECT a + b $$;

CREATE PROCEDURE sales.archive_orders(before timestamptz)
LANGUAGE plpgsql AS $$
BEGIN
    RAISE NOTICE 'archiving before %', before;
END $$;

-- No-op trigger so the tree has something to show.
CREATE FUNCTION sales.orders_noop() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
CREATE TRIGGER orders_noop BEFORE INSERT ON sales.orders FOR EACH ROW EXECUTE FUNCTION sales.orders_noop();
