"""The tools that read rows (spec ⑧ §3.5, 'Read-only queries' access): run_query, explain and Couchbase's sample_fields.
Each call goes through the agent gate first (refused: History `refused`, nothing connects), then the reader (D-08-1,
D-08-3), and leaves one History record (`source=mcp`, the client's name, CR-08-3, P3): its outcome, its whole time and
the rows the agent got, whether it was refused, failed before its statement or ran. A History that cannot be written
never changes the answer, as for the user's own tabs. INFO says which tool, the connection's id, the outcome, rows and
time; never the SQL, a value, the server's text or the client's name (D-08-14, Task 2 ruling I-2: those are DEBUG)."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pydantic import ValidationError

from ..adapters.base import CONNECTION_LOST, StatementError
from ..adapters.couchbase.explain import analyze
from ..domain.couchbase import FieldSample, Keyspace
from ..domain.data_jobs import ENGINE_NAMES
from ..domain.errors import ProfileNotFound, QueryFailed
from ..domain.execution import QueryContext
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile, Engine, profile_database
from ..domain.well_formed import well_formed
from ..services.agent_gate import SERVER_READ_ONLY, agent_read
from ..services.field_sample_service import FieldSampleService
from ..services.history_service import HistoryService
from ..services.settings_service import SettingsService
from ..sql.dialects import Dialect
from .reader import CONNECTION_CLOSED, AgentReader, CallControl, ReadOutcome, ReadRequest
from .shape import ERROR_CHARS, MAX_CELL_DEFAULT, MAX_ROWS, MAX_ROWS_DEFAULT, RESPONSE_CHARS, compact, note
from .tools import AgentTools, Shared, ToolFailure, answered, checked_database, elapsed_ms

TIMEOUT_DEFAULT_S, TIMEOUT_MAX_S, TIMEOUT_RED_S = 30, 60, 30  # CR-08-8: red connections: 30 at most
SQL_MAX = 20_000  # CR-08-3: also a History record's longest text
SAMPLE_MAX = 1_000
EXPLAIN_CELL_CHARS = 30_000  # one plan cell; the rest of the response is its keys and notes
MORE_ROWS = "More rows exist: raise max_rows (at most 1000) or narrow the query"
TOO_BIG = ("The response is limited to about 40,000 characters: select fewer columns, lower max_cell_chars or narrow "
           "the query")
READ_CAPPED = ("Stopped after reading about 64 Mi characters of values: select fewer or smaller columns, or narrow the "
               "query")
PLAN_CUT = "The plan is longer than the response holds: it is cut"
COLUMNS_CUT = "{} more columns left out: select fewer columns"
_LOWERED = "timeout_s was lowered to {} s: {}"
_TIMEOUT = "Stopped after {:g} s (timeout_s; at most 60, 30 on production connections)"
_CANCELLED = "Cancelled by the client"
_READ_ONLY_NOTE = " (DbaGuy's MCP access is read-only)"
_NOT_COUCHBASE = "sample_fields reads Couchbase collections: '{}' is a {} connection"
_CONTEXT_ONLY_COUCHBASE = "query_context is for Couchbase connections; use `database` for '{}'"
_BAD_CONTEXT = "query_context is `bucket` or `bucket.scope`: {}"
_LONG_CONTEXT = "a bucket's name is at most 100 characters, a scope's 251"
_BAD_KEYSPACE = "Couchbase names a bucket in at most 100 characters, a scope and a collection in at most 251"
_SHOWPLAN = "-- SHOWPLAN_TEXT (the statement was not run)\n"
# The reader's codes for a session that closed or lost its server under the call (Task 6 carry): not the server's error
_CONNECTION_GONE = frozenset({CONNECTION_LOST, CONNECTION_CLOSED})
# Engines whose agent session has no database of the agent's choosing (AgentSessions.key): SQLite has one; Couchbase's
# requests carry a query_context instead
_NO_DATABASE = frozenset({Engine.SQLITE, Engine.COUCHBASE})


def _cut(message: str, code: str | None = None) -> str:
    """A server's or driver's message, cut at ERROR_CHARS (review I4: it can quote a value of any size the agent
    built), then its code."""
    message = note(message, ERROR_CHARS)
    return f"{message} ({code})" if code else message


class QueryTools:
    def __init__(self, tools: AgentTools, reader: AgentReader, history: HistoryService, samples: FieldSampleService,
                 settings: SettingsService) -> None:
        self._tools = tools
        self._reader = reader
        self._history = history
        self._samples = samples
        self._settings = settings

    # ---- run_query, explain ----

    def run_query(self, connection: str, sql: str, database: str | None = None, query_context: str | None = None,
                  max_rows: int = MAX_ROWS_DEFAULT, max_cell_chars: int = MAX_CELL_DEFAULT,
                  timeout_s: float | None = None, *, client: str | None = None,
                  control: CallControl | None = None) -> dict[str, Any]:
        shared = self._tools.shared(connection, "run_query", "query")
        request, notes = self._request(shared, "run_query", sql, sql, database, query_context, timeout_s, client)
        request = replace(request, max_rows=max_rows, max_cell_chars=max_cell_chars)
        outcome = self._read("run_query", shared, request, sql, client, control)
        if outcome.columns_dropped:  # review I5
            notes.append(COLUMNS_CUT.format(outcome.columns_dropped))
        if outcome.read_capped:  # one note at most says why it stopped
            notes.append(READ_CAPPED)
        elif outcome.rows_dropped:
            notes.append(TOO_BIG)
        elif outcome.more_rows:
            notes.append(MORE_ROWS)
        return {
            "columns": outcome.columns, "rows": outcome.rows, "row_count": len(outcome.rows),
            "more_rows": outcome.more_rows,
            "truncated": {"rows_dropped_for_size": outcome.rows_dropped} if outcome.rows_dropped else None,
            "elapsed_ms": outcome.elapsed_ms, "notes": notes + outcome.notes,
        }

    def explain(self, connection: str, sql: str, database: str | None = None, query_context: str | None = None,
                timeout_s: float | None = None, *, client: str | None = None,
                control: CallControl | None = None) -> dict[str, Any]:
        """D-08-10: the plan of one read, without running it (no ANALYZE); Couchbase adds its findings. History keeps
        what was sent: `EXPLAIN <statement>`, or SQL Server's statement marked as a SHOWPLAN (refused ones too, so an
        explain's refusal never merges with run_query's of the same text)."""
        shared = self._tools.shared(connection, "explain", "query")
        dialect = shared.adapter.dialect
        recorded = (_SHOWPLAN + sql) if dialect == Dialect.TSQL else f"EXPLAIN {sql}"
        request, notes = self._request(shared, "explain", sql, recorded, database, query_context, timeout_s, client)
        request = replace(request, explain=True, max_rows=MAX_ROWS, max_cell_chars=EXPLAIN_CELL_CHARS)
        outcome = self._read("explain", shared, request, recorded, client, control)
        plan, cut = _plan_text(dialect, outcome)
        result: dict[str, Any] = {"plan": plan, "elapsed_ms": outcome.elapsed_ms, "notes": notes + outcome.notes}
        if shared.adapter.capabilities.plan_format == "couchbase":
            result["findings"] = [] if cut else _findings(outcome)
        if cut:
            result["notes"].append(PLAN_CUT)
        return result

    def _request(self, shared: Shared, tool: str, sql: str, recorded: str, database: str | None,
                 query_context: str | None, timeout_s: float | None,
                 client: str | None) -> tuple[ReadRequest, list[str]]:
        """The gate (refused: History, INFO, `refused`; nothing connects), then the arguments (one the tool cannot take
        is invalid_argument, as the SDK's own checks, and leaves no record: review M1 put the gate first, so a statement
        is judged and recorded whatever came with it), then the timeout for this connection (CR-08-8). `recorded`: the
        call's History text."""
        profile, dialect = shared.profile, shared.adapter.dialect
        verdict = agent_read(sql, dialect)
        if not verdict.allowed:
            self._keep(self._history.refused, self._record(profile, recorded, self._shown_as_given(
                shared, database, query_context), datetime.now(UTC), 0, 0, "refused", verdict.message(), client))
            logger.info("MCP refused {} on {}: {}", tool, profile.id, verdict.refusal)
            logger.debug("MCP refused {} on {} by {!r}: {!r}", tool, profile.id, client, sql[:300])
            raise ToolFailure("refused", verdict.message(), logged=True, connection=str(profile.id))
        database = checked_database(database)
        context = self._context(shared, query_context)
        notes: list[str] = []
        red = profile.color == "red"
        limit = TIMEOUT_RED_S if red else TIMEOUT_MAX_S
        timeout = float(timeout_s if timeout_s is not None else TIMEOUT_DEFAULT_S)
        if timeout > limit:
            notes.append(_LOWERED.format(limit, "a production (red) connection" if red else "the most a call may take"))
            timeout = float(limit)
        return ReadRequest(profile=profile, dialect=dialect, sql=sql, query_context=context, timeout_s=timeout,
                           database=None if profile.engine in _NO_DATABASE else database,
                           admit=lambda: self._tools.shared(str(profile.id), tool, "query")), notes

    @staticmethod
    def _context(shared: Shared, text: str | None) -> QueryContext | None:
        """Couchbase's `bucket` or `bucket.scope` (None: the profile's default); an error on any other engine."""
        if text is None:
            return None
        if not shared.adapter.capabilities.query_context:
            raise ToolFailure("invalid_argument", _CONTEXT_ONLY_COUCHBASE.format(shared.profile.name))
        try:
            return QueryContext.parse(text)
        except ValidationError:  # a name too long: pydantic's text would quote it back whole
            raise ToolFailure("invalid_argument", _BAD_CONTEXT.format(_LONG_CONTEXT)) from None
        except ValueError as exc:
            raise ToolFailure("invalid_argument", _BAD_CONTEXT.format(exc)) from None

    def _shown_as_given(self, shared: Shared, database: str | None, query_context: str | None) -> str | None:
        """A refusal's database, from arguments not checked yet: what they name when they are valid, else nothing."""
        try:
            return self._shown(shared, checked_database(database), self._context(shared, query_context))
        except ToolFailure:
            return self._shown(shared, None, None)

    @staticmethod
    def _shown(shared: Shared, database: str | None, context: QueryContext | None) -> str | None:
        """History's database: Couchbase's context (the profile's default without one), else the database the call
        named or the profile's own."""
        profile = shared.profile
        if shared.adapter.capabilities.query_context:
            return context.text() if context is not None else profile_database(profile)
        if profile.engine in _NO_DATABASE:
            return None
        return database or profile_database(profile)

    def _read(self, tool: str, shared: Shared, request: ReadRequest, recorded: str, client: str | None,
              control: CallControl | None) -> ReadOutcome:
        """The reader's outcome, its History record and log line; an outcome that is not `ok` raises its §4 error."""
        profile = shared.profile
        shown = self._shown(shared, request.database, request.query_context)
        started, clock = datetime.now(UTC), time.monotonic()
        with self._recorded(profile, recorded, shown, started, clock, client):
            with answered(profile), self._still_shared(shared, tool):
                outcome = self._reader.read(request, control)
        failure = None if outcome.status == "ok" else self._failure(shared, request, outcome)
        refused = failure is not None and failure.code == "refused"
        status: HistoryStatus = "refused" if refused else outcome.status
        if status == "ok" and control is not None and control.cancelled:
            status = "cancelled"  # review M2: the client gave up as it ended (its frame's end): it never got the rows
        record = self._record(profile, recorded, shown, started, outcome.elapsed_ms, len(outcome.rows), status,
                              outcome.error_message if outcome.status != "ok" else None, client)
        self._keep(self._history.refused if refused else self._history.record, record)
        logger.info("MCP {} on {}: {}, {} rows, {} ms", tool, profile.id, outcome.error_code or status,
                    len(outcome.rows), outcome.elapsed_ms)
        logger.debug("MCP {} on {} by {!r}: {!r}{}", tool, profile.id, client, request.sql[:300],
                     f", {outcome.error_message[:1000]!r}" if outcome.error_message else "")
        if failure is not None:
            raise failure
        return outcome

    @contextmanager
    def _still_shared(self, shared: Shared, tool: str) -> Iterator[None]:
        """Review I1: the profile went away during the call (deleted): `unknown_connection`, as the next call gets."""
        try:
            yield
        except ProfileNotFound:
            self._tools.shared(str(shared.profile.id), tool, "query")  # raises unknown_connection now
            raise

    @contextmanager
    def _recorded(self, profile: AnyProfile, sql: str, shown: str | None, started: datetime, clock: float,
                  client: str | None) -> Iterator[None]:
        """A call that never reached its statement (a password, a session that cannot open, busy, a setting the
        session is refused for) is recorded too: `refused` for a refusal (merged like the gate's), else `error`."""
        try:
            yield
        except ToolFailure as failure:
            refused = failure.code == "refused"
            record = self._record(profile, sql, shown, started, elapsed_ms(clock), 0,
                                  "refused" if refused else "error", failure.message, client)
            self._keep(self._history.refused if refused else self._history.record, record)
            raise
        except Exception:
            record = self._record(profile, sql, shown, started, elapsed_ms(clock), 0, "error", "Internal error", client)
            self._keep(self._history.record, record)
            raise

    @staticmethod
    def _failure(shared: Shared, request: ReadRequest, outcome: ReadOutcome) -> ToolFailure:
        """§4's error for an outcome that is not `ok`; its log line is written already."""
        connection = str(shared.profile.id)

        def failed(code: Any, message: str) -> ToolFailure:
            return ToolFailure(code, message, logged=True, connection=connection)

        if outcome.status in ("timeout", "cancelled"):
            text = _TIMEOUT.format(request.timeout_s) if outcome.status == "timeout" else _CANCELLED
            return failed(outcome.status, f"{text}. {outcome.error_message}" if outcome.error_message else text)
        code, message = outcome.error_code, _cut(outcome.error_message or "The query failed")
        if code == "read_only":  # the worker's backstop: the gate's verdict, again
            return failed("refused", message)
        if code == "session_changed":
            return failed("session_changed", message)
        if code in _CONNECTION_GONE:
            return failed("connection_failed", message)
        if code in SERVER_READ_ONLY.get(shared.adapter.dialect, ()):
            message += _READ_ONLY_NOTE
        return failed("query_failed", f"{message} ({code})" if code else message)

    @staticmethod
    def _record(profile: AnyProfile, sql: str, database: str | None, started: datetime, elapsed_ms: int, rows: int,
                status: HistoryStatus, error: str | None, client: str | None) -> HistoryRecord:
        """Well-formed (an agent's JSON can carry a lone surrogate, "\\ud800", which the history file cannot store:
        it is kept as U+FFFD) and at most SQL_MAX characters (an explain's text adds its head to the agent's 20,000).
        The client's name is `server.client_name`'s: at most 80 characters, no control characters."""
        return HistoryRecord(
            profile_id=profile.id, profile_name=profile.name, database=well_formed(database) if database else None,
            sql=well_formed(sql)[:SQL_MAX],
            started_at=started, elapsed_ms=elapsed_ms, rows=rows, status=status,
            error=well_formed(error) if error else None, source="mcp", client=well_formed(client) if client else None,
        )

    @staticmethod
    def _keep(write: Callable[[HistoryRecord], object], record: HistoryRecord) -> None:
        """Write the call's record. A History that cannot be written never changes the call's answer: logged, as the
        worker's `_notify` does for the user's own tabs."""
        try:
            write(record)
        except Exception as exc:
            logger.warning("MCP: the History record of a call on {} could not be written ({})", record.profile_id,
                           type(exc).__name__)
            logger.opt(exception=exc).debug("MCP: the History write failed")

    # ---- sample_fields ----

    def sample_fields(self, connection: str, bucket: str, scope: str, collection: str, size: int | None = None, *,
                      client: str | None = None) -> dict[str, Any]:
        """A Couchbase collection's field paths from a sample of its documents (④ R-04-11): the one already taken
        when there is one and no size is asked for, else a new sample on the agents' catalog connection (D-08-11)."""
        clock, started = time.monotonic(), datetime.now(UTC)
        shared = self._tools.shared(connection, "sample_fields", "query")
        profile = shared.profile
        if profile.engine != Engine.COUCHBASE:
            raise ToolFailure("not_allowed", _NOT_COUCHBASE.format(profile.name, ENGINE_NAMES[Engine(profile.engine)]))
        try:
            keyspace = Keyspace(bucket=bucket, scope=scope, collection=collection)
        except ValidationError:
            raise ToolFailure("invalid_argument", _BAD_KEYSPACE) from None
        summary = f"-- MCP sample_fields {'.'.join((bucket, scope, collection))}"
        with self._recorded(profile, summary, None, started, clock, client):
            with answered(profile), self._still_shared(shared, "sample_fields"):
                sample = self._samples.cached(profile.id, keyspace) if size is None else None
                if sample is None:
                    wanted = min(size or self._settings.get().couchbase.sample_size, SAMPLE_MAX)
                    sample = self._sampled(profile, keyspace, wanted)
        elapsed = elapsed_ms(clock)
        self._keep(self._history.record, self._record(profile, f"{summary} ({sample.documents} documents)", None,
                                                      started, elapsed, sample.documents, "ok", None, client))
        logger.info("MCP sample_fields on {}: ok, {} documents, {} paths, {} ms", profile.id, sample.documents,
                    len(sample.fields), elapsed)
        logger.debug("MCP sample_fields on {} by {!r}: {!r}", profile.id, client, summary)
        return _sample_result(sample)

    def _sampled(self, profile: AnyProfile, keyspace: Keyspace, size: int) -> FieldSample:
        """A new sample on the agents' catalog connection; a server's error ends with its code, as run_query's."""
        try:
            return self._samples.sample(profile.id, keyspace, size, agent=True)
        except (QueryFailed, StatementError) as exc:
            cause = exc.__cause__ if isinstance(exc, QueryFailed) else exc  # server_errors_are_final keeps it
            raise ToolFailure("query_failed", _cut(exc.message, getattr(cause, "code", None))) from None


