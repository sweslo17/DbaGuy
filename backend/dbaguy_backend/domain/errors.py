from typing import Any


class DomainError(Exception):
    """Base error carrying an API error code and HTTP status."""

    code = "internal_error"
    status = 500

    def __init__(self, message: str, detail: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Unauthorized(DomainError):
    code = "unauthorized"
    status = 401


class NotFound(DomainError):
    code = "not_found"
    status = 404


class ProfileNotFound(NotFound):
    code = "profile_not_found"


class SessionNotFound(NotFound):
    code = "session_not_found"


class ValidationFailed(DomainError):
    code = "validation_failed"
    status = 422


class UnsupportedEngine(ValidationFailed):
    code = "unsupported_engine"


class UnsupportedFormat(ValidationFailed):
    """The export or copy format cannot be produced for this engine's dialect."""

    code = "unsupported_format"


class NotSupported(ValidationFailed):
    """The engine has no such data tool (spec ⑥ D-06-13): `<Engine> connections cannot …`."""

    code = "not_supported"


class FileNotFound(NotFound):
    """A path that names no regular file (spec ⑥ §4): `<path> is not a file`."""

    code = "file_not_found"

    def __init__(self, path: str) -> None:
        super().__init__(f"{path} is not a file")


class NotEditable(ValidationFailed):
    """Grid editing refused for this table or engine (spec ⑤ §3.4): the message is the target's reason."""

    code = "not_editable"


class ConnectionFailed(DomainError):
    code = "connection_failed"
    status = 502


class TunnelFailed(ConnectionFailed):
    code = "tunnel_failed"


class SessionBusy(DomainError):
    code = "session_busy"
    status = 409


class SecretStoreError(DomainError):
    code = "secret_store_error"
    status = 500


class ProfileStoreError(DomainError):
    """profiles.json cannot be read, moved aside or written."""

    code = "profile_store_error"
    status = 500


class StorageError(DomainError):
    """A local data file (settings, workspace, scripts) cannot be read, moved aside or written."""

    code = "storage_error"
    status = 500


class AlreadyExists(DomainError):
    code = "already_exists"
    status = 409


class PreviewSuperseded(DomainError):
    """A copy's preview stopped between two tables: the same dialog asked for a newer one (its `preview_key`), and nobody
    waits for this one any more (06c final review)."""

    code = "preview_superseded"
    status = 409

    def __init__(self) -> None:
        super().__init__("A newer preview was asked for")


class QueryFailed(DomainError):
    code = "query_failed"
    status = 400


class PasswordRequired(ConnectionFailed):
    """The profile does not store its password and none was unlocked this run."""

    code = "password_required"
    status = 428


class DocumentMissing(NotFound):
    code = "document_missing"

    def __init__(self) -> None:
        super().__init__("The document does not exist (it may have been deleted)")


class DocumentConflict(DomainError):
    """A CAS-checked write met a newer version of the document; `detail.cas` is the CAS it holds now."""

    code = "document_conflict"
    status = 409

    def __init__(self, current_cas: str) -> None:
        super().__init__("The document was changed after it was opened", detail={"cas": current_cas})


class DocumentExists(DomainError):
    code = "document_exists"
    status = 409

    def __init__(self) -> None:
        super().__init__("A document with this ID already exists")


class BinaryDocument(ValidationFailed):
    code = "binary_document"

    def __init__(self) -> None:
        super().__init__("This is a binary document: DbaGuy shows it but never changes it")


class WriteOutcomeUnknown(DomainError):
    """A write whose answer never came (D-04-9): it may have been applied, so it is never sent again. The message says
    why: by default the store's (the connection was lost), or the metadata call's (a timeout, ruling C42)."""

    code = "write_outcome_unknown"
    status = 502
    LOST = ("The connection to the query node was lost during the write: the document may or may not have changed. "
            "Reload it to see what the server holds")

    def __init__(self, message: str = LOST) -> None:
        super().__init__(message)


class InvalidJson(ValidationFailed):
    """A document text that is not one JSON value (or nests too deeply to check): `detail.offset` is where, in UTF-16
    code units as the editor counts; `detail.reason` a fixed text, never a quote of the document."""

    code = "invalid_json"

    def __init__(self, offset: int, reason: str) -> None:
        super().__init__(f"Not valid JSON: {reason}", detail={"offset": offset, "reason": reason})


class NumberOutOfRange(ValidationFailed):
    """Numbers the server would store with another value (ruling C4). Their paths travel in `detail`, never in the
    message, so they never reach the INFO log."""

    code = "number_out_of_range"

    def __init__(self, paths: list[str]) -> None:
        super().__init__(
            "Couchbase would change these numbers (integers beyond 64 bits or numbers beyond a double's range): write "
            "them as strings to save",
            detail={"paths": paths},
        )


READ_ONLY_MESSAGE = "This connection is read-only"


class ReadOnlyProfile(DomainError):
    code = "read_only"
    status = 403

    def __init__(self) -> None:
        super().__init__(READ_ONLY_MESSAGE)
