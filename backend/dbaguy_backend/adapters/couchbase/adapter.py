"""Couchbase through the Query Service REST API (no SDK, R-04-1): SQL++ sent with the session's options, the response
streamed and shaped by result.py, cancel by shutting the socket down from any thread (the server marks the request
`closed`, R-04-5), read-only enforced by the server (`readonly=true`) as well as the classifier, and no transactions
(R-04-4).

Threads: the worker thread that owns a connection does everything except cancel(), force_cancel() and abort(), which
only flip a flag under a leaf lock and shut down the socket duplicate of the request's channel (http.py). No other
state is touched by two threads (the ③ lesson)."""

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from loguru import logger

from ...domain.couchbase import FieldSample, Keyspace
from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import QueryContext, StatementParams
from ...domain.metadata import NodeKind, ObjectDetail, ObjectNode, PathSegment, ServerInfo, child_path
from ...domain.profiles import AnyProfile, Engine, ServerProfile
from ...domain.schema import RelationKind, SchemaSnapshot
from ...sql.classifier import is_transaction_statement
from ...sql.dialects import Dialect
from ...sql.lexer import first_keyword
from ...sql.splitter import split
from ...version import app_version
from ..base import (
    INVALID_PARAMS,
    INVALID_TEXT,
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    FieldSampling,
    ParsedUrl,
    RunOptions,
    StatementCancelled,
    StatementError,
    StatementNotSent,
    StatementResult,
    invalid_text_error,
)
from ..cancel_window import CancelWindow
from ..documents import DocumentCapable, DocumentEngine, DocumentRules, DocumentStore
from . import catalog, sampling
from .documents import SqlppDocumentRules, SqlppDocumentStore
from .http import (
    ChannelError, HttpChannel, LiteralNotJson, NodeClient, QueryRequest, TlsFailure, basic_auth, build_body,
    client_context_id,
)
from .messages import PROTOCOL_ERROR, is_server_error
from .nodes import candidate_nodes
from .params import parameter_warnings
from .result import CouchbaseResult, EventStream, start_result
from .rows import has_bare_star
from .settings import (
    QUERY_PORT,
    URL_SCHEMES,
    CouchbaseSettings,
    connection_settings,
    endpoint_target,
    parse_couchbase_url,
    request_context,
)
from .stream import RawRow

# R-04-6: a paused request holds a query servicer; the worker closes it after this (+ up to one 15 s housekeeping
# pass). A13 (7.6.12): the server itself ends a request whose writes have stalled 77–86 s after the client stopped
# reading (1203 "Slow/stalled client write timed out", the rest of the response cut), so the worker must close it
# first, with its own message, well before 77 s: 45–60 s.
PAUSED_RESULT_MAX_IDLE_S = 45.0
# R-04a-T11a: the row memory a paused result reads ahead into (spec §8 Q1 「預讀上限 8 MiB」), counted by
# `sys.getsizeof` of each row's text (result.row_cost): a 20,000-row collection page-through (≈ 3 MB held) fits and is
# read to its end, so the server holds nothing for it; the same cap as the `*` peek (rows.STAR_PEEK_BYTES).
READ_AHEAD_BYTES = 8 * 1024 * 1024
TRANSACTIONS_UNSUPPORTED = "transactions_unsupported"
_NO_TRANSACTIONS = ("SQL++ transactions (BEGIN WORK, COMMIT, ROLLBACK, SAVEPOINT) are not supported in DbaGuy yet; "
                    "every statement commits on its own")
_IDENTIFY = "SELECT ds_version() AS v, CURRENT_USERS() AS u"
_VERSION = "SELECT ds_version() AS v"  # A17: for a user that may not call CURRENT_USERS()
_DML = frozenset({"INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE"})
_PLANS = frozenset({"EXPLAIN", "ADVISE"})
_PROFILE = "timings"  # a run with profile (R-04-13): the answer ends with `profile.executionTimings` (A16)
# How a wrong user name or password comes back: 7.6.12 answers 401 with JSON code 2120 "Failure to authenticate user"
# (A14); 10000 and 13014 are the older and the privilege codes (spec §3.6.3); http_401 a 401 that is not JSON.
_AUTH_FAILURES = frozenset({"2120", "10000", "13014", "http_401"})
_AUTH_FAILED = "auth_failed"
_TLS_FAILED = "tls_failed"
_NOT_FOUND = "http_404"
_NODE_DOWN = "node_down"
_BAD_REQUEST_TEXT = "The query context or a parameter holds a character that is not valid text"
_REASON_LIMIT = 100

