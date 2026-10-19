"""Documents by key (spec §3.9.3, R-04-2): read, save, insert and remove through the engine's DocumentStore, on the
profile's metadata connection (D-04-9: a document tab has no session of its own).

- A text to write is checked before any connection is used: text UTF-8 can carry, then one JSON value no deeper than
  MAX_DOCUMENT_DEPTH (`json_problem`; its offset is sent in UTF-16 code units, as the editor counts).
- On the connection: numbers the server would store with another value are refused, also when confirmed (ruling C4).
  Unless confirmed, the server's form of the text is compared with it token by token (`diff`): any difference comes
  back as a SavePreview and nothing is written; whitespace is no difference (spec §12), so such a save is written at
  once.
- A write runs with `retry=False`: whatever fails after it may have been sent, it is never sent again (D-04-9, C26).
  A read keeps the metadata call's one reconnect.
- Not through SafetyPolicy (spec §3.9.3 step 5): a document write is an explicit action with its own confirmation.

Logs: INFO a write's keyspace and a preview's change counts by kind; DEBUG the document's ID; never a text, a path or a
value (spec §5)."""

import re
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection
from ..adapters.documents import DocumentCapable, DocumentStore
from ..adapters.registry import AdapterRegistry
from ..domain.couchbase import (
    DOCUMENT_ID_MAX_BYTES, ChangeCounts, DocumentSaved, DocumentView, Keyspace, SavePreview,
)
from ..domain.errors import DocumentMissing, InvalidJson, NumberOutOfRange, ReadOnlyProfile, ValidationFailed
from ..sql.jsontext import diff, json_problem
from ..sql.offsets import utf16_offset
from .metadata_errors import server_errors_are_final
from .metadata_service import MetadataService
from .profile_service import ProfileService

Action = Literal["get", "save", "insert", "remove"]

_NO_DOCUMENTS = "This connection has no documents"
_ID_NOT_TEXT = "The document ID holds a character that is not valid text"
_ID_SIZE = f"A document ID is 1 to {DOCUMENT_ID_MAX_BYTES} bytes long (UTF-8)"
_TEXT_NOT_TEXT = "The document holds a character that is not valid text"
_TOO_DEEP_TO_CHECK = "Nested too deeply to check before saving"
# What UTF-8 cannot encode: in a Python str, only a surrogate code point. Searched instead of encoding a 20 MiB text.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _where(keyspace: Keyspace) -> str:
    return f"{keyspace.bucket}.{keyspace.scope}.{keyspace.collection}"


