import asyncio
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..container import AppContainer
from ..domain.errors import DomainError, NotFound, SessionNotFound
from ..domain.execution import CancelCommand, ClientCommand, ErrorEvent, ExecutionSchema, FinishedEvent
from ..sessions.manager import SessionCreateRequest, SessionInfo
from .deps import get_container, require_token, websocket_token_ok

router = APIRouter(prefix="/sessions", tags=["sessions"])
_COMMANDS: TypeAdapter[Any] = TypeAdapter(ClientCommand)
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_SESSION_NOT_FOUND = 4404


async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
    logger.info("WebSocket {} rejected: {} ({})", websocket.url.path, reason, code)
    await websocket.close(code=code, reason=reason)


async def _fail_request(outbox: asyncio.Queue[dict[str, Any] | None], request_id: str, message: str, code: str) -> None:
    """Answer a request the worker never ran: an error plus the `finished` every request must end with.

    `in_transaction` is false because the connection belongs to the worker thread and may already be gone.
    """
    await outbox.put(ErrorEvent(request_id=request_id, message=message, code=code).model_dump(mode="json"))
    if request_id:
        finished = FinishedEvent(request_id=request_id, elapsed_ms=0, in_transaction=False, cancelled=False)
        await outbox.put(finished.model_dump(mode="json"))


@router.post("", response_model=SessionInfo, status_code=201, dependencies=[Depends(require_token)])
def create_session(body: SessionCreateRequest, container: AppContainer = Depends(get_container)) -> SessionInfo:
    return container.sessions.create(body)


@router.delete("/{session_id}", status_code=204, dependencies=[Depends(require_token)])
def close_session(session_id: str, container: AppContainer = Depends(get_container)) -> Response:
    container.sessions.close(session_id)
    return Response(status_code=204)


@router.get("/_schema/execution", response_model=ExecutionSchema, dependencies=[Depends(require_token)])
def execution_schema() -> ExecutionSchema:
    """Never called; exposes the WebSocket protocol models in OpenAPI."""
    raise NotFound("Schema-only endpoint")


@router.websocket("/{session_id}/ws")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    container: AppContainer = websocket.app.state.container
    # Accept before any rejection: closing an unaccepted socket is an HTTP 403 handshake failure, which browsers
    # report as close code 1006, so the client could never tell a bad token from an unknown session.
    await websocket.accept()
    if not websocket_token_ok(websocket):
        await _reject(websocket, _CLOSE_UNAUTHORIZED, "Invalid token")
        return
    try:
        container.sessions.worker(session_id)
    except SessionNotFound:
        await _reject(websocket, _CLOSE_SESSION_NOT_FOUND, "Session not found")
        return
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def emit(event: BaseModel) -> None:  # called from the worker thread
        loop.call_soon_threadsafe(outbox.put_nowait, event.model_dump(mode="json"))

    async def sender() -> None:
        while (item := await outbox.get()) is not None:
            await websocket.send_json(item)

    worker = container.sessions.attach(session_id, emit)
    send_task = asyncio.create_task(sender())
    logger.info("WebSocket attached to session {}", session_id)
    try:
        while True:
            raw = await websocket.receive_json()
            try:
                command = _COMMANDS.validate_python(raw)
            except ValidationError as exc:
                request_id = str(raw.get("request_id", "")) if isinstance(raw, dict) else ""
                await _fail_request(outbox, request_id, f"Invalid command: {exc.errors()[0]['msg']}", "invalid_command")
                continue
            try:
                # submit() and cancel() can wait on the worker's state lock (a cancel holds it across a PG cancel),
                # so neither may run on the event loop.
                if isinstance(command, CancelCommand):
                    await asyncio.to_thread(worker.cancel, command.request_id)
                else:
                    await asyncio.to_thread(worker.submit, command)
            except DomainError as exc:
                await _fail_request(outbox, command.request_id, exc.message, exc.code)
    except WebSocketDisconnect:
        pass
    finally:
        container.sessions.detach(session_id, emit)
        await outbox.put(None)
        with suppress(Exception):
            await send_task
        logger.info("WebSocket detached from session {}", session_id)
