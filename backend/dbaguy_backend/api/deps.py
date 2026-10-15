import secrets

from fastapi import Header, Request, WebSocket

from ..container import AppContainer
from ..domain.errors import Unauthorized


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def _same_token(provided: str, expected: str) -> bool:
    # compare_digest raises TypeError on non-ASCII str operands; bytes never raise for any input.
    return secrets.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {request.app.state.container.settings.token}"
    if authorization is None or not _same_token(authorization, expected):
        raise Unauthorized("Missing or invalid token")


def websocket_token_ok(websocket: WebSocket) -> bool:
    expected = websocket.app.state.container.settings.token
    return _same_token(websocket.query_params.get("token", ""), expected)
