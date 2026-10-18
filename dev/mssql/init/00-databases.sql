-- DbaGuy dev SQL Server (2022). Run by mssql-init as sa. Fake credentials only.
-- Re-runnable: seed.sh runs the whole directory whenever dbaguy.dbo.seed_done is missing, which is also the state after
-- a run that was stopped or failed half-way, so whatever an earlier run left behind is dropped first.
DECLARE @drop nvarchar(max) = N'';
SELECT @drop += N'ALTER DATABASE ' + QUOTENAME(name) + N' SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE ' + QUOTENAME(name) + N'; '
FROM sys.databases WHERE name IN (N'dbaguy', N'analytics', N'locked_away');
EXEC (@drop);
GO
IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'dbaguy') DROP LOGIN dbaguy;
GO
-- The dbaguy login owns nothing at the server level: like a real account, it sees only the databases it was given.
CREATE LOGIN dbaguy WITH PASSWORD = 'Dbaguy_dev1', CHECK_POLICY = OFF, DEFAULT_DATABASE = master;
GO
CREATE DATABASE dbaguy;
GO
CREATE DATABASE analytics;
GO
CREATE DATABASE locked_away;  -- dbaguy has no access: the tree must not list it
GO
USE dbaguy;
CREATE USER dbaguy FOR LOGIN dbaguy;
ALTER ROLE db_owner ADD MEMBER dbaguy;
GO
USE analytics;
CREATE USER dbaguy FOR LOGIN dbaguy;
ALTER ROLE db_owner ADD MEMBER dbaguy;
GO
ALTER LOGIN dbaguy WITH DEFAULT_DATABASE = dbaguy;
GO
