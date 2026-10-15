from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .api import metadata, profiles, sessions, system
from .api.errors import register_error_handlers
from .config import Settings
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

    app = FastAPI(title="DbaGuy Backend", version=app_version(), lifespan=lifespan)
    app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.include_router(system.router)
    app.include_router(profiles.router)
    app.include_router(metadata.router)
    app.include_router(sessions.router)
    return app


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --factory` in development."""
    settings = Settings()
    setup_logging(settings.log_level, settings.log_dir if settings.log_to_file else None)
    return create_app(settings)
