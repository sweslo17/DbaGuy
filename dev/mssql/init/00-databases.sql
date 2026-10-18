-- DbaGuy dev SQL Server (2022). Run by mssql-init as sa. Fake credentials only.
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
