from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EditorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_size: int = Field(default=13, ge=8, le=32)
    vim: bool = False
    word_wrap: bool = False
    tab_size: int = Field(default=2, ge=1, le=8)
    keyword_case: Literal["upper", "preserve"] = "upper"


class RunSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: Literal["statement", "all"] = "statement"
    continue_on_error: bool = False
    page_size: int = Field(default=500, ge=1, le=10_000)
    statement_timeout_s: int = Field(default=0, ge=0, le=86_400)  # 0 = none


class SafetySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idle_transaction_warn_minutes: int = Field(default=3, ge=0, le=1440)
    idle_transaction_rollback_minutes: int = Field(default=0, ge=0, le=1440)  # 0 = off


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    long_query_seconds: int = Field(default=15, ge=0)


class AppSettings(BaseModel):
    """The app preferences (`settings.json`). Persisted, so a newer build's extra fields are tolerated on read and
    kept on write; input from the API stays strict."""

    model_config = ConfigDict(extra="forbid")

    editor: EditorSettings = EditorSettings()
    run: RunSettings = RunSettings()
    safety: SafetySettings = SafetySettings()
    notifications: NotificationSettings = NotificationSettings()
    scripts_dir: str | None = None  # None = the default folder inside the data dir
