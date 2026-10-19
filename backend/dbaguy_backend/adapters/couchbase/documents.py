"""Couchbase documents through SQL++ (spec §3.9.2, R-04-2): a document read with one `SELECT … USE KEYS` keeps the
stored text exactly (key order, whitespace, `1e400`) with its META; writes are CAS-checked UPDATE, INSERT and DELETE.

- Every UPDATE and DELETE carries `AND META(d).type = "json"` (A30): without it an UPDATE turns a binary document into
  JSON. A save keeps the expiration (`preserve_expiry`), which is read again after the write (A28: RETURNING says 0).
- A CAS-checked write that changed nothing reads the document's state to say why: missing, binary, or a conflict with
  the CAS it holds now (ruling C6). The server answers 12009 both to a concurrent change and to an INSERT of a key that
  exists (A29).
- A write is sent once (D-04-9): the server's own error answer is final (`QueryFailed`, B39); any other failure means
  the answer never came, so it may have been applied (`WriteOutcomeUnknown`). So does a failed read right after a
  write that was answered (ruling C25): the document was written, but its META cannot be reported.

Literals and identifiers only from `sql/dialects.py`; `$doc` is the caller's text, which must be one JSON value
(`json_problem` found nothing): it goes into the request body as it is, as parameter literals do. Logs nothing (the
service does), never a text."""

import math
import re
from collections.abc import Mapping
from typing import NamedTuple, Protocol

from ...domain.couchbase import DocumentSaved, DocumentView, Keyspace
from ...domain.errors import (
    BinaryDocument, DocumentConflict, DocumentExists, DocumentMissing, DomainError, QueryFailed, ValidationFailed,
    WriteOutcomeUnknown,
)
from ...sql.dialects import Dialect, keyspace_path, quote_literal
from ...sql.jsontext import number_paths, one_line, without_strings
from ..base import StatementError
from ..documents import DocumentStore
from .messages import PROTOCOL_ERROR, is_server_error
from .stream import RawRow, RowField

BLOCKED_PATHS_MAX = 50  # paths of blocked numbers named in a refusal
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_GET = ("SELECT META(d).cas AS cas, META(d).expiration AS expiration, META(d).type AS type, d "
        "FROM {ks} AS d USE KEYS $id")
_STATE = ("SELECT META(d).cas AS cas, META(d).expiration AS expiration, META(d).type AS type "
          "FROM {ks} AS d USE KEYS $id")
_SERVER_FORM = "SELECT RAW $doc"
# The type guard (A30): without it an UPDATE turns a binary document into JSON.
_REPLACE = ('UPDATE {ks} AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas AND META(d).type = "json" '
            "RETURNING META(d).cas AS cas")
_INSERT = "INSERT INTO {ks} AS d (KEY, VALUE) VALUES ($id, $doc) RETURNING META(d).cas AS cas"
_REMOVE = ('DELETE FROM {ks} AS d USE KEYS $id WHERE META(d).cas = $cas AND META(d).type = "json" '
           "RETURNING RAW META(d).id")
_CONCURRENT = "12009"  # a concurrent change, and also (INSERT) a duplicate key (A29: reason 17012)
_BINARY_SIZE = re.compile(r'"<binary \((\d+) b\)>"')

_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_INT64_DIGITS = 19
# Only these can hold a number the server changes in value (ruling C4): an integer beyond int64 has 19 digits or more,
# and a number beyond a double's range needs a three-digit positive exponent or a mantissa of 309 digits.
_SUSPECT = re.compile(r"[0-9]{19,}|[eE]\+?[0-9]{3,}")
_NOT_A_CAS = "Not a CAS value of this server"


class Statements(Protocol):
    """What the store sends its statements through: `CouchbaseConnection.statement_rows`."""

    def statement_rows(self, statement: str, named: Mapping[str, str] | None = None, *, write: bool = False,
                       preserve_expiry: bool = False) -> list[RawRow]: ...


class _State(NamedTuple):
    """A document's META now, without its text."""

    cas: str
    expiration: int
    type: str


class _Concurrent(Exception):
    """The server answered 12009 to a write: a concurrent change, or (INSERT) a duplicate key (A29)."""

    def __init__(self, error: StatementError) -> None:
        super().__init__(error.message)
        self.message = error.message


def changed_by_server(literal: str) -> bool:
    """A number literal the server would store with another value (ruling C4): an integer outside int64 (more than 19
    digits is never inside, and is not converted), or any other number beyond a double's range."""
    if _INTEGER.fullmatch(literal):
        digits = len(literal) - literal.startswith("-")
        return digits > _INT64_DIGITS or not INT64_MIN <= int(literal) <= INT64_MAX
    return math.isinf(float(literal))


def _utf8_size(text: str) -> int:
    """Bytes of `text` in UTF-8, without a copy for an ASCII text (a 20 MiB document)."""
    return len(text) if text.isascii() else len(text.encode("utf-8"))


def _protocol_error() -> StatementError:
    return StatementError(PROTOCOL_ERROR, code="protocol_error")


def _fields(row: RawRow, names: tuple[str, ...]) -> dict[str, RowField]:
    """The members of an object row by key; protocol_error when the row is not an object or lacks one of `names`."""
    found = {member.key: member for member in row.fields or ()}
    if any(name not in found for name in names):
        raise _protocol_error()
    return found


def _integer(field: RowField) -> int:
    try:
        return int(field.text)
    except ValueError:
        raise _protocol_error() from None


