import sys
from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "DbaGuy"


def default_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "DbaGuy"


class Settings(BaseSettings):
    """Runtime settings, read from DBAGUY_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="DBAGUY_")

    token: str = Field(min_length=1)  # no default: every HTTP/WS request must carry this secret
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
    # The MCP server's bearer token, kept in memory only (a scratch or frozen sidecar a probe runs): set, the Keychain
    # is never read or written for it (spec ⑧ R-08-3 keeps it there otherwise)
    mcp_token: str | None = Field(default=None, min_length=1)


def load_settings() -> Settings:
    """Settings for a real server, from the environment. Exits with a clear message (status 2) when they are
    unusable, e.g. DBAGUY_TOKEN is not set: the backend never runs without an API token."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False):  # never echo values: one of them is the token
            name = "DBAGUY_" + "_".join(str(part) for part in error["loc"]).upper()
            problems.append(f"{name} is not set" if error["type"] == "missing" else f"{name}: {error['msg']}")
        sys.stderr.write(f"dbaguy-backend: cannot start: {'; '.join(problems)}\n")
        raise SystemExit(2) from None