# What a failed TLS setup suggests, by cause (R-04a-F1); no hint for TlsFailure.OTHER: the OpenSSL text alone.
_TLS_HINTS = {
    TlsFailure.HOSTNAME: "The certificate does not name this host: connect by a name it holds, or use SSL mode verify-ca",
    TlsFailure.ISSUER: "Set the cluster's CA file in the SSL tab (the app does not read the macOS keychain)",
    TlsFailure.NOT_TLS: "This port does not speak TLS: the query service uses 18093 for TLS (8093 is plain HTTP)",
}
# The cluster manager (8091, 18091) answers a query request with 404; so does anything else that is not the service.
_NOT_QUERY_SERVICE = "This port is not the query service (8093, or 18093 with TLS)"
# A TLS port (18093, 18091) answers a plain-HTTP request with a non-JSON 400 ("Client sent an HTTP request to an HTTPS
# server"); only said when this profile did not ask for TLS.
_BAD_REQUEST = "http_400"
_PLAIN_TO_TLS_PORT = ("This port may expect TLS: set SSL mode to require or verify-ca "
                      "(18093 is the query service over TLS)")


@dataclass(frozen=True)
class _Deadline:
    """How long an internal query may take (R-04a-F3): nobody can cancel it (`SessionWorker.close()` cannot interrupt a
    `call()`), so a stalled catalog query would hold the metadata worker until `call()` gives up and leaks it."""

    server: str  # the request's `timeout`: the server ends the statement and answers error 1080
    client_s: float  # seconds without a byte before the client gives up, if the server cannot answer at all


# A user's statement has none: the worker times tabs, and a paused result must not be timed by the server (spec, R-04-5).
_INTERNAL_DEADLINE = _Deadline(server="30s", client_s=40.0)
# Ruling C40: an internal write is never sent again (D-04-9), so it goes out only on a channel that answered at most
# this long ago. A channel idle for longer (the server may have closed it) or a new one first carries _PING, a read: a
# closed kept-alive channel is replaced and the read sent again (NodeClient.send), and the write follows on a fresh one.
WRITE_CHANNEL_IDLE_MAX_S = 2.0
_PING = "SELECT RAW 1"


def _transaction_statement(sql: str) -> bool:
    """D-04-13, per statement (R-04a-C7): the worker sends one, but a text that holds several must not hide one."""
    return any(is_transaction_statement(span.text, Dialect.SQLPP) for span in split(sql, Dialect.SQLPP))


def _shown_reason(reason: str) -> str:
    """An HTTP reason phrase as a message may show it: one short line (the peer chose it)."""
    return " ".join(reason.split())[:_REASON_LIMIT]


def _edition(version: str) -> Literal["enterprise", "community"] | None:
    if version.endswith("-enterprise"):
        return "enterprise"
    return "community" if version.endswith("-community") else None


def _read_all(result: CouchbaseResult) -> list[RawRow]:
    """An internal statement's rows to the end, then an error that came after them; the result is closed in any case."""
    try:
        rows: list[RawRow] = []
        while batch := result.fetch_rows(1000):
            rows.extend(batch)
        result.next_result()  # an error that came after the rows
        return rows
    finally:
        result.close()


