USE dbaguy;
GO
CREATE PROCEDURE dbo.two_results @n int AS
BEGIN
    SET NOCOUNT ON;
    PRINT 'two_results starts';
    SELECT id, label FROM dbo.big_table WHERE id <= @n ORDER BY id;
    SELECT COUNT(*) AS total FROM dbo.big_table;
END;
GO
CREATE PROCEDURE dbo.fail_after_select AS
BEGIN
    SELECT 1 AS first;
    RAISERROR('boom after the first result', 16, 1);
END;
GO
CREATE PROCEDURE dbo.noisy AS
BEGIN
    PRINT 'one';
    RAISERROR('two', 0, 1) WITH NOWAIT;
    WAITFOR DELAY '00:00:02';
    RAISERROR('three', 10, 1);
    SELECT 4 AS four;
END;
GO
CREATE FUNCTION dbo.add_numbers (@a int, @b int) RETURNS int AS
BEGIN
    RETURN @a + @b;
END;
GO
CREATE FUNCTION sales.orders_of (@customer bigint) RETURNS TABLE AS
RETURN SELECT id, total FROM sales.orders WHERE customer_id = @customer;
GO
CREATE TRIGGER sales.orders_note ON sales.orders AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;
    UPDATE o SET note = N'' FROM sales.orders o JOIN inserted i ON i.id = o.id WHERE o.note IS NULL;
END;
GO
