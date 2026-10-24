"""Copy-data tests (06c): requests between two dev databases, scratch tables `zz_06c_*` made and dropped by exact name
(`data_support.dropping`), checks through each engine's own driver (`edit_support.ScratchDb.rows`)."""

from typing import Any
from uuid import UUID

from dbaguy_backend.domain.transfer import QuerySource, SourceSpec, TargetSpec, TransferRequest
from tests.data_support import SCRATCH_06C, DataApp, scratch_name
from tests.edit_support import ScratchDb


def table_name(kind: str = "t") -> str:
    return scratch_name(kind, SCRATCH_06C)


def profile(app: DataApp, db: ScratchDb, name: str) -> UUID:
    """A profile of `db` in the app (its password in memory only)."""
    return app.add_profile(db.profile(name=name))


def tables_request(
    source: ScratchDb, source_profile: UUID, names: list[str], target: ScratchDb, target_profile: UUID,
    mode: str = "create", *, filter: str | None = None, **target_fields: Any,
) -> TransferRequest:
    """Copy `names` of `source`'s schema into `target`'s schema."""
    return TransferRequest(
        source=SourceSpec(profile_id=source_profile, database=source.database,
                          tables=[source.path(name) for name in names], filter=filter),
        target=TargetSpec(profile_id=target_profile, database=target.database, schema_name=target.schema, mode=mode,
                          **target_fields),
    )


def query_request(source: ScratchDb, source_profile: UUID, sql: str, columns: list[dict], target: ScratchDb,
                  target_profile: UUID, table: str, mode: str = "create", **target_fields: Any) -> TransferRequest:
    return TransferRequest(
        source=SourceSpec(profile_id=source_profile, database=source.database,
                          query=QuerySource(sql=sql, columns=columns, title="Query 1")),
        target=TargetSpec(profile_id=target_profile, database=target.database, schema_name=target.schema, mode=mode,
                          table=table, **target_fields),
    )
