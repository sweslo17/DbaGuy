"""Copy data's preview (spec ⑥ §3.5.1, POST /data/transfer/plan): which source columns go to which target columns,
with which types and conversions, the statements the copy adds (CREATE TABLE, DELETE, IDENTITY_INSERT, setval), its
warnings and problems; when nothing is wrong, the plan the copy job takes once (D-06-8: the copy runs what was
previewed). Reads the source and the target on their metadata connections (the relations' columns, the target's
edit target, the source's keys from the schema snapshot); nothing is written and no query is run.

Logs: INFO the two profiles, the table count, the mode and the problem count; never a value, a filter or a query's
text."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid4

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, DataTools, RowEditing
from ..adapters.columns import TableColumn
from ..adapters.dump import DumpTable, parents_first
from ..adapters.editing import not_writable_reason
from ..adapters.families import of_name
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import cannot
from ..domain.edits import EditTarget, edit_kind
from ..domain.errors import NotFound, NotSupported, PasswordRequired, ReadOnlyProfile, ValidationFailed
from ..domain.execution import ColumnMeta
from ..domain.metadata import PathSegment
from ..domain.profiles import AnyProfile, SqliteProfile
from ..domain.schema import SchemaSnapshot, SnapshotRelation
from ..domain.transfer import (
    COPY_DATA, ColumnMapping, IdentityChoice, TableTransfer, TargetSpec, TransferPlan, TransferProblem,
    TransferRequest,
)
from ..sql.dialects import Dialect, qualified
from ..sql.dump_script import RESTORE_CHECKS_FOREIGN_KEYS
from ..sql.select_builder import FILTER_NOT_A_CONDITION, build_copy_select, check_filter, relation_from_path
from ..sql.transfer_sql import advance_sequence, create_table, delete_rows, identity_switch, summary
from ..sql.type_map import conversion, engine_name, incompatible, planned_column, target_type, type_problem
from .metadata_service import MetadataService
from .profile_service import ProfileService
from .safety import is_plain_read
from .schema_cache import SchemaCache
from .transfer_plans import PlannedColumn, PlannedTable, StoredTransfer, TransferPlanStore

DEFAULT_SCHEMAS: dict[Dialect, str] = {Dialect.POSTGRES: "public", Dialect.TSQL: "dbo", Dialect.SQLITE: "main"}
QUERY_RUNS_AGAIN = "The query runs again when the copy starts, as an export does: its rows may differ from those shown"
FK_CYCLE = ("Foreign keys between these tables form a cycle: {} checks them as rows load, so a table may fail to "
            "copy")
CREATED_STAYS = ("MySQL commits a CREATE TABLE at once: if the copy of {} fails, the new table stays, empty")
NON_TRANSACTIONAL = ("{} cannot roll back (non-transactional): if the copy fails, the rows written before stay, and "
                     "what happened to the table is not known")
NON_TRANSACTIONAL_REPLACE = ("{} cannot roll back (non-transactional): its rows are deleted at once, and if the copy "
                             "fails they stay deleted, with only the rows written before")
# MySQL and SQLite targets copy with their foreign key checks off (TARGET_SETUP, R-06-9): said of an existing table with
# foreign keys of its own, and, when its rows are replaced, of one that others reference (06c pre-flight L2)
FK_UNCHECKED = ("{} has foreign keys, and {} does not check them during the copy: a row whose parent is missing goes in "
                "unchecked")
FK_NOT_ENFORCED = ("Other tables reference {}, and {} does not enforce their foreign keys during the copy: deleting its "
                   "rows neither cascades to theirs nor stops on them")
UNCHECKED_TARGETS = frozenset({Dialect.MYSQL, Dialect.SQLITE})
NOT_UNICODE = ("{} is not Unicode ({}): characters outside its code page become ?")
LEFT_OUT = "left out"
NOT_IN_TARGET = "no such column in the target"
COMPUTED = "the target computes it"
SKIPPED_SOURCE = "not copied: {}"  # the source's own reason (a generated column, rowversion, sql_variant, …)

T = TypeVar("T")


def _problem(code: str, message: str, column: str | None = None) -> TransferProblem:
    return TransferProblem(code=code, message=message, column=column)  # type: ignore[arg-type]


class _Source:
    """One source relation (or the query) as the plan reads it."""

    def __init__(self, key: str, name: str, schema: str | None, table: str, path: list[PathSegment] | None,
                 columns: list[TableColumn] | None, filter: str | None = None) -> None:
        self.key = key  # what the request's overrides, identity and left_out use
        self.name = name  # as messages say it
        self.schema = schema
        self.table = table
        self.path = path
        self.columns = columns  # None: it is gone
        self.filter = filter


def query_columns(dialect: Dialect, metas: Sequence[ColumnMeta]) -> list[TableColumn]:
    """A query result's columns as a copy reads them: their family from the type's name (or, failing that, from the
    grid's category, spec §3.5.2's last sentence); nullable, nothing else known."""
    found = []
    for meta in metas:
        named = of_name(dialect, meta.type_name, meta.category)
        found.append(TableColumn(
            name=meta.name, type_name=meta.type_name, family=named.family, nullable=True, has_default=False,
            kind=edit_kind(meta.category), compare="none", writable=True, precision=named.precision,
            scale=named.scale, length=named.length,
        ))
    return found


def _same_file(first: AnyProfile, second: AnyProfile) -> bool:
    if not (isinstance(first, SqliteProfile) and isinstance(second, SqliteProfile)):
        return False
    one, other = Path(first.path).expanduser(), Path(second.path).expanduser()
    try:
        return one.resolve() == other.resolve() or (one.exists() and other.exists() and one.samefile(other))
    except OSError:
        return False


def _find(snapshot: SchemaSnapshot | None, schema: str | None, name: str) -> SnapshotRelation | None:
    if snapshot is None:
        return None
    for each in snapshot.schemas:
        if schema is None or each.name == schema:
            for relation in each.relations:
                if relation.name == name:
                    return relation
    return None


def referenced_tables(snapshot: SchemaSnapshot | None) -> frozenset[tuple[str, str]]:
    """Each (schema, table) another table's foreign key references (a table's reference to itself is not counted)."""
    if snapshot is None:
        return frozenset()
    return frozenset((fk.ref_schema, fk.ref_table) for each in snapshot.schemas for other in each.relations
                     for fk in other.foreign_keys if (fk.ref_schema, fk.ref_table) != (each.name, other.name))


def foreign_key_warnings(dialect: Dialect, mode: str, shown: str, snapshot: SchemaSnapshot | None,
                         referenced: frozenset[tuple[str, str]], schema: str, name: str) -> list[str]:
    """What the preview says of an existing MySQL or SQLite target whose foreign keys the copy does not check (L2): its
    own, when appending or replacing; the ones of the tables that reference it (`referenced_tables`), when replacing
    its rows. From the target's schema snapshot, the completion cache's (no catalog query of its own, so a table made
    after it was built gets no warning): nothing when it lacks the table (a warning, never a refusal)."""
    if dialect not in UNCHECKED_TARGETS or mode == "create":
        return []
    relation = _find(snapshot, schema, name)
    if relation is None:
        return []
    engine = engine_name(dialect)
    said = [FK_UNCHECKED.format(shown, engine)] if relation.foreign_keys else []
    if mode == "replace_rows" and (schema, name) in referenced:
        said.append(FK_NOT_ENFORCED.format(shown, engine))
    return said


def _match(name: str, target: dict[str, TableColumn]) -> TableColumn | None:
    """The target column of that name: the same name first, else the one that differs only in case."""
    if name in target:
        return target[name]
    folded = [column for key, column in target.items() if key.casefold() == name.casefold()]
    return folded[0] if len(folded) == 1 else None


class TransferService:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService,
                 schema_cache: SchemaCache, plans: TransferPlanStore | None = None) -> None:
        self._profiles = profiles
        self._registry = registry
        self._metadata = metadata
        self._schema_cache = schema_cache
        self.plans = plans if plans is not None else TransferPlanStore()

    def plan(self, request: TransferRequest) -> TransferPlan:
        source, target = request.source, request.target
        source_profile, source_adapter = self._admit(source.profile_id)
        target_profile, target_adapter = self._admit(target.profile_id)
        if target_profile.read_only:
            raise ReadOnlyProfile()
        if target.database is None and target_adapter.capabilities.per_database_connection:
            # A connection that names no database copies into its own (PostgreSQL's and SQL Server's paths start with
            # it): the target's paths, the plan's binding and the same-table check all need its name
            database = self._on(target.profile_id, None, lambda c: c.current_database())
            target = target.model_copy(update={"database": database})
            request = request.model_copy(update={"target": target})
        source_dialect, target_dialect = source_adapter.dialect, target_adapter.dialect
        schema = target.schema_name or DEFAULT_SCHEMAS.get(target_dialect)
        if schema is None:
            raise ValidationFailed("Choose the schema (MySQL: the database) to copy into")
        problems: list[TransferProblem] = []
        warnings: list[str] = []
        sources = self._sources(request, source_dialect, problems, warnings)
        snapshot = self._snapshot(source.profile_id, source.database, "source's keys") if source.tables else None
        ordered, cycle = self._order(sources, snapshot)
        if cycle and target.mode != "create" and target_dialect in RESTORE_CHECKS_FOREIGN_KEYS:
            warnings.append(FK_CYCLE.format(engine_name(target_dialect)))
        if _same_file(source_profile, target_profile):
            problems.append(_problem("same_sqlite_file", "The source and the target are the same SQLite file: use "
                                                         "INSERT … SELECT in a query tab"))
        names = [target.table or item.table for item in ordered]
        existing = self._targets(target, target_adapter, schema, names)
        unchecked = target_dialect in UNCHECKED_TARGETS and target.mode != "create"
        keys = self._snapshot(target.profile_id, target.database, "target's foreign keys") if unchecked else None
        referenced = referenced_tables(keys)  # once: a server's snapshot may hold thousands of tables
        tables: list[TableTransfer] = []
        planned: list[PlannedTable] = []
        for item, name in zip(ordered, names, strict=True):
            view, made = self._table(request, item, name, schema, existing.get(name), source_dialect,
                                     target_dialect, target_adapter, source_profile, target_profile, snapshot)
            if existing.get(name) is not None and not view.problems:
                view.warnings += foreign_key_warnings(target_dialect, target.mode, view.target, keys, referenced,
                                                      schema, name)
            tables.append(view)
            if made is not None:
                planned.append(made)
        blocked = bool(problems) or any(table.problems for table in tables) or not tables
        plan_id = None if blocked else self._store(request, planned, source_profile, target_profile, target_adapter)
        logger.info("Copy plan from {} to {}: {} table(s), {}, {} problem(s)", source_profile.name, target_profile.name,
                    len(tables), target.mode, len(problems) + sum(len(table.problems) for table in tables))
        return TransferPlan(plan_id=plan_id, source_profile_id=source_profile.id, tables=tables, warnings=warnings,
                            problems=problems)

    def _on(self, profile_id: UUID, database: str | None, read: Callable[[DatabaseConnection], T]) -> T:
        """A read on a profile's metadata connection. The preview reads two connections, so a missing password says
        whose it is (`detail.profile_id`): the dialog asks for that profile's, not always the source's."""
        try:
            return self._metadata.with_connection(profile_id, database, read)
        except PasswordRequired as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise PasswordRequired(exc.message, {**detail, "profile_id": str(profile_id)}) from None

    def _admit(self, profile_id: UUID) -> tuple[AnyProfile, DatabaseAdapter]:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        if not adapter.capabilities.copy_data:
            raise NotSupported(cannot(profile.engine, COPY_DATA))
        return profile, adapter

    def _sources(self, request: TransferRequest, dialect: Dialect, problems: list[TransferProblem],
                 warnings: list[str]) -> list[_Source]:
        source = request.source
        if source.query is not None:
            query = source.query
            if not is_plain_read(query.sql, dialect):
                problems.append(_problem("query_not_select", "Only a statement that returns rows (SELECT, WITH, "
                                                             "VALUES…) can be copied, and one that only reads"))
            names = [meta.name for meta in query.columns]
            if len(set(names)) != len(names):
                twice = next(name for name in names if names.count(name) > 1)
                problems.append(_problem("duplicate_column", f"The result has two columns named {twice}: give them "
                                                             "names of their own (AS) and run it again", twice))
            warnings.append(QUERY_RUNS_AGAIN)
            return [_Source(query.title, query.title, None, query.title, None, query_columns(dialect, query.columns))]
        paths = source.tables or []

        def read(connection: DatabaseConnection) -> list[_Source]:
            if not isinstance(connection, DataTools):
                raise NotSupported(cannot(connection.server_info().engine, COPY_DATA))
            found = []
            for path in paths:
                schema, name = relation_from_path(path)
                shown = qualified(dialect, schema, name) if schema else name
                try:
                    columns: list[TableColumn] | None = connection.table_columns(path)
                except NotFound:
                    columns = None
                found.append(_Source(shown, shown, schema, name, path, columns, source.filter))
            return found

        return self._on(source.profile_id, source.database, read)

    def _snapshot(self, profile_id: UUID, database: str | None, what: str) -> SchemaSnapshot | None:
        """The source's keys (a new table's primary key) and foreign keys (the order), or a MySQL or SQLite target's
        foreign keys (L2): from the schema snapshot, built or cached for completion. Without it the copy runs anyway, in
        the order chosen, without keys and without that warning."""
        try:
            return self._schema_cache.get(profile_id, database)
        except Exception as exc:  # above DEBUG only its code: a server's text may echo what it read (D-06-12)
            logger.warning("The {} could not be read for a copy plan ({})", what,
                           getattr(exc, "code", type(exc).__name__))
            logger.opt(exception=exc).debug("The {} could not be read", what)
            return None

    @staticmethod
    def _order(sources: list[_Source], snapshot: SchemaSnapshot | None) -> tuple[list[_Source], bool]:
        """Parents first, by the source's foreign keys among these tables (R-06-9), else as chosen; and whether they
        form a cycle."""
        if len(sources) < 2 or snapshot is None:
            return sources, False
        key = "{}\x00{}".format  # schema and name: a name with a dot in it stays one name
        by_key = {key(item.schema, item.table): item for item in sources}
        tables = []
        for item in sources:
            relation = _find(snapshot, item.schema, item.table)
            refs = tuple(ref for ref in (key(fk.ref_schema if item.schema else None, fk.ref_table)
                                         for fk in (relation.foreign_keys if relation else [])) if ref in by_key)
            tables.append(DumpTable(schema=item.schema, table=item.table, name=key(item.schema, item.table),
                                    columns=(), references=refs))
        ordered, cycle = parents_first(tuple(tables))
        return [by_key[table.name] for table in ordered], cycle

    def _targets(self, target: TargetSpec, adapter: DatabaseAdapter, schema: str,
                 names: list[str]) -> dict[str, tuple[list[TableColumn], EditTarget] | None]:
        """Each target table's columns and what grid editing says of writing it (a view, a table that cannot be
        written as a whole), or None when there is no such table."""

        def read(connection: DatabaseConnection) -> dict[str, tuple[list[TableColumn], EditTarget] | None]:
            found: dict[str, tuple[list[TableColumn], EditTarget] | None] = {}
            for name in dict.fromkeys(names):
                path = adapter.relation_path(target.database, schema, name, "table")
                try:
                    columns = connection.table_columns(path) if isinstance(connection, DataTools) else []
                except NotFound:
                    found[name] = None
                    continue
                edit = connection.edit_target(path) if isinstance(connection, RowEditing) else None
                found[name] = (columns, edit) if edit is not None else (columns, EditTarget(path=path, editable=True))
            return found

        return self._on(target.profile_id, target.database, read)

    def _table(
        self, request: TransferRequest, item: _Source, name: str, schema: str,
        existing: tuple[list[TableColumn], EditTarget] | None, source_dialect: Dialect, target_dialect: Dialect,
        adapter: DatabaseAdapter, source_profile: AnyProfile, target_profile: AnyProfile,
        snapshot: SchemaSnapshot | None,
    ) -> tuple[TableTransfer, PlannedTable | None]:
        target = request.target
        shown = qualified(target_dialect, schema, name) if target_dialect is not Dialect.MYSQL else name
        view = TableTransfer(source=item.name, target=shown, source_key=item.key, source_path=item.path,
                             mode=target.mode, columns=[])
        if item.columns is None:
            view.problems.append(_problem("source_missing", f"{item.name} is not there any more"))
            return view, None
        same_database = (source_profile.id == target_profile.id
                         and (not adapter.capabilities.per_database_connection
                              or request.source.database == target.database))
        if same_database and item.path is not None and (item.schema or DEFAULT_SCHEMAS.get(source_dialect)) == schema \
                and item.table == name:
            view.problems.append(_problem("same_table", "The source and the target are the same table"))
            return view, None
        if target.mode == "create" and existing is not None:
            view.problems.append(_problem("target_exists",
                                          f"{shown} already exists: append to it, or choose another name"))
            return view, None
        if target.mode != "create" and existing is None:
            view.problems.append(_problem("target_missing",
                                          f"{shown} does not exist: create it, or choose another table"))
            return view, None
        if existing is not None and (refused := not_writable_reason(existing[1])) is not None:
            view.problems.append(_problem("target_not_writable", f"{shown} cannot be written: {refused}"))
            return view, None
        read = [column for column in item.columns if column.copied]
        if item.path is not None:
            for column in item.columns:
                if not column.copied:
                    view.columns.append(ColumnMapping(
                        source_name=column.name, source_type=column.type_name, family=column.family,
                        skipped=SKIPPED_SOURCE.format(column.not_copied_reason or "the server computes it")))
        left_out = set(target.left_out.get(item.key, []))
        if target.mode == "create":
            written, before, after, create_first = self._create(view, item, read, left_out, schema, name,
                                                                source_dialect, target_dialect, request, snapshot)
            transactional = True
        else:
            assert existing is not None
            written, before, after = self._append(view, item, read, left_out, schema, name, existing[0],
                                                  source_dialect, target_dialect, request)
            create_first = None
            transactional = existing[1].transactional
            if not transactional:
                said = NON_TRANSACTIONAL if target.mode == "append" else NON_TRANSACTIONAL_REPLACE
                view.warnings.append(said.format(shown))
        if not written and not view.problems:
            view.problems.append(_problem("no_columns", f"No column of {item.name} would be copied"))
        view.before, view.after = ([create_first] if create_first else []) + before, after
        select = self._select(view, item, read, source_dialect, request)
        if view.problems or select is None:
            return view, None
        path = tuple(adapter.relation_path(target.database, schema, name, "table"))
        return view, PlannedTable(
            source_name=item.name, target_name=shown, target_schema=schema, target_table=name, target_path=path,
            select=select, columns=tuple(read), written=tuple(written), before=tuple(before), after=tuple(after),
            created=target.mode == "create", create_first=create_first, transactional=transactional,
            filtered=item.filter is not None,
        )

    def _select(self, view: TableTransfer, item: _Source, read: list[TableColumn], dialect: Dialect,
                request: TransferRequest) -> str | None:
        """What the source runs: the tab's statement as it is, or the table's SELECT with its filter (one condition
        that only reads, as a dump's)."""
        if request.source.query is not None:
            return request.source.query.sql
        if not read:
            return None
        try:
            check_filter(dialect, item.filter)
            sql = build_copy_select(dialect, item.schema, item.table, read, item.filter)
        except ValidationFailed as exc:
            view.problems.append(_problem("invalid_filter", exc.message))
            return None
        if item.filter is not None and not is_plain_read(sql, dialect):
            view.problems.append(_problem("invalid_filter", FILTER_NOT_A_CONDITION))
            return None
        return sql

    def _create(
        self, view: TableTransfer, item: _Source, read: list[TableColumn], left_out: set[str], schema: str,
        name: str, source_dialect: Dialect, target_dialect: Dialect, request: TransferRequest,
        snapshot: SchemaSnapshot | None,
    ) -> tuple[list[PlannedColumn], list[str], list[str], str | None]:
        overrides = request.target.overrides.get(item.key, {})
        columns: list[tuple[str, str, bool]] = []
        written: list[PlannedColumn] = []
        for index, column in enumerate(read):
            mapping = ColumnMapping(source_name=column.name, source_type=column.type_name, family=column.family,
                                    type_editable=True)
            if column.name in left_out:
                view.columns.append(mapping.model_copy(update={"skipped": LEFT_OUT}))
                continue
            typed = overrides.get(column.name)
            if typed is not None:
                if (why := type_problem(target_dialect, typed)) is not None:
                    message = f"{column.name}: Not a single type: {why}"
                    view.problems.append(_problem("invalid_type", message, column.name))
                    view.columns.append(mapping.model_copy(update={"target_name": column.name, "target_type": typed,
                                                                   "overridden": True, "problem": message}))
                    continue
                type_sql, note = typed.strip(), None
                into = planned_column(column.name, type_sql, target_dialect)
            else:
                mapped = target_type(column, source_dialect, target_dialect)
                if mapped.type_sql is None:
                    message = mapped.problem or f"{column.name} has no type"
                    view.problems.append(_problem("unmappable_type", message, column.name))
                    view.columns.append(mapping.model_copy(update={"target_name": column.name, "problem": message}))
                    continue
                type_sql, note = mapped.type_sql, mapped.note
                into = planned_column(column.name, type_sql, target_dialect)
            notes = [note] if note else []
            notes += [said for said in conversion(column, into, source_dialect, target_dialect).notes
                      if said not in notes]
            if (why := incompatible(column, into)) is not None:
                view.problems.append(_problem("unconvertible", why, column.name))
            view.columns.append(mapping.model_copy(update={
                "target_name": column.name, "target_type": type_sql, "overridden": typed is not None,
                "conversion": "; ".join(notes) or None}))
            columns.append((column.name, type_sql, column.nullable))
            written.append(PlannedColumn(index, column.name))
        relation = _find(snapshot, item.schema, item.table) if item.path else None
        copied = {column for column, _, _ in columns}
        key = list(relation.primary_key) if relation and relation.primary_key and set(relation.primary_key) <= copied \
            else []
        ddl = create_table(target_dialect, schema, name, columns, key) if columns else ""
        if target_dialect is Dialect.MYSQL:
            view.warnings.append(CREATED_STAYS.format(name))
            return written, [], [], ddl or None
        return written, [ddl] if ddl else [], [], None

    def _append(
        self, view: TableTransfer, item: _Source, read: list[TableColumn], left_out: set[str], schema: str,
        name: str, target_columns: list[TableColumn], source_dialect: Dialect, target_dialect: Dialect,
        request: TransferRequest,
    ) -> tuple[list[PlannedColumn], list[str], list[str]]:
        choices: dict[str, IdentityChoice] = request.target.identity.get(item.key, {})
        by_name = {column.name: column for column in target_columns}
        used: set[str] = set()
        written: list[PlannedColumn] = []
        kept_identity: list[TableColumn] = []
        for index, column in enumerate(read):
            mapping = ColumnMapping(source_name=column.name, source_type=column.type_name, family=column.family)
            into = _match(column.name, by_name)
            if column.name in left_out:
                view.columns.append(mapping.model_copy(update={"skipped": LEFT_OUT}))
                continue
            if into is None:
                view.columns.append(mapping.model_copy(update={"skipped": NOT_IN_TARGET}))
                view.warnings.append(f"{column.name} is not copied: {name} has no such column")
                continue
            used.add(into.name)
            update: dict = {"target_name": into.name, "target_type": into.type_name}
            if into.generated or into.family == "rowversion" or not into.copied:
                view.columns.append(mapping.model_copy(update={**update, "skipped": COMPUTED}))
                continue
            if into.identity is not None:
                choice = choices.get(column.name, "keep")
                update["identity"] = choice
                if choice == "generate":
                    view.columns.append(mapping.model_copy(update={**update, "skipped": "the target generates it"}))
                    continue
                kept_identity.append(into)
            if (why := incompatible(column, into)) is not None:
                view.problems.append(_problem("unconvertible", why, column.name))
                update["problem"] = why
            notes = list(conversion(column, into, source_dialect, target_dialect).notes)
            if target_dialect is Dialect.TSQL and not into.unicode and column.family in ("char", "varchar", "text"):
                notes.append(NOT_UNICODE.format(into.name, into.type_name))
            update["conversion"] = "; ".join(notes) or None
            view.columns.append(mapping.model_copy(update=update))
            written.append(PlannedColumn(index, into.name))
        for column in target_columns:
            if column.name not in used and not column.nullable and not column.has_default and not column.generated:
                view.problems.append(_problem("missing_column", f"{name}.{column.name} needs a value (NOT NULL, no "
                                                                f"default) and no column of {item.name} goes into it"))
        before = [delete_rows(target_dialect, schema, name)] if request.target.mode == "replace_rows" else []
        after: list[str] = []
        if kept_identity and (switch := identity_switch(target_dialect, schema, name)) is not None:
            before.append(switch[0])
            after.append(switch[1])
        if target_dialect is Dialect.POSTGRES:
            # Only an integer column owns a sequence (an identity's, a serial's): setval takes a bigint, and a numeric
            # column's max() would not resolve (42883), failing every append into a table with a numeric default
            numbers = ("int16", "int32", "int64")
            after += [advance_sequence(schema, name, column.name) for column in target_columns
                      if column.name in {planned.target for planned in written} and column.family in numbers
                      and (column.identity is not None or column.has_default)]
        return written, before, after

    def _store(self, request: TransferRequest, planned: list[PlannedTable], source_profile: AnyProfile,
               target_profile: AnyProfile, target_adapter: DatabaseAdapter) -> str:
        source, target = request.source, request.target
        plan_id = uuid4().hex
        per_database = target_adapter.capabilities.per_database_connection
        statements = [statement for table in planned for statement in table.statements]
        text = summary(source_profile.name, target_profile.name,
                       [(table.source_name, table.target_name, target.mode) for table in planned],
                       target_adapter.dialect, statements)
        what = planned[0].source_name if len(planned) == 1 else f"{len(planned):,} tables"
        self.plans.put(StoredTransfer(
            plan_id=plan_id, profile_id=target_profile.id, database=target.database if per_database else None,
            source_profile_id=source_profile.id, source_database=source.database, query=source.query is not None,
            tables=tuple(planned), summary=text, title=f"Copy {what} from {source_profile.name} to {target_profile.name}",
        ))
        return plan_id
