from enum import StrEnum


class Dialect(StrEnum):
    """The SQL dialect of an engine: how its statements are split, classified, completed and quoted."""

    POSTGRES = "postgres"
    SQLITE = "sqlite"
    MYSQL = "mysql"
    TSQL = "tsql"
    SQLPP = "sqlpp"  # Couchbase SQL++ (N1QL)