def _sample_result(sample: FieldSample) -> dict[str, Any]:
    """The sample's paths (SQL++'s way of writing each, its JSON kinds, how many documents have it) while the response
    has room, then a note saying how many were left out."""
    result: dict[str, Any] = {
        "keyspace": ".".join((sample.keyspace.bucket, sample.keyspace.scope, sample.keyspace.collection)),
        "sampled_at": sample.sampled_at.isoformat(), "documents": sample.documents, "fields": [],
        "paths_capped": sample.paths_capped, "depth_capped": sample.depth_capped,
        "long_keys_skipped": sample.long_keys_skipped, "cut_at_bytes": sample.cut_at_bytes,
    }
    room = RESPONSE_CHARS - len(compact(result)) - 200
    for path in sample.fields:
        entry = {"path": path.display, "types": path.types, "docs": path.docs}
        size = len(compact(entry)) + 1
        if size > room:
            result["notes"] = [f"{len(sample.fields) - len(result['fields'])} more paths left out (response size)"]
            break
        result["fields"].append(entry)
        room -= size
    return result


def _plan_text(dialect: Dialect, outcome: ReadOutcome) -> tuple[str, bool]:
    """The plan's rows as text, and whether it is cut (rows the response had no room for, or a cell cut at
    EXPLAIN_CELL_CHARS). One column: a line per row (PostgreSQL's QUERY PLAN, MySQL's tree, SQL Server's StmtText,
    Couchbase's JSON). SQLite's EXPLAIN QUERY PLAN rows (id, parent, notused, detail): an indented tree. Several columns
    otherwise (MySQL before 8.0.16 has no tree): a header line, then each row's cells joined."""
    rows = outcome.rows
    cut = outcome.more_rows or any(isinstance(cell, dict) and cell.get("truncated") for row in rows for cell in row)
    if dialect == Dialect.SQLITE:
        depth: dict[Any, int] = {}
        lines = []
        for row in rows:
            node, parent, detail = row[0], row[1], row[-1]
            depth[node] = depth.get(parent, -1) + 1
            lines.append("  " * depth[node] + _cell_text(detail))
        return "\n".join(lines), cut
    if len(outcome.columns) > 1:
        header = " | ".join(str(column["name"]) for column in outcome.columns)
        return "\n".join([header, *(" | ".join(_cell_text(cell) for cell in row) for row in rows)]), cut
    return "\n".join(_cell_text(row[0]) for row in rows if row), cut


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") if cell.get("$type") == "json" else cell.get("preview", ""))
    return "" if cell is None else str(cell)


def _findings(outcome: ReadOutcome) -> list[str]:
    """Couchbase's plan findings (④ explain.py: primary scans, uncovered index scans…) of its one JSON row."""
    if not outcome.rows or not isinstance(outcome.rows[0][0], dict) or outcome.rows[0][0].get("$type") != "json":
        return []
    return [finding.text for finding in analyze(outcome.rows[0][0]["text"]).findings]
