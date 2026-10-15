INSERT INTO public.customers (email, name, address, tags, profile) VALUES
    ('ada@example.com', 'Ada Lovelace', ROW('12 Analytical St', 'London', 'N1'), '{math,pioneer}',
     '{"vip": true, "score": 12345678901234567890, "nested": {"z": 1, "a": 2}}'),
    ('lin@example.com', '林小明', ROW('台北市信義路 5 段', '台北', '110'), '{}', '{"note": "中文資料"}'),
    ('empty@example.com', '', NULL, NULL, NULL);

INSERT INTO sales.orders (customer_id, status, total, note) VALUES
    (1, 'paid', 12345678901234567890.1234567890, NULL),
    (1, 'pending', 10.5, 'gift'),
    (2, 'shipped', 99.99, '');

INSERT INTO sales.events (occurred_at, payload) VALUES ('2026-03-01', '{"kind": "signup"}');
INSERT INTO public.blobs (data, big_number, huge_int) VALUES ('\xdeadbeef', 12345678901234567890.123456789, 9007199254740993);
INSERT INTO public.big_table (id, label, value) SELECT g, 'row ' || g, g * 1.5 FROM generate_series(1, 100000) g;
INSERT INTO public."Order Items" ("Select", "order") VALUES ('first', 1);

REFRESH MATERIALIZED VIEW sales.daily_totals;
ANALYZE;
