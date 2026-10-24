"""Data tools (spec ⑥): a job — a dump, a run of a SQL file, a copy between connections — runs on a session of its own
and ends with exactly one outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""

from datetime import datetime
from typing import ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .profiles import Engine

JobKind = Literal["dump", "run_file", "copy_data"]
# R-06-12. Dump: completed or failed (nothing written). Run, all or nothing: completed, rolled_back, partial (the file
# or a DDL ended the transaction), unknown (the link died during COMMIT). Run, statement by statement: completed,
# stopped, unknown (the link died while a statement ran). Any job: not_started (cancelled while queued, its session
# closed first, or refused before anything was sent). Copy (06c): completed (every table copied), partial (some were,
# then one failed or a Cancel stopped it), rolled_back (none was), unknown (what a table's COMMIT did, or a table that
# cannot roll back, is not known). A cancel is the `cancelled` flag on one of these, never an outcome.
JobOutcomeKind = Literal["completed", "failed", "rolled_back", "partial", "stopped", "unknown", "not_started"]
JobStage = Literal["starting", "schema", "data", "post_data", "statements", "commit"]

MAX_JOB_ERRORS = 100  # D-06-11: errors kept with their text ("skip and continue"); the rest are only counted
MAX_JOB_MESSAGES = 200  # D-06-11: server messages (NOTICE, PRINT) forwarded per job; the rest are only counted
PROGRESS_INTERVAL_S = 0.25  # D-06-11: at most one job_progress this often (and at each object's start and end)
MAX_JOBS_LISTED = 200  # the job list keeps this many; the oldest finished ones go first
ENGINE_NAMES: dict[Engine, str] = {  # what messages call an engine (the UI's ENGINE_LABELS)
    Engine.POSTGRES: "PostgreSQL", Engine.MYSQL: "MySQL", Engine.MSSQL: "SQL Server", Engine.SQLITE: "SQLite",
    Engine.COUCHBASE: "Couchbase",
}
RUN_SQL_FILES = "run SQL files"  # what a connection without `run_file` cannot do (spec §4)
BE_DUMPED = "be dumped"  # what a connection without `dump` cannot (spec §4)
# A dump's or a copy's filter that is not one condition on the rows (06a Task 9 fix M2): its text is select_builder's
# FILTER_NOT_A_CONDITION, never the value
INVALID_FILTER = "invalid_filter"


def cannot_read(path: str, exc: OSError) -> str:
    """Why a file a data tool reads cannot be read (Run SQL file's run and inspection)."""
    return f"Cannot read {path}: {exc.strerror or exc}"


def cannot(engine: Engine | str, action: str) -> str:
    """§4's refusal of a data tool the engine lacks (D-06-13): `Couchbase connections cannot be dumped`. The one text
    of both the REST routes (422 `not_supported`) and a job command on the WebSocket (pre-flight L4)."""
    return f"{ENGINE_NAMES[Engine(engine)]} connections cannot {action}"


class JobCommand(BaseModel):
    """A WebSocket command that starts a job (D-06-2): the worker runs every subclass through one path."""

    job_kind: ClassVar[JobKind]
    request_id: str


class JobError(BaseModel):
    """One statement that failed: where, the server's code and its own text (shown, never logged above DEBUG)."""

    line: int | None = None
    statement: int | None = None  # 1-based among the file's statements
    code: str | None = None
    message: str


class StoppedAt(BaseModel):
    """A place in a run's file: where it stopped (the statement that failed, was cancelled or was not sent), or where a
    run again starts (`resume_at`, 06b B1). `exact`: `line` is where that statement starts. False for a stop with no
    statement of its own (a line that is not UTF-8 inside a statement, a psql command, a statement over the limit, a
    read error): the line is where the run stopped, so a run again from it sends no `from_line` (06b review M-b)."""

    line: int
    statement: int
    # Always sent. The schema states no default, so the generated type has it optional: a place written by hand (a
    # test's fixture) is exact, and the frontend omits `from_line` only for `exact === false`
    exact: bool = Field(default=True, json_schema_extra=lambda schema: schema.pop("default", None))


# A table of a copy (06c, R-06-12): its rows are in, its transaction was rolled back, it never began, or what its
# COMMIT (or a table that cannot roll back) did is not known
TableOutcome = Literal["copied", "rolled_back", "not_started", "unknown"]


class JobObject(BaseModel):
    """One object of a dump, with the rows written for it; one table of a copy, with its outcome."""

    kind: str  # table, view, sequence, function, …
    name: str  # as the file names it: schema.name (a copy: the target table)
    rows: int = 0
    outcome: TableOutcome | None = None  # a copy's table
    source: str | None = None  # a copy's table: what it was copied from


class JobProgressEvent(BaseModel):
    """How far a job is (D-06-11): at most every 250 ms, and as each object starts and ends. Cumulative counts."""

    type: Literal["job_progress"] = "job_progress"
    request_id: str
    stage: JobStage = "starting"
    object: str | None = None  # the object being written or read (dump), schema.name
    rows: int = 0
    bytes: int = 0  # written (dump) or read (run) so far
    total_bytes: int | None = None  # the file's size (run)
    statements: int = 0  # sent (run)
    errors: int = 0  # skipped (run, statement by statement, continue)
    line: int | None = None  # where the run is


class JobOutcomeEvent(BaseModel):
    """How a job ended (R-06-12): sent once, right before the request's `session_state` and `finished`."""

    type: Literal["job_outcome"] = "job_outcome"
    request_id: str
    job: JobKind
    outcome: JobOutcomeKind
    cancelled: bool = False
    rows: int = 0
    bytes: int = 0
    statements: int = 0  # sent and done (run)
    path: str | None = None  # the file written (dump) or read (run)
    objects: list[JobObject] = []
    errors: list[JobError] = []  # the first MAX_JOB_ERRORS
    error_count: int = 0
    stopped_at: StoppedAt | None = None
    # Run SQL file, statement by statement: where a run again starts so that nothing is skipped or run twice — every
    # statement before it took effect (or failed and was skipped, in `errors`) and none from it on did. The earliest
    # statement of a file transaction that was rolled back, else where the run stopped (06b B1). One exception: an
    # `unknown` outcome with a `resume_at` lost the connection while a statement ran, and that statement may have taken
    # effect — check the target first (06b L13, review L5). None: all or nothing, read to the end with nothing
    # undone, or what the earlier statements did is not known (the warnings say which; a statement applied after a
    # file transaction the run rolled back, a `GO n` that stopped after some of its runs, a write that failed where
    # tables cannot roll back or on Couchbase's documents). `exact` False: send no `from_line` with it (M-b)
    resume_at: StoppedAt | None = None
    # Why it stopped or failed: the `error` event sent right before it, with where the run stopped when it did, so a
    # window that attaches later or lists the jobs (GET /data/jobs) knows it too (06a fix wave F3). None: no error
    error: JobError | None = None
    # What the outcome means, and the job's own warnings sent live as messages too (JOB_ROLLBACK_FAILED, F3)
    warnings: list[str] = []


class JobInfo(BaseModel):
    """A job of this backend run, as the Jobs list shows it (GET /data/jobs)."""

    session_id: str
    request_id: str
    job: JobKind
    title: str
    profile_id: UUID
    # A copy's source connection (06c): its sessions close when that connection is deleted, so the delete waits
    source_profile_id: UUID | None = None
    started_at: datetime
    progress: JobProgressEvent | None = None  # the last one sent
    outcome: JobOutcomeEvent | None = None  # set once the job ended


class DumpHeader(BaseModel):
    """The first lines of a dump DbaGuy wrote (spec ⑥ §3.3.2), as Run SQL file's inspection reads them back: a hint
    for the dialog, never a permission."""

    format: int
    engine: Engine
    server: str | None = None  # `PostgreSQL 17.11`
    scope: str | None = None  # `database dbaguy · schema public`
    created: str | None = None
    drop_first: bool = False
    # Only the rows of one table that match a filter (its Rows line, 06a fix wave F2): with drop_first, restoring the
    # file replaces that table with them
    filtered: bool = False
    filtered_table: str | None = None  # as the file names it (comment_safe: a line break is written as text)
    lines: list[str]  # the header's comment lines, as written


class FileInspection(BaseModel):
    """What Run SQL file's dialog shows of a file before it runs (POST /data/run-file/inspect)."""

    path: str
    size: int  # bytes
    header: DumpHeader | None = None  # a DbaGuy dump's header
    preview: str  # its first PREVIEW_LINES lines, at most PREVIEW_BYTES
    preview_truncated: bool
    warnings: list[str] = []


class FileExcerpt(BaseModel):
    """Lines of a file around one line (POST /data/run-file/excerpt): where an error of Run SQL file is, for its dialog
    (06b B2). Lines are counted at LF, as a run counts them; text that is not UTF-8 is shown replaced."""

    path: str
    first_line: int  # the line `text` starts at
    text: str  # at most EXCERPT_LINES lines and EXCERPT_BYTES, from first_line on
    found: bool  # the line asked for is in the file
    truncated: bool  # the file goes on after `text`


MAX_DUMP_NAMES = 500  # relations one `tables` scope may name


class DumpScope(BaseModel):
    """What a dump covers (spec ⑥ §3.3.1): a whole database (PostgreSQL, SQL Server), one schema (MySQL: a database;
    SQLite: main), or named tables and views of one schema."""

    kind: Literal["database", "schema", "tables"]
    schema_name: str | None = Field(default=None, min_length=1, max_length=1024)  # schema and tables
    names: list[str] = Field(default_factory=list, max_length=MAX_DUMP_NAMES)  # tables: the relations, in that schema

    @model_validator(mode="after")
    def _a_schema_for_schema_and_tables(self) -> Self:
        if self.kind != "database" and self.schema_name is None:
            raise ValueError("a schema or tables dump names its schema")
        if (self.kind == "tables") != bool(self.names):
            raise ValueError("a tables dump names its tables, and only a tables dump does")
        return self


class DumpObjectKinds(BaseModel):
    """Which objects besides tables a dump writes (R-06-2); a table's own sequences and column types always come
    with it. Routines include MySQL events; views include SQL Server synonyms."""

    views: bool = True
    sequences: bool = True
    types: bool = True
    routines: bool = True
    triggers: bool = True


DumpContent = Literal["schema", "data", "both"]
DUMP_FILTER_LENGTH = 65_536  # the longest filter a dump (and its plan) takes
FILTER_ONE_TABLE = "a filter applies to a dump of one table"


def filter_fits(scope: DumpScope, filter: str | None) -> bool:
    """A dump's filter is the table-data tab's: it applies to a `tables` scope of one name (the dump and its plan)."""
    return filter is None or (scope.kind == "tables" and len(scope.names) == 1)


class DumpPlanObject(BaseModel):
    """One object a dump writes under its own name, as the dump dialog lists it."""

    kind: str  # table, view, function, …
    name: str  # as the file names it: schema.name (MySQL, SQLite: name)
    rows_estimate: int | None = None  # ≈ the catalog's statistics (tables whose rows the dump writes)
    notes: list[str] = []


class DumpPlan(BaseModel):
    """What a dump of a scope would write (POST /data/dump/plan): the dialog's object list and warnings. Read on the
    metadata connection, outside any snapshot; the dump reads the catalog again inside its own.

    `objects` are the objects the content writes under their own names, in the file's order: schemas, extensions,
    collations, types, sequences, routines, tables, views, triggers, events, synonyms, rules, extended statistics and
    policies. The parts of another object are left to it: a table's indexes, foreign keys, check constraints,
    defaults and settings, a trigger's state, a sequence's owner and value, an identity's value, a table's row-level
    security switch, SQLite's header settings (user_version, application_id). A data-only plan lists the tables, parents
    first, then what only a data-only dump writes (`refresh`: PostgreSQL's materialized views, made again from the
    rows). `not_included` is the header's Not included list, word for word."""

    objects: list[DumpPlanObject]
    not_included: list[str]
    consistency: Literal["snapshot", "per_table"]
    warnings: list[str] = []