class CouchbaseConnection(DatabaseConnection, FieldSampling, DocumentCapable):
    """One session's connection: one keep-alive channel to one query node (NodeClient), one request at a time. All
    methods except cancel(), force_cancel() and abort() run on the owning worker thread."""

    def __init__(self, client: NodeClient, *, settings: CouchbaseSettings, info: ServerInfo) -> None:
        self._client = client
        self._read_only = settings.read_only
        self._default_context = settings.default_context
        self._info = info
        self._window = CancelWindow("Couchbase")  # arm_cancel / disarm_cancel (Task 6b, R-04a-C16)
        # Guards the three fields below; never held across a blocking call. cancel() holds it over the channel's
        # shutdown() (one non-blocking syscall under the channel's own leaf lock), so the request it flagged cannot
        # end and a reused channel carry the next one in between (a late cancel never hits the next statement).
        self._lock = threading.Lock()
        self._busy = False  # from the start of a request until its response is read or abandoned
        self._channel: HttpChannel | None = None  # what a cancel shuts down
        self._cancelled = False  # a cancel reached the current request
        self._result: CouchbaseResult | None = None
        self._identified = False  # the node has answered once: from then on a failure is a lost connection
        self._last_status: int | None = None  # the HTTP status of the last answer (a 401 is a login failure)
        self.last_client_context_id: str | None = None

    # ---- requests ----

    def _begin(self, sql: str, *, context: QueryContext | None, params: StatementParams | None, readonly: bool,
               retry_safe: bool, deadline: _Deadline | None = None, profile: str | None = None,
               preserve_expiry: bool = False) -> EventStream:
        """Send one request and wait for its status line. Busy from here until the response is read or abandoned.
        `preserve_expiry`: a write keeps the documents' expiration (a document save, R-04-2)."""
        with self._lock:
            self._busy, self._cancelled, self._channel = True, False, None
        try:
            # After busy is set: a cancel from now on is either kept by the window (raised here) or flags the request
            # (attach refuses, or the channel is shut down), so none falls in between.
            self._window.take_over()
            request = QueryRequest(
                statement=sql, client_context_id=client_context_id(uuid4().hex),
                query_context=request_context(context) if context is not None else None, readonly=readonly,
                named=params.named if params else {}, positional=params.positional if params else (),
                timeout=deadline.server if deadline is not None else None, profile=profile,
                preserve_expiry=preserve_expiry,
            )
            self.last_client_context_id = request.client_context_id
            logger.debug("Couchbase request {} to {}{}{}", request.client_context_id, self._client.label,
                         " (read-only)" if readonly else "", f" (profile {profile})" if profile else "")
            body = build_body(request)
            response = self._client.send(body, retry_safe=retry_safe, attach=self._attach,
                                         read_timeout=deadline.client_s if deadline is not None else None)
        except ChannelError as exc:
            self._end()
            raise self._channel_failure(exc) from exc
        except UnicodeEncodeError:  # a lone surrogate in a context name or a parameter (the SQL is checked in run())
            self._end()
            raise StatementNotSent(_BAD_REQUEST_TEXT, code=INVALID_TEXT) from None
        except LiteralNotJson as exc:  # C39: it would add fields of its own to the body; named, never echoed
            self._end()
            raise StatementNotSent(f"{exc}: nothing was sent", code=INVALID_PARAMS) from None
        except BaseException:
            self._end()
            raise
        self._identified = True
        self._last_status = response.status
        logger.debug("Couchbase response {} {}", response.status, response.reason)
        if "json" not in response.content_type.lower():  # a proxy's page, not the query service
            response.abandon()
            self._end()
            status = response.status
            raise StatementError(f"HTTP {status} {_shown_reason(response.reason)}", code=f"http_{status}")
        return EventStream(response, cancelled=self.cancelled, node=self._client.label)

    def _attach(self, channel: HttpChannel) -> None:
        """NodeClient, worker thread, before anything is sent on `channel`."""
        with self._lock:
            if self._cancelled:
                raise StatementCancelled()  # a cancel came while the request was being prepared: nothing is sent
            self._channel = channel

    def _end(self) -> None:
        with self._lock:
            self._busy, self._channel = False, None

    def cancelled(self) -> bool:
        """A cancel reached the current request (any thread)."""
        with self._lock:
            return self._cancelled

    def _channel_failure(self, exc: ChannelError) -> StatementError:
        if self.cancelled():
            return StatementCancelled()
        if exc.tls_failure is not None:
            hint = _TLS_HINTS.get(exc.tls_failure)
            logger.info("TLS to the query node {} failed ({}): {}", self._client.label, exc.tls_failure.value, exc)
            return StatementError(f"TLS failed: {exc}" + (f"\n{hint}" if hint else ""), code=_TLS_FAILED)
        if not self._identified:  # opening: this node is down, try the next one
            return StatementError(str(exc), code=_NODE_DOWN)
        logger.warning("Connection to the query node {} was lost: {}", self._client.label, exc)
        return StatementError(f"Connection to the query node was lost: {exc}", code="connection_lost")

    def _close_result(self) -> None:
        result, self._result = self._result, None
        if result is not None:
            result.close()

    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        options = options or RunOptions()
        if _transaction_statement(sql):  # D-04-13: an engine capability, not a safety decision; nothing is sent
            raise StatementError(_NO_TRANSACTIONS, code=TRANSACTIONS_UNSUPPORTED)
        try:
            sql.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise invalid_text_error(sql, exc.start) from None
        self._close_result()
        warnings = parameter_warnings(options.params)  # spec §3.5.4: by name, never the value (B10)
        if warnings:
            logger.debug("Couchbase: {} parameter(s) the server reads as a double", len(warnings))
        stream = self._begin(sql, context=options.query_context, params=options.params, readonly=self._read_only,
                             retry_safe=options.retry_safe, profile=_PROFILE if options.profile_plan else None)
        first = first_keyword(sql, Dialect.SQLPP)
        self._result = start_result(stream, sql, plan=first in _PLANS, bare_star=has_bare_star(sql),
                                    dml=first in _DML, on_done=self._end, profile=options.profile_plan,
                                    messages=warnings)
        return self._result

    def _internal_result(self, statement: str, named: Mapping[str, str] | None, context: QueryContext | None, *,
                         write: bool = False, preserve_expiry: bool = False) -> CouchbaseResult:
        """Start an internal statement (catalog, identification, ADVISE, a field sample, a document's statements) and
        read its head, with a deadline (_INTERNAL_DEADLINE: the server's `timeout` of 30 s, whose error 1080 is raised
        like any other, and 40 s without a byte on the client). A read is read-only and retry-safe; a write (`write`, a
        document store's UPDATE, INSERT or DELETE) is read-only only on a read-only profile (the server refuses it then,
        too), never sent again (D-04-9), and goes out only on a channel that has just answered (ruling C40, a read
        first otherwise). The caller reads the rows and closes it. `named`: parameter names without `$` and their JSON
        literal texts (each must be one JSON value, C39). `context`: the request's query context (None: none is sent,
        as the catalog wants). `preserve_expiry`: see `_begin`."""
        self._close_result()
        if write:
            self._fresh_channel_for_write()
        params = StatementParams(named=dict(named)) if named else None
        stream = self._begin(statement, context=context, params=params,
                             readonly=self._read_only if write else True, retry_safe=not write,
                             deadline=_INTERNAL_DEADLINE, preserve_expiry=preserve_expiry)
        return start_result(stream, statement, plan=False, bare_star=False, dml=write, on_done=self._end)

    def _fresh_channel_for_write(self) -> None:
        """Ruling C40 (see WRITE_CHANNEL_IDLE_MAX_S); a failed read is StatementNotSent (the write never went out)."""
        if self._client.answered_within(WRITE_CHANNEL_IDLE_MAX_S):
            return
        logger.debug("Couchbase: a read before the write, on a channel that has not answered within {} s",
                     WRITE_CHANNEL_IDLE_MAX_S)
        try:
            self.query(_PING)
        except StatementError as exc:
            raise StatementNotSent(exc.message, code=exc.code) from exc

    def query(self, statement: str, named: Mapping[str, str] | None = None, *,
              context: QueryContext | None = None) -> list[Any]:
        """A read-only internal statement read to its end (see `_internal_result`), rows decoded with plain JSON (an int
        stays an int: the catalog's num_replica, R-04a-C20)."""
        return [json.loads(row.text) for row in _read_all(self._internal_result(statement, named, context))]

    def statement_rows(self, statement: str, named: Mapping[str, str] | None = None, *, write: bool = False,
                       preserve_expiry: bool = False) -> list[RawRow]:
        """An internal statement read to its end (see `_internal_result`; no query context: the statement names its
        keyspace in full), each row as parsed: its exact text and an object row's members, nested values located, never
        built. `named`: names without `$` → JSON literal texts."""
        return _read_all(self._internal_result(statement, named, None, write=write, preserve_expiry=preserve_expiry))

    def documents(self) -> DocumentStore:
        return SqlppDocumentStore(self)

    def sample_fields(self, keyspace: Keyspace, size: int) -> FieldSample:
        """A manual sample (R-04-11): up to `size` documents, their field paths derived in Python (sampling.py). The rows
        are read one at a time and the byte cap is checked after each (ruling B38: a batch is read whole before anything
        looks at it, and 200 documents of up to 20 MiB each would be 4 GB); the row that takes the texts past
        SAMPLE_MAX_BYTES is the last: the result is closed, which abandons the rest and stops the request on the server
        at once. A server's error answer is raised as StatementError. Nothing of a document is logged."""
        started = time.monotonic()
        collector = sampling.FieldCollector()
        read, cut = 0, False
        result = self._internal_result(sampling.sample_statement(keyspace), {"n": str(size)}, None)
        try:
            while rows := result.fetch_raw(1):
                try:  # the row is decoded once, here: its document goes to the collector as decoded
                    collector.add_decoded(*sampling.parse_sample_row(rows[0]))
                except (ValueError, RecursionError):  # the statement selects `t` and `d` of every row: anything else
                    raise StatementError(PROTOCOL_ERROR, code="protocol_error") from None  # is not the service
                read += len(rows[0])  # characters, as the `*` peek counts (ruling P12)
                if read > sampling.SAMPLE_MAX_BYTES:  # read now, not at import: a test lowers it
                    cut = collector.documents < size
                    break
        finally:
            result.close()
        fields = collector.fields()
        logger.debug("Couchbase sample: {} documents, {} characters, {} field paths{}", collector.documents, read,
                     len(fields), " (cut at the byte cap)" if cut else "")
        return FieldSample(
            keyspace=keyspace, sampled_at=datetime.now(UTC), requested=size, documents=collector.documents,
            cut_at_bytes=cut, paths_capped=collector.paths_capped, long_keys_skipped=collector.long_keys_skipped,
            depth_capped=collector.depth_capped, limits=sampling.limits(),
            elapsed_ms=int((time.monotonic() - started) * 1000), fields=fields,
        )

    def arm_cancel(self) -> None:
        self._window.arm()

    def disarm_cancel(self) -> None:
        self._window.disarm()

    def cancel(self) -> None:
        if self._window.keep():  # armed: nothing is running yet, run() will not start (Task 6b)
            logger.info("Couchbase cancel on {}: kept until the statement would start", self._client.label)
            return
        started = time.monotonic()
        with self._lock:
            if not self._busy:
                return  # nothing running: no flag either (P46); a cancel must never reach the next statement
            self._cancelled = True
            channel = self._channel
            if channel is not None:
                channel.shutdown()
        logger.info("Couchbase cancel on {}: {} in {:.0f} ms", self._client.label,
                    "connection shut down" if channel is not None else "before the request went out",
                    (time.monotonic() - started) * 1000)

    def force_cancel(self) -> None:
        self.cancel()  # a shutdown already ends the request at once

    def abort(self) -> None:
        self.cancel()

    # ---- identification ----

    def _first_row(self, statement: str) -> dict[str, Any]:
        rows = self.query(statement)
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def identify(self, username: str) -> ServerInfo:
        """What answered (spec §3.6.3 step 4): version, edition and the current user, read-only like every internal
        query. A login failure is raised with code `auth_failed`, whatever the server called it."""
        try:
            try:
                row = self._first_row(_IDENTIFY)
            except StatementError as exc:
                if not is_server_error(exc) or self._last_status == 401:
                    raise  # unreachable, or the login itself failed
                # A17: a limited user may not call CURRENT_USERS() (a privilege error such as 13014 is then a login
                # failure only if the version alone is refused too)
                row = {**self._first_row(_VERSION), "u": None}
        except StatementError as exc:
            if self._login_failed(exc):
                raise StatementError(exc.message, code=_AUTH_FAILED) from exc
            raise
        version = str(row.get("v") or "")
        users = row.get("u")
        user = users[0] if isinstance(users, list) and users and isinstance(users[0], str) else username
        context = self._default_context
        self._info = ServerInfo(
            engine=Engine.COUCHBASE, version=f"Couchbase Server {version}".rstrip(), edition=_edition(version),
            current_user=user, current_database=context.text() if context is not None else None,
            default_query_context=context,
        )
        return self._info

    def _login_failed(self, exc: StatementError) -> bool:
        return exc.code in _AUTH_FAILURES or (self._last_status == 401 and is_server_error(exc))

    # ---- no transactions (R-04-4, spec §3.7.5) ----

    def in_transaction(self) -> bool:
        return False

    def autocommit(self) -> bool:
        return True

    def set_autocommit(self, enabled: bool) -> None:
        if not enabled:
            raise StatementError(_NO_TRANSACTIONS, code=TRANSACTIONS_UNSUPPORTED)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    # ---- metadata ----

    def server_info(self) -> ServerInfo:
        return self._info

    def current_database(self) -> str | None:
        return self._info.current_database

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        return catalog.list_children(self.query, path)

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        return catalog.object_detail(self.query, path)

    def schema_snapshot(self) -> SchemaSnapshot:
        return catalog.build_snapshot(self.query)

    def close(self) -> None:
        try:
            self._close_result()
        finally:
            self._client.close()


