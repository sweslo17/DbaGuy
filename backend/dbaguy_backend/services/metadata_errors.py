"""How a statement sent on a metadata connection reports the server's own error answer (B39)."""

from collections.abc import Iterator
from contextlib import contextmanager

from ..adapters.base import StatementError
from ..adapters.couchbase.adapter import is_server_error
from ..domain.errors import QueryFailed


@contextmanager
def server_errors_are_final() -> Iterator[None]:
    """Around a statement sent on a metadata connection: the server's own error answer (a numeric code) is raised as a
    DomainError (QueryFailed, with the server's text), so the metadata call does not reconnect and send the statement
    again. That resend is for a lost connection, and ADVISE on `app.users` panics the server (B39, A15)."""
    try:
        yield
    except StatementError as exc:
        if is_server_error(exc):
            raise QueryFailed(exc.message) from exc
        raise
