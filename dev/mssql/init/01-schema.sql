USE dbaguy;
GO
CREATE SCHEMA sales AUTHORIZATION dbo;
GO
CREATE TYPE dbo.email_address FROM nvarchar(200) NOT NULL;
GO
CREATE TYPE dbo.id_list AS TABLE (id int NOT NULL PRIMARY KEY);
GO
CREATE TABLE dbo.customers (
    id bigint IDENTITY(1, 1) NOT NULL CONSTRAINT customers_pk PRIMARY KEY,
    email dbo.email_address CONSTRAINT customers_email_uq UNIQUE,
    name nvarchar(200) NOT NULL,
    profile nvarchar(max) NULL CONSTRAINT customers_profile_json CHECK (profile IS NULL OR ISJSON(profile) = 1),
    created_at datetime2(3) NOT NULL CONSTRAINT customers_created_df DEFAULT SYSUTCDATETIME()
);
EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'Customer master data',
    @level0type = N'SCHEMA', @level0name = N'dbo', @level1type = N'TABLE', @level1name = N'customers';
GO
CREATE TABLE sales.orders (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT orders_pk PRIMARY KEY,
    customer_id bigint NOT NULL CONSTRAINT orders_customer_fk REFERENCES dbo.customers (id) ON DELETE CASCADE,
    total decimal(30, 10) NOT NULL,
    placed_at datetime2(0) NOT NULL CONSTRAINT orders_placed_df DEFAULT SYSUTCDATETIME(),
    note nvarchar(400) NULL
);
CREATE INDEX orders_customer_idx ON sales.orders (customer_id) INCLUDE (total) WHERE note IS NOT NULL;
GO
CREATE TABLE dbo.types_demo (
    id int NOT NULL PRIMARY KEY,
    huge bigint NULL,
    amount decimal(38, 10) NULL,
    price money NULL,
    ratio float NULL,
    flag bit NULL,
    guid uniqueidentifier NULL,
    born date NULL,
    seen datetime NULL,
    precise datetime2(7) NULL,
    stamped datetimeoffset(7) NULL,
    lasted time(7) NULL,
    data varbinary(max) NULL,
    body nvarchar(max) NULL,
    doc xml NULL,
    anything sql_variant NULL,
    legacy varchar(50) COLLATE Chinese_Taiwan_Stroke_CI_AS NULL,
    version rowversion NOT NULL
);
GO
CREATE TABLE dbo.big_table (id int NOT NULL PRIMARY KEY, label nvarchar(50) NOT NULL, value float NULL);
GO
-- Identifiers that need quoting: a space, reserved words, a closing bracket inside names (Review Focus 1 / Task 1).
CREATE TABLE dbo.[Order Items] (id int IDENTITY(1, 1) PRIMARY KEY, [select] nvarchar(50), [order] int);
CREATE TABLE dbo.[brack]]et] ([a]]b] int NOT NULL PRIMARY KEY, label nvarchar(20));
GO
-- CJK identifiers, a reserved word as a table name, and sql_variant values (a CJK one is the worst case for encodings).
CREATE TABLE dbo.[客戶備註] ([編號] int PRIMARY KEY, [內容] nvarchar(50));
CREATE TABLE dbo.[group] ([id] int PRIMARY KEY, [order] int);
CREATE TABLE dbo.variant_cases (id int PRIMARY KEY, v sql_variant);
GO
CREATE TABLE dbo.job_log (id int IDENTITY(1, 1) PRIMARY KEY, message nvarchar(200), created_at datetime2 DEFAULT SYSUTCDATETIME());
GO
CREATE SEQUENCE sales.order_seq AS bigint START WITH 1000 INCREMENT BY 10;
GO
CREATE SYNONYM dbo.customer_alias FOR dbo.customers;
GO
CREATE VIEW dbo.order_summary AS
SELECT c.name, COUNT(o.id) AS orders, SUM(o.total) AS total
FROM dbo.customers c LEFT JOIN sales.orders o ON o.customer_id = c.id
GROUP BY c.name;
GO
USE analytics;
GO
CREATE TABLE dbo.page_views (id bigint IDENTITY(1, 1) PRIMARY KEY, path nvarchar(200) NOT NULL, viewed_at datetime2 NOT NULL DEFAULT SYSUTCDATETIME());
GO
