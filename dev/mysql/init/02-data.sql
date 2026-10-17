USE dbaguy;

-- The image's init client runs in the POSIX locale and would send non-ASCII text as latin1 (double-encoded).
SET NAMES utf8mb4;

SET SESSION cte_max_recursion_depth = 100000;
INSERT INTO big_table (id, label, value)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 100000)
SELECT n, CONCAT('row ', n), n * 1.5 FROM seq;

INSERT INTO customers (email, name, profile) VALUES
    ('ada@example.com', 'Ada Lovelace', '{"vip": true, "score": 12345678901234567890}'),
    ('lin@example.com', '林小明', '{"note": "中文資料"}'),
    ('empty@example.com', '', NULL);

INSERT INTO orders (customer_id, total, note) VALUES
    (1, 12345678901234567890.1234567890, NULL),
    (1, 10.5, 'gift'),
    (2, 99.99, '');

-- A zero date: strict mode (the default) rejects it, but real tables still hold them.
SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION';
INSERT INTO types_demo VALUES
    (1, 18446744073709551615, -9007199254740993, 12345678901234567890.1234567890, 1.5, 1, b'10100101',
     '0000-00-00', '2026-10-17 12:34:56', '2026-10-17 12:34:56', '-12:30:00', 2026,
     '{"a": [1, 2], "b": null}', x'DEADBEEF', x'01020304', 'happy', 'a,b', 'it''s \\ back', '中文 😀'),
    (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
SET SESSION sql_mode = DEFAULT;

-- Edge values: strict mode would reject the zero and partly-zero dates, so relax it for this one insert.
SET SESSION sql_mode = '';
INSERT INTO edge_values VALUES
    (1, '838:59:59', '-838:59:59', '12:34:56.789012', '2026-10-17 01:02:03.456789', '0000-00-00 00:00:00',
     '2026-00-15', 0.1, 'a long text value', x'00FF');
SET SESSION sql_mode = DEFAULT;

INSERT INTO `客戶備註` (`編號`, `內容`) VALUES (1, '測試');
INSERT INTO `group` (`id`, `order`) VALUES (1, 42);

INSERT INTO `Order Items` (`select`, `order`) VALUES ('first', 1);
INSERT INTO `tick``table` (`a``b`, label) VALUES (1, 'one');
INSERT INTO job_log (message) VALUES ('seeded');

INSERT INTO analytics.page_views (path)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 50)
SELECT CONCAT('/page/', n) FROM seq;
