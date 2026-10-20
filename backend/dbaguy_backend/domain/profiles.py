from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .dialect import Dialect


class Engine(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MSSQL = "mssql"
    COUCHBASE = "couchbase"
    SQLITE = "sqlite"


SslMode = Literal["disable", "prefer", "require", "verify-ca", "verify-full"]
ProfileColor = Literal["none", "green", "yellow", "orange", "red", "purple"]
SafetyLevel = Literal["relaxed", "standard", "strict"]
SshAuth = Literal["agent_or_key", "password"]


class SslConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SslMode = "prefer"
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None


class SshConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = ""  # may be an alias from ~/.ssh/config
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = None
    identity_file: str | None = None
    auth: SshAuth = "agent_or_key"


class _ProfileCommon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    folder: str = ""  # "/"-separated folder path
    color: ProfileColor = "none"
    read_only: bool = False
    safety: SafetyLevel = "standard"
    save_password: bool = True


class ServerProfileDraft(_ProfileCommon):
    engine: Literal["postgres", "mysql", "mssql", "couchbase"]
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    username: str | None = None
    ssl: SslConfig = Field(default_factory=SslConfig)
    ssh: SshConfig = Field(default_factory=SshConfig)
    options: dict[str, str] = Field(default_factory=dict)


class SqliteProfileDraft(_ProfileCommon):
    engine: Literal["sqlite"]
    path: str = Field(min_length=1)


class ServerProfile(ServerProfileDraft):
    id: UUID


class SqliteProfile(SqliteProfileDraft):
    id: UUID


AnyDraft = ServerProfileDraft | SqliteProfileDraft
AnyProfile = ServerProfile | SqliteProfile
ProfileDraft = Annotated[AnyDraft, Field(discriminator="engine")]
Profile = Annotated[AnyProfile, Field(discriminator="engine")]


def profile_from_draft(draft: AnyDraft, profile_id: UUID) -> AnyProfile:
    data = draft.model_dump()
    if isinstance(draft, SqliteProfileDraft):
        return SqliteProfile(id=profile_id, **data)
    return ServerProfile(id=profile_id, **data)


class ProfileSecretsInput(BaseModel):
    """`None` keeps the stored secret; an empty string clears it."""

    password: str | None = None
    ssh_password: str | None = None


class ProfileUpsertRequest(BaseModel):
    profile: ProfileDraft
    secrets: ProfileSecretsInput = Field(default_factory=ProfileSecretsInput)


class ProfileEntry(BaseModel):
    profile: Profile
    has_password: bool
    has_ssh_password: bool


class ConnectionTestRequest(BaseModel):
    profile: ProfileDraft
    secrets: ProfileSecretsInput = Field(default_factory=ProfileSecretsInput)
    existing_id: UUID | None = None  # reuse stored secrets of this profile when inputs are None


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    server_version: str | None = None
    elapsed_ms: int


class ImportUrlRequest(BaseModel):
    url: str


class ImportUrlResponse(BaseModel):
    profile: ProfileDraft
    password: str | None = None


class EngineInfo(BaseModel):
    engine: Engine
    default_port: int | None
    url_schemes: list[str]
    dialect: Dialect
    per_database_connection: bool
    # PostgreSQL: cancelling a statement aborts the transaction around it (the export-cancel warning)
    cancel_aborts_transaction: bool
    server_read_only: bool  # the database can enforce a read-only session; SQL Server cannot (03b)
    transactions: bool  # BEGIN / COMMIT / ROLLBACK and the auto-commit switch (Couchbase: none)
    query_context: bool  # requests carry a query context (Couchbase)
    parameters: bool  # statements take typed parameters
    documents: bool  # the engine stores JSON documents by key
    executed_plan: bool  # a statement can also return the plan it ran with (04b)
    plan_format: Literal["couchbase"] | None  # how EXPLAIN / profile results are laid out; None: no plan view
    row_editing: bool  # a table-data grid can be edited (spec ⑤)
    # A table-data tab's first browse waits for the edit target and sorts by its stable_order (PostgreSQL, 05b K13)
    orders_browse_by_key: bool
    run_file: bool = False  # Run SQL file (spec ⑥ D-06-13)