def _check_id(doc_id: str) -> None:
    try:
        size = len(doc_id.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValidationFailed(_ID_NOT_TEXT) from None
    if not 1 <= size <= DOCUMENT_ID_MAX_BYTES:
        raise ValidationFailed(_ID_SIZE)


def _check_text(text: str) -> None:
    """Spec §3.9.3 step 2, before any connection is used."""
    if not text.isascii() and _SURROGATE.search(text):
        raise ValidationFailed(_TEXT_NOT_TEXT)
    problem = json_problem(text)
    if problem is not None:
        raise InvalidJson(utf16_offset(text, problem.offset), problem.reason)


def _within_depth[T](check: Callable[[], T]) -> T:
    """`blocked_numbers` and `diff` recurse one Python frame per level (ruling C27). `json_problem` refuses more than
    MAX_DOCUMENT_DEPTH levels, far below the recursion limit; should that ever not hold, the text is refused like
    invalid JSON, instead of leaving the metadata call as a failure that drops the connection."""
    try:
        return check()
    except RecursionError:
        raise InvalidJson(0, _TOO_DEEP_TO_CHECK) from None


def _counted(counts: ChangeCounts) -> str:
    """`3 change(s) (key_order 2, number 1)`: the non-zero counts only, never a path or a value."""
    by_kind = counts.model_dump()
    kinds = ", ".join(f"{kind} {count}" for kind, count in by_kind.items() if count)
    return f"{sum(by_kind.values())} change(s) ({kinds})"


class DocumentService:
    def __init__(self, metadata: MetadataService, profiles: ProfileService, registry: AdapterRegistry) -> None:
        self._metadata = metadata
        self._profiles = profiles
        self._registry = registry

    def get(self, profile_id: UUID, keyspace: Keyspace, doc_id: str) -> DocumentView:
        """The document as stored (exact text and META). DocumentMissing when there is none."""
        self._admit(profile_id, keyspace, doc_id, "get")
        view = self._on_store(profile_id, lambda store: store.get(keyspace, doc_id), retry=True)
        if view is None:
            raise DocumentMissing()
        return view

    def save(
        self, profile_id: UUID, keyspace: Keyspace, doc_id: str, cas: str, text: str, confirmed: bool
    ) -> DocumentSaved | SavePreview:
        """Write `text` over the document if it still has `cas` (the store checks its range), keeping its expiration;
        a SavePreview instead while the server would store other tokens and the user has not confirmed them."""
        self._admit(profile_id, keyspace, doc_id, "save")
        version = int(cas)
        return self._write_text(profile_id, keyspace, "save", text, confirmed,
                                lambda store: store.replace(keyspace, doc_id, text, version))

    def insert(
        self, profile_id: UUID, keyspace: Keyspace, doc_id: str, text: str, confirmed: bool
    ) -> DocumentSaved | SavePreview:
        """Create the document (DocumentExists when the ID is taken), previewed like a save."""
        self._admit(profile_id, keyspace, doc_id, "insert")
        return self._write_text(profile_id, keyspace, "insert", text, confirmed,
                                lambda store: store.insert(keyspace, doc_id, text))

    def remove(self, profile_id: UUID, keyspace: Keyspace, doc_id: str, cas: str) -> None:
        """Delete the document if it still has `cas`."""
        self._admit(profile_id, keyspace, doc_id, "remove")
        version = int(cas)
        self._on_store(profile_id, lambda store: store.remove(keyspace, doc_id, version), retry=False)
        logger.info("Deleted a document in {}", _where(keyspace))

    def _admit(self, profile_id: UUID, keyspace: Keyspace, doc_id: str, action: Action) -> None:
        """The checks every call makes first: an engine with documents, a writable profile for a write, an ID the
        server can hold."""
        profile = self._profiles.get(profile_id)
        if not self._registry.get(profile.engine).capabilities.documents:
            raise ValidationFailed(_NO_DOCUMENTS)
        if action != "get" and profile.read_only:
            raise ReadOnlyProfile()
        _check_id(doc_id)
        logger.debug("Document {} in {}: {}", doc_id, _where(keyspace), action)

    def _write_text(
        self, profile_id: UUID, keyspace: Keyspace, action: Literal["save", "insert"], text: str, confirmed: bool,
        write: Callable[[DocumentStore], DocumentSaved],
    ) -> DocumentSaved | SavePreview:
        """Spec §3.9.3 steps 2–4: the local checks, then on one metadata call the blocked numbers, the preview
        (unless confirmed) and the write, which is never sent twice."""
        _check_text(text)

        def run(store: DocumentStore) -> DocumentSaved | SavePreview:
            blocked = _within_depth(lambda: store.blocked_numbers(text))
            if blocked:
                raise NumberOutOfRange(blocked)
            if not confirmed:
                server = store.server_form(text)
                found = _within_depth(lambda: diff(text, server))
                if found.changes:
                    return SavePreview(status="needs_confirmation", server_text=server, changes=found.changes,
                                       counts=found.counts)
            return write(store)

        answer = self._on_store(profile_id, run, retry=False)
        if isinstance(answer, SavePreview):
            logger.info("A save in {} needs confirmation: {}", _where(keyspace), _counted(answer.counts))
        else:
            logger.info("{} a document in {}", "Saved" if action == "save" else "Inserted", _where(keyspace))
        return answer

    def _on_store[T](self, profile_id: UUID, fn: Callable[[DocumentStore], T], *, retry: bool) -> T:
        """`fn` on the profile's document store, on its metadata connection. The server's own error answer is final
        (B39): never sent again on a fresh connection, a read included."""

        def run(connection: DatabaseConnection) -> T:
            if not isinstance(connection, DocumentCapable):
                raise ValidationFailed(_NO_DOCUMENTS)
            with server_errors_are_final():
                return fn(connection.documents())

        return self._metadata.with_connection(profile_id, None, run, retry=retry)
