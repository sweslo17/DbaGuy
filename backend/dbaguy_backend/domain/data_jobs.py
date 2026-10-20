"""Data tools (spec ⑥): a job — a dump, a run of a SQL file — runs on a session of its own and ends with exactly one
outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""

from datetime import datetime
from typing import ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .profiles import Engine

JobKind = Literal["dump", "run_file"]
# R-06-12. Dump: completed or failed (nothing written). Run, all or nothing: completed, rolled_back, partial (the file
# or a DDL ended the transaction), unknown (the link died during COMMIT). Run, statement by statement: completed,
# stopped, unknown (the link died while a statement ran). Any job: not_started (cancelled while queued, its session
# closed first, or refused before anything was sent). A cancel is the `cancelled` flag on one of these, never an outcome.
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
    """Where a run stopped: the statement that failed, was cancelled or was not sent."""

    line: int
    statement: int


class JobObject(BaseModel):
    """One object of a dump, with the rows written for it."""

    kind: str  # table, view, sequence, function, …
    name: str  # as the file names it: schema.name
    rows: int = 0


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
    warnings: list[str] = []


class JobInfo(BaseModel):
    """A job of this backend run, as the Jobs list shows it (GET /data/jobs)."""

    session_id: str
    request_id: str
    job: JobKind
    title: str
    profile_id: UUID
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
    lines: list[str]  # the header's comment lines, as written


class FileInspection(BaseModel):
    """What Run SQL file's dialog shows of a file before it runs (POST /data/run-file/inspect)."""

    path: str
    size: int  # bytes
    header: DumpHeader | None = None  # a DbaGuy dump's header
    preview: str  # its first PREVIEW_LINES lines, at most PREVIEW_BYTES
    preview_truncated: bool
    warnings: list[str] = []


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
    security switch, SQLite's header settings (user_version, application_id). A data-only plan lists the tables alone,
    parents first. `not_included` is the header's Not included list, word for word."""

    objects: list[DumpPlanObject]
    not_included: list[str]
    consistency: Literal["snapshot", "per_table"]
    warnings: list[str] = []
