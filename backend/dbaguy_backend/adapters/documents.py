"""The engine-neutral contract of a document store (spec §3.9.1, ruling C8): the save flow (DocumentService) depends on
this, never on an engine's package. Couchbase implements it over SQL++ (`adapters/couchbase/documents.py`); an SDK store
would implement the same."""

from abc import ABC, abstractmethod

from ..domain.couchbase import DocumentSaved, DocumentView, Keyspace


class DocumentStore(ABC):
    """JSON documents by key (spec §3.9.1, R-04-2). Runs on the profile's metadata worker thread. Raises the document
    errors of domain/errors.py, QueryFailed for the server's own error answer (final, B39), WriteOutcomeUnknown for a
    write whose answer never came; a read's lost connection is a StatementError (MetadataService reconnects once)."""

    @abstractmethod
    def get(self, keyspace: Keyspace, doc_id: str) -> DocumentView | None:
        """The document as stored (its exact text and META), or None when there is none."""

    @abstractmethod
    def server_form(self, text: str) -> str:
        """What this store would store for `text` (an SDK store: `text`)."""

    @abstractmethod
    def blocked_numbers(self, text: str) -> list[str]:
        """Paths of the numbers in `text` (valid JSON) this store would change in value: a save is refused."""

    @abstractmethod
    def replace(self, keyspace: Keyspace, doc_id: str, text: str, cas: int) -> DocumentSaved:
        """Write `text` over the document if it still has `cas`, keeping its expiration. DocumentMissing,
        BinaryDocument or DocumentConflict (with the current CAS) when nothing was written."""

    @abstractmethod
    def insert(self, keyspace: Keyspace, doc_id: str, text: str) -> DocumentSaved:
        """Create the document. DocumentExists when the ID is taken."""

    @abstractmethod
    def remove(self, keyspace: Keyspace, doc_id: str, cas: int) -> None:
        """Delete the document if it still has `cas`. DocumentMissing, BinaryDocument or DocumentConflict when nothing
        was deleted."""


class DocumentCapable(ABC):
    """A connection whose engine stores JSON documents by key (capability `documents`)."""

    @abstractmethod
    def documents(self) -> DocumentStore: ...
