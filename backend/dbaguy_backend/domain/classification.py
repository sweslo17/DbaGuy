"""What the classifier says about a statement and why the safety policy holds it back.

Plain models without any SQL machinery, so the execution protocol can carry them without importing `sql/` (and,
through it, sqlglot).
"""

from enum import StrEnum

from pydantic import BaseModel


class StatementKind(StrEnum):
    READ = "read"
    WRITE = "write"
    DDL = "ddl"
    TRANSACTION = "transaction"
    SESSION = "session"
    UNKNOWN = "unknown"


class DangerReason(StrEnum):
    """Why a statement is dangerous: the standard safety level asks before running it."""

    UPDATE_WITHOUT_WHERE = "update_without_where"
    DELETE_WITHOUT_WHERE = "delete_without_where"
    DROP = "drop"
    TRUNCATE = "truncate"
    ALTER_DROP = "alter_drop"
    SYSTEM_KEYSPACE = "system_keyspace"  # SQL++: a write to system: (kills requests, drops prepared statements)
    MERGE_DELETE = "merge_delete"  # SQL++: MERGE … WHEN MATCHED THEN DELETE


class GuardReason(StrEnum):
    """Why the safety policy asks for confirmation (a danger, or any non-read statement at the strict level) or blocks
    a statement (a read-only connection). Every `DangerReason` is one, with the same value."""

    UPDATE_WITHOUT_WHERE = DangerReason.UPDATE_WITHOUT_WHERE.value
    DELETE_WITHOUT_WHERE = DangerReason.DELETE_WITHOUT_WHERE.value
    DROP = DangerReason.DROP.value
    TRUNCATE = DangerReason.TRUNCATE.value
    ALTER_DROP = DangerReason.ALTER_DROP.value
    SYSTEM_KEYSPACE = DangerReason.SYSTEM_KEYSPACE.value
    MERGE_DELETE = DangerReason.MERGE_DELETE.value
    STRICT_WRITE = "strict_write"
    STRICT_DDL = "strict_ddl"
    STRICT_TRANSACTION = "strict_transaction"
    STRICT_SESSION = "strict_session"
    STRICT_UNKNOWN = "strict_unknown"
    READ_ONLY = "read_only"


class Classification(BaseModel):
    kind: StatementKind
    dangerous_reasons: list[DangerReason] = []
