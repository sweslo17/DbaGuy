"""The one place production code reaches pymssql: `pymssql._mssql` with DbaGuy's module-wide settings applied.

`min_error_severity = 11`. SQL Server reports PRINT and RAISERROR with severity 0-10 as messages, not errors. pymssql's
default (6) raises an exception for severities 6-10 and so ends the batch on a mere message; with 11, only what the
server itself calls an error (severity 11 and above: a failed statement, `RAISERROR('x', 16, 1)`) raises.

Side effect on DB-Lib errors: FreeTDS's own errors (20003 timeout, 20004 read error, 20006 write error, 20009
connection failed, 20047 dead connection) pass through the same threshold, and all of them have a severity of 10 or
less. With 11 they never raise, and what is left of a dead connection is an `MSSQLDatabaseException` with number 0 and
the text "Unknown error" while `connected` stays True. Whoever needs to know whether the connection is alive asks the
server (the adapter runs a `SELECT 1`); it never reads an error text for that.

Importing this module is what sets the threshold, so every production module takes pymssql from here, never from
`pymssql` directly (test helpers may).
"""

from typing import Any

from pymssql import _mssql

_mssql.min_error_severity = 11  # severities 0-10 (PRINT, RAISERROR … 10) are messages; pymssql's default 6 ends the batch
ROW = _mssql.ROW_FORMAT_TUPLE
Connection = _mssql.MSSQLConnection
DatabaseError = _mssql.MSSQLDatabaseException
DriverError = _mssql.MSSQLDriverException
ERRORS = (_mssql.MSSQLDatabaseException, _mssql.MSSQLDriverException)


def connect(login_timeout: int, **kwargs: Any) -> Connection:
    """`_mssql.connect`. pymssql keeps the login timeout in a module global (FreeTDS applies it process-wide), so it is
    set right before each login."""
    _mssql.login_timeout = login_timeout
    return _mssql.connect(**kwargs)
