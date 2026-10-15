import asyncio
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..container import AppContainer
from ..domain.errors import DomainError, NotFound, SessionNotFound
from ..domain.execution import CancelCommand, ClientCommand, ErrorEvent, ExecutionSchema
from ..sessions.manager import SessionCreateRequest, SessionInfo
from .deps import get_container, require_token, websocket_token_ok

router = APIRouter(prefix="/sessions", tags=["sessions"])
_COMMANDS: TypeAdapter[Any] = TypeAdapter(ClientCommand)


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
    if not websocket_token_ok(websocket):
        await websocket.close(code=4401)
        return
    try:
        container.sessions.worker(session_id)
    except SessionNotFound:
        await websocket.close(code=4404)
        return
    await websocket.accept()
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
                error = ErrorEvent(request_id=request_id, message=f"Invalid command: {exc.errors()[0]['msg']}", code="invalid_command")
                await outbox.put(error.model_dump(mode="json"))
                continue
            try:
                # submit() and cancel() can wait on the worker's state lock (a cancel holds it across a PG cancel),
                # so neither may run on the event loop.
                if isinstance(command, CancelCommand):
                    await asyncio.to_thread(worker.cancel, command.request_id)
                else:
                    await asyncio.to_thread(worker.submit, command)
            except DomainError as exc:
                await outbox.put(ErrorEvent(request_id=command.request_id, message=exc.message, code=exc.code).model_dump(mode="json"))
    except WebSocketDisconnect:
        pass
    finally:
        container.sessions.detach(session_id, emit)
        await outbox.put(None)
        with suppress(Exception):
            await send_task
        logger.info("WebSocket detached from session {}", session_id)