_DOCUMENT_RULES = SqlppDocumentRules()  # pure: one for every profile


class CouchbaseAdapter(DatabaseAdapter, DocumentEngine):
    engine = Engine.COUCHBASE
    url_schemes = URL_SCHEMES
    default_port = QUERY_PORT
    dialect = Dialect.SQLPP
    # executed_plan: 04a's ruling P6 lifted in 04b, A16 holds (the `profile` field follows `metrics`, so it arrives
    # after the last row as a second result set); documents: lifted in 04c
    capabilities = AdapterCapabilities(
        server_read_only=True, transactions=False, query_context=True, parameters=True, documents=True,
        executed_plan=True, plan_format="couchbase", paused_result_max_idle_s=PAUSED_RESULT_MAX_IDLE_S,
        read_ahead_bytes=READ_AHEAD_BYTES, run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_couchbase_url(url)

    def document_rules(self) -> DocumentRules:
        return _DOCUMENT_RULES

    def relation_path(
        self, database: str | None, schema: str, name: str, kind: RelationKind
    ) -> list[PathSegment]:
        """A collection (⌘P) opens at bucket › scope › collection; `database` is its bucket, `schema` its scope."""
        if kind != "collection":
            return super().relation_path(database, schema, name, kind)
        if database is None:
            raise ValidationFailed("A collection path needs its bucket")
        path = child_path([], NodeKind.BUCKET, database)
        path = child_path(path, NodeKind.SCOPE, schema)
        return child_path(path, NodeKind.COLLECTION, name)

    def endpoint_target(self, profile: ServerProfile) -> tuple[str, int]:
        return endpoint_target(profile)

    def open(self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None,
             database: str | None, *, agent: bool = False) -> DatabaseConnection:
        """Spec §3.6.3: the candidate nodes in order (discovered or given), the first that answers. A refused, timed
        out or unknown node: the next one; a login or TLS failure: at once (another node would say the same). Runs on
        the worker's open path; discovery is bounded by its own deadline (R-04a-C19)."""
        if not isinstance(profile, ServerProfile) or endpoint is None:
            raise ValidationFailed("Not a Couchbase server profile")
        settings = connection_settings(profile, endpoint, database)
        auth = basic_auth(settings.username, password or "")
        nodes, how = candidate_nodes(settings, auth)
        logger.debug("Couchbase query nodes to try: {}", ", ".join(node.label for node in nodes))
        failures: list[str] = []
        for node in nodes:
            client = NodeClient(node.host, node.port, auth=auth, user_agent=f"DbaGuy/{app_version()}", tls=settings.tls,
                                server_hostname=settings.server_hostname)
            connection = CouchbaseConnection(client, settings=settings,
                                             info=ServerInfo(engine=Engine.COUCHBASE, version="Couchbase Server"))
            try:
                info = connection.identify(settings.username)
            except StatementError as exc:
                connection.close()
                if exc.code == _NODE_DOWN:
                    logger.info("Couchbase query node {} not reachable ({})", node.label, exc.message)
                    failures.append(f"{node.label} ({exc.message})")
                    continue
                if exc.code == _AUTH_FAILED:
                    raise ConnectionFailed(f"{exc.message}\nCheck the user name and password") from exc
                if exc.code == _NOT_FOUND:  # the answer to a query request on the cluster manager's port, say
                    raise ConnectionFailed(f"{exc.message}\n{_NOT_QUERY_SERVICE}") from exc
                if exc.code == _BAD_REQUEST and settings.tls is None:  # plain HTTP to a TLS port
                    raise ConnectionFailed(f"{exc.message}\n{_PLAIN_TO_TLS_PORT}") from exc
                raise ConnectionFailed(exc.message) from exc  # TLS failures carry their hint already
            except BaseException:
                connection.close()
                raise
            logger.info("Connected to {} at {} via {} (tls={}{})", info.version, node.label, how, settings.tls_mode,
                        ", read-only" if settings.read_only else "")
            return connection
        raise ConnectionFailed("Could not reach a query node: " + ", ".join(failures))
