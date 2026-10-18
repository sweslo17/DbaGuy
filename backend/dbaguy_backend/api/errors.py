from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from ..domain.errors import DomainError


def error_body(code: str, message: str, detail: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return {"error": error}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        if exc.status >= 500:
            logger.error("{} {} failed: {} ({})", request.method, request.url.path, exc.message, exc.code)
        else:
            logger.info("{} {} rejected: {} ({})", request.method, request.url.path, exc.message, exc.code)
        return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message, exc.detail))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Where and why, never the input itself: it can be a secret, and a lone surrogate in it cannot be encoded
        errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
        return JSONResponse(
            status_code=422, content=error_body("validation_failed", "Invalid request", jsonable_encoder(errors)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The client only gets a generic message: driver exceptions can carry connection details.
        logger.opt(exception=exc).error("Unhandled error on {} {}", request.method, request.url.path)
        return JSONResponse(status_code=500, content=error_body("internal_error", "Internal error"))
