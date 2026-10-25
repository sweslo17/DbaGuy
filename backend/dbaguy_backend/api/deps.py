from fastapi import Header, Request, WebSocket

from ..container import AppContainer
from ..domain.errors import Unauthorized
from ..tokens import same_token


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {request.app.state.container.settings.token}"
    if authorization is None or not same_token(authorization, expected):
        raise Unauthorized("Missing or invalid token")


def websocket_token_ok(websocket: WebSocket) -> bool:
    expected = websocket.app.state.container.settings.token
    return same_token(websocket.query_params.get("token", ""), expected)
