USE dbaguy;
GO
SET NOCOUNT ON;
INSERT INTO dbo.big_table (id, label, value)
SELECT TOP (100000) n, CONCAT(N'row ', n), n * 1.5
FROM (SELECT ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n FROM sys.all_columns a CROSS JOIN sys.all_columns b) AS seq;
INSERT INTO dbo.customers (email, name, profile) VALUES
    (N'ada@example.com', N'Ada Lovelace', N'{"vip": true, "score": 12345678901234567890}'),
    (N'lin@example.com', N'林小明', N'{"note": "中文資料"}'),
    (N'empty@example.com', N'', NULL);
INSERT INTO sales.orders (customer_id, total, note) VALUES
    (1, 12345678901234567890.1234567890, NULL), (1, 10.5, N'gift'), (2, 99.99, N'');
INSERT INTO dbo.types_demo (id, huge, amount, price, ratio, flag, guid, born, seen, precise, stamped, lasted, data, body, doc, anything, legacy) VALUES
    (1, 9223372036854775807, 12345678901234567890.1234567890, 12.3456, 1.5, 1, 'B9F3C1D2-0000-4000-8000-000000000001',
     '2026-10-18', '2026-10-18 12:34:56.123', '2026-10-18 12:34:56.1234567', '2026-10-18 12:34:56.1234567 +08:00',
     '12:34:56.1234567', 0xDEADBEEF, N'it''s 中文 😀', N'<a x="1"/>', CAST(42 AS sql_variant), N'中文'),
    (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, CAST(N'abc' AS sql_variant), NULL);
INSERT INTO dbo.[Order Items] ([select], [order]) VALUES (N'first', 1);
INSERT INTO dbo.[brack]]et] ([a]]b], label) VALUES (1, N'one');
INSERT INTO dbo.job_log (message) VALUES (N'seeded');
GO
USE analytics;
GO
SET NOCOUNT ON;
INSERT INTO dbo.page_views (path)
SELECT TOP (50) CONCAT(N'/page/', ROW_NUMBER() OVER (ORDER BY (SELECT NULL))) FROM sys.all_objects;
GO
-- Last: the mssql healthcheck reports healthy only once this table exists, so `compose up --wait` waits for the seed.
USE dbaguy;
GO
CREATE TABLE dbo.seed_done (at datetime2 NOT NULL DEFAULT SYSUTCDATETIME());
INSERT INTO dbo.seed_done DEFAULT VALUES;
GO
