from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from loguru import logger

from .api import history, metadata, profiles, scripts, sessions, sql, system, workspace
from .api import settings as settings_api  # `settings` is the parameter of create_app
from .api.errors import register_error_handlers
from .config import Settings, load_settings
from .container import AppContainer
from .logging_setup import setup_logging
from .version import app_version


def create_app(settings: Settings, container: AppContainer | None = None) -> FastAPI:
    container = container or AppContainer(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        container.start()
        logger.info("Backend started (version {})", app_version())
        try:
            yield
        finally:
            container.close()
            logger.info("Backend stopped")

    app = FastAPI(
        title="DbaGuy Backend",
        version=app_version(),
        lifespan=lifespan,
        separate_input_output_schemas=False,
    )
    app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost: only requests addressed to the loopback name we listen on (port ignored), so a web page using DNS
    # rebinding cannot reach the backend under its own host name.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    register_error_handlers(app)
    app.include_router(system.router)
    app.include_router(profiles.router)
    app.include_router(metadata.router)
    app.include_router(sessions.router)
    app.include_router(settings_api.router)
    app.include_router(workspace.router)
    app.include_router(scripts.router)
    app.include_router(history.router)
    app.include_router(sql.router)
    return app


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --factory` in development."""
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_dir if settings.log_to_file else None)
    return create_app(settings)
