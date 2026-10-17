-- DbaGuy dev MySQL (mysql:8.4). Runs once, as root, when the container is created. Fake credentials only.
-- Objects are created with the dev account as DEFINER so that the account DbaGuy connects with can read their DDL.
CREATE DATABASE analytics;
GRANT ALL ON analytics.* TO 'dbaguy'@'%';

USE dbaguy;

CREATE TABLE customers (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(200) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    profile JSON,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT = 'Customer master data';

CREATE TABLE orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id BIGINT UNSIGNED NOT NULL,
    total DECIMAL(30, 10) NOT NULL,
    placed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note TEXT,
    KEY orders_customer_idx (customer_id),
    CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

CREATE TABLE types_demo (
    id INT PRIMARY KEY,
    huge BIGINT UNSIGNED,
    big_signed BIGINT,
    amount DECIMAL(30, 10),
    ratio DOUBLE,
    flag TINYINT(1),
    bits BIT(8),
    born DATE,
    seen DATETIME,
    stamped TIMESTAMP NULL,
    lasted TIME,
    yr YEAR,
    doc JSON,
    data BLOB,
    code BINARY(4),
    mood ENUM('happy', 'sad'),
    tags SET('a', 'b'),
    note VARCHAR(100),
    body TEXT
);

CREATE TABLE big_table (id INT PRIMARY KEY, label VARCHAR(50) NOT NULL, value DOUBLE);

-- Identifiers that need quoting: a space, reserved words, a backtick inside names (Review Focus 3).
CREATE TABLE `Order Items` (id INT AUTO_INCREMENT PRIMARY KEY, `select` VARCHAR(50), `order` INT);
CREATE TABLE `tick``table` (`a``b` INT PRIMARY KEY, label VARCHAR(20));

CREATE TABLE job_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    message VARCHAR(200),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE DEFINER = `dbaguy`@`%` SQL SECURITY INVOKER VIEW order_summary AS
SELECT c.name, COUNT(o.id) AS orders, SUM(o.total) AS total
FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.name;

USE analytics;
CREATE TABLE page_views (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    path VARCHAR(200) NOT NULL,
    viewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
