"""Grid editing's preview (spec ⑤ §3.4): the table's EditTarget read on the profile's metadata connection (a read: its
one reconnect is fine), the plan built and checked on the request thread (`sql/dml.py`, pure: checking 10,000 changes
never holds up the tree or completion), and a one-time EditPlan for the tab's own session (`EditPlanStore`). A preview
writes nothing and can be asked again; nothing is cached (each preview reads the target again, so DDL is never missed).

Logs: INFO each preview's profile, table, counts, problem count and time; DEBUG the target read. Never a value or a key."""

import time
from uuid import UUID, uuid4

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, RowEditing
from ..adapters.editing import read_only_relation
from ..adapters.registry import AdapterRegistry
from ..domain.edits import EditChanges, EditPlan, EditPreview, EditTarget
from ..domain.errors import NotEditable, ReadOnlyProfile
from ..domain.metadata import PathSegment, path_database
from ..domain.profiles import AnyProfile
from ..sql.dml import build_plan
from .edit_plans import EditPlanStore
from .metadata_service import MetadataService
from .profile_service import ProfileService

READ_ONLY_REASON = "This connection is read-only"
NO_ROW_EDITING = "Rows of this connection cannot be edited in the grid"


class EditService:
    def __init__(
        self, metadata: MetadataService, profiles: ProfileService, registry: AdapterRegistry,
        plans: EditPlanStore | None = None,
    ) -> None:
        self._metadata = metadata
        self._profiles = profiles
        self._registry = registry
        self.plans = plans or EditPlanStore()

    def target(self, profile_id: UUID, path: list[PathSegment]) -> EditTarget:
        """How the rows of the table at `path` are identified and written. A read-only profile is answered without
        connecting: not editable, with the reason the toolbar shows (spec G8)."""
        profile, _ = self._admit(profile_id)
        if profile.read_only:
            return read_only_relation(path, READ_ONLY_REASON)
        return self._read_target(profile_id, path)

    def preview(self, profile_id: UUID, path: list[PathSegment], changes: EditChanges) -> EditPreview:
        """The exact statements a commit of `changes` sends, and a plan id when nothing is wrong with them."""
        started = time.monotonic()
        profile, adapter = self._admit(profile_id)
        if profile.read_only:
            raise ReadOnlyProfile()
        target = self._read_target(profile_id, path)
        if not target.editable:
            raise NotEditable(target.reason or NO_ROW_EDITING)
        built = build_plan(adapter.dialect, target, changes)
        plan_id = None
        if not built.problems:
            plan = EditPlan(
                plan_id=uuid4().hex, profile_id=profile_id, database=path_database(path), table=built.table,
                begin=built.begin, statements=tuple(built.statements), commit=built.commit,
                transactional=target.transactional, script=built.script,
            )
            self.plans.put(plan)
            plan_id = plan.plan_id
        counts = built.counts
        logger.info(
            "Edit preview for {} on {}: {} delete(s), {} update(s), {} insert(s), {} problem(s) in {} ms",
            profile.name, built.table, counts.deletes, counts.updates, counts.inserts, len(built.problems),
            int((time.monotonic() - started) * 1000),
        )
        return EditPreview(
            plan_id=plan_id, begin=built.begin, statements=built.statements, commit=built.commit,
            problems=built.problems, warnings=built.warnings, counts=counts, script=built.script,
        )

    def _admit(self, profile_id: UUID) -> tuple[AnyProfile, DatabaseAdapter]:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        if not adapter.capabilities.row_editing:
            raise NotEditable(NO_ROW_EDITING)
        return profile, adapter

    def _read_target(self, profile_id: UUID, path: list[PathSegment]) -> EditTarget:
        def read(connection: DatabaseConnection) -> EditTarget:
            if not isinstance(connection, RowEditing):
                raise NotEditable(NO_ROW_EDITING)
            return connection.edit_target(path)

        target = self._metadata.with_connection(profile_id, path_database(path), read)
        logger.debug("Edit target of {}: editable={}, key {}", path[-1].name if path else "-", target.editable,
                     target.key)
        return target
