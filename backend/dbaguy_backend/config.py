from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "DbaGuy"


def default_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "DbaGuy"


class Settings(BaseSettings):
    """Runtime settings, read from DBAGUY_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="DBAGUY_")

    token: str = "dev-token"
    host: str = "127.0.0.1"
    port: int = 0
    data_dir: Path = Field(default_factory=default_data_dir)
    log_dir: Path = Field(default_factory=default_log_dir)
    log_to_file: bool = True
    log_level: str = "INFO"
    allowed_origins: list[str] = [
        "tauri://localhost",
        "http://tauri.localhost",
        "http://localhost:1420",
    ]
    watch_stdin: bool = False