def _cas_literal(cas: int) -> str:
    """`$cas`: the decimal text of a CAS this server can hold (SQL++ compares one beyond int64 as a double)."""
    if type(cas) is not int or not 0 < cas <= INT64_MAX:
        raise ValidationFailed(_NOT_A_CAS)
    return str(cas)


def _key(doc_id: str) -> str:
    return quote_literal(Dialect.SQLPP, doc_id)


def _path(keyspace: Keyspace) -> str:
    return keyspace_path(keyspace.bucket, keyspace.scope, keyspace.collection)


class SqlppDocumentStore(DocumentStore):
    """DocumentStore over a Couchbase connection's internal statements (see the module docstring). Runs on the
    profile's metadata worker thread, as the connection does."""

    def __init__(self, statements: Statements) -> None:
        self._statements = statements

    # ---- reads ----

    def _one(self, statement: str, ks: str, doc_id: str) -> RawRow | None:
        rows = self._statements.statement_rows(statement.format(ks=ks), {"id": _key(doc_id)})
        return rows[0] if rows else None

    def get(self, keyspace: Keyspace, doc_id: str) -> DocumentView | None:
        row = self._one(_GET, _path(keyspace), doc_id)
        if row is None:
            return None
        fields = _fields(row, ("cas", "expiration", "type", "d"))
        cas, expiration, document = fields["cas"].text, _integer(fields["expiration"]), fields["d"].text
        if fields["type"].value == "json":
            return DocumentView(id=doc_id, cas=cas, expiration=expiration, type="json", text=document,
                                size=_utf8_size(document))
        size = _BINARY_SIZE.fullmatch(document)
        return DocumentView(id=doc_id, cas=cas, expiration=expiration, type="base64", text=None,
                            size=int(size.group(1)) if size else 0)

    def _state(self, ks: str, key: str) -> _State | None:
        """The document's META now (a read: resent after a stale connection like any read), or None without one."""
        row = self._one(_STATE, ks, key)
        if row is None:
            return None
        fields = _fields(row, ("cas", "expiration", "type"))
        return _State(fields["cas"].text, _integer(fields["expiration"]), fields["type"].value)

    def server_form(self, text: str) -> str:
        rows = self._statements.statement_rows(_SERVER_FORM, {"doc": text})
        if len(rows) != 1:
            raise _protocol_error()
        return rows[0].text

    def blocked_numbers(self, text: str) -> list[str]:
        if not _SUSPECT.search(without_strings(text)):
            return []  # almost every save
        return number_paths(text, changed_by_server, BLOCKED_PATHS_MAX)[0]

    # ---- writes ----

    def _write(self, statement: str, named: Mapping[str, str], *, preserve_expiry: bool = False) -> list[RawRow]:
        try:
            return self._statements.statement_rows(statement, named, write=True, preserve_expiry=preserve_expiry)
        except StatementError as exc:
            if exc.code == _CONCURRENT:
                raise _Concurrent(exc) from exc
            if is_server_error(exc):
                raise QueryFailed(exc.message) from exc  # the server's answer is final (B39)
            raise WriteOutcomeUnknown() from exc  # it may have been written: never sent again (D-04-9)

    def _refusal(self, ks: str, key: str) -> DomainError:
        """Why a CAS-checked UPDATE or DELETE changed nothing (spec §3.9.2), from the document's state now."""
        state = self._state(ks, key)
        if state is None:
            return DocumentMissing()
        if state.type != "json":
            return BinaryDocument()
        return DocumentConflict(state.cas)

    def _saved(self, ks: str, key: str, rows: list[RawRow], text: str) -> DocumentSaved:
        """A write the server answered: its CAS from RETURNING, the expiration read now (A28). Anything failing here
        (no row, a malformed one, the read) is an unknown outcome (ruling C25): the document was written, but what it
        holds now cannot be reported, and a failed save would make the next ⌘S meet a conflict."""
        try:
            if not rows:
                raise _protocol_error()
            cas = _fields(rows[0], ("cas",))["cas"].text
            state = self._state(ks, key)
        except StatementError as exc:
            raise WriteOutcomeUnknown() from exc
        return DocumentSaved(status="saved", cas=cas, expiration=state.expiration if state is not None else 0,
                             size=_utf8_size(one_line(text)))

    def replace(self, keyspace: Keyspace, doc_id: str, text: str, cas: int) -> DocumentSaved:
        ks = _path(keyspace)
        named = {"id": _key(doc_id), "doc": text, "cas": _cas_literal(cas)}
        try:
            rows = self._write(_REPLACE.format(ks=ks), named, preserve_expiry=True)
        except _Concurrent:
            rows = []
        if not rows:
            raise self._refusal(ks, doc_id)
        return self._saved(ks, doc_id, rows, text)

    def insert(self, keyspace: Keyspace, doc_id: str, text: str) -> DocumentSaved:
        ks = _path(keyspace)
        try:
            rows = self._write(_INSERT.format(ks=ks), {"id": _key(doc_id), "doc": text})
        except _Concurrent as exc:
            if self._state(ks, doc_id) is not None:
                raise DocumentExists() from exc
            raise QueryFailed(exc.message) from exc
        return self._saved(ks, doc_id, rows, text)

    def remove(self, keyspace: Keyspace, doc_id: str, cas: int) -> None:
        ks = _path(keyspace)
        named = {"id": _key(doc_id), "cas": _cas_literal(cas)}
        try:
            rows = self._write(_REMOVE.format(ks=ks), named)
        except _Concurrent:
            rows = []
        if not rows:
            raise self._refusal(ks, doc_id)
