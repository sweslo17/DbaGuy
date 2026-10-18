"""How a SQL Server profile becomes pymssql._mssql.connect arguments, and a SQL Server URL a profile draft. Pure: no I/O."""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit

from loguru import logger

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.profiles import ServerProfile, ServerProfileDraft, SslConfig, SslMode
from ..base import MALFORMED_URL_MESSAGE, URL_ENCODING_HINT, Endpoint, ParsedUrl
from .freetds import TdsSection

URL_SCHEMES = ("mssql", "sqlserver", "mssql+pymssql")
CONNECT_TIMEOUT_S = 10
TDS_VERSION = "7.4"  # SQL Server 2012+; spec §3.6
_TDS_VERSIONS = frozenset({"7.0", "7.1", "7.2", "7.3", "7.4"})
# SSMS's session options: pymssql's own list without CURSOR_CLOSE_ON_COMMIT. QUOTED_IDENTIFIER ON is also what the
# T-SQL lexer assumes (`"x"` names an identifier). IMPLICIT_TRANSACTIONS stays OFF: manual commit turns it on (Task 8).
SESSION_DEFAULTS = (
    "SET ARITHABORT ON; SET CONCAT_NULL_YIELDS_NULL ON; SET ANSI_NULLS ON; SET ANSI_NULL_DFLT_ON ON; "
    "SET ANSI_PADDING ON; SET ANSI_WARNINGS ON; SET QUOTED_IDENTIFIER ON; SET TEXTSIZE 2147483647;"
)
_ENCRYPTION: dict[SslMode, str] = {
    "disable": "off", "prefer": "request", "require": "require", "verify-ca": "require", "verify-full": "require",
}
_VERIFYING_MODES = frozenset({"verify-ca", "verify-full"})
_NAMED_INSTANCE = (
    "Named instances (host\\instance) are not supported: give the instance's TCP port instead "
    "(SQL Server Configuration Manager → TCP/IP → IP Addresses)"
)
_URL_HINT = "e.g. mssql://user@host:1433/db or jdbc:sqlserver://host:1433;databaseName=db"
_INVALID_PORT = "Invalid port in the SQL Server URL"
_NO_WINDOWS_LOGIN = "Windows and Azure AD logins are not supported: use a SQL login"


# ---- profile -> pymssql ----


@dataclass(frozen=True)
class ConnectParams:
    kwargs: dict[str, Any]  # for pymssql._mssql.connect
    tds_section: TdsSection | None  # set when the login must verify the certificate: see freetds.py


def _tds_version(options: Mapping[str, str]) -> str:
    """The `tds_version` option; options are an allow-list of that one key (key case-insensitive)."""
    version = TDS_VERSION
    ignored = []
    for key, value in options.items():
        if key.lower() == "tds_version":
            version = value.strip()
        else:
            ignored.append(key)
    if ignored:  # names only: the values may hold secrets
        logger.warning(
            "Ignoring SQL Server connection options (only tds_version is taken): {}", ", ".join(sorted(ignored))
        )
    if version not in _TDS_VERSIONS:
        raise ConnectionFailed(f"tds_version must be one of 7.0-7.4, not {version!r}")
    return version


def _tds_section(ssl: SslConfig, endpoint: Endpoint) -> TdsSection | None:
    """The freetds.conf section a certificate-verifying login needs: pymssql has no parameter for a CA file or a host
    name check. Raises ConnectionFailed for settings that cannot work."""
    if ssl.mode not in _VERIFYING_MODES:
        return None
    if ssl.mode == "verify-full" and endpoint.tls_server_name:
        raise ConnectionFailed(
            "verify-full checks the server's host name, which FreeTDS cannot do through an SSH tunnel "
            "(it would check 127.0.0.1); use verify-ca, or connect without the tunnel"
        )
    if not ssl.ca_file:
        raise ConnectionFailed(
            f"SSL mode {ssl.mode} needs a CA file for SQL Server: FreeTDS does not read the macOS keychain"
        )
    return TdsSection(endpoint.host, endpoint.port, os.path.expanduser(ssl.ca_file), ssl.mode == "verify-full")


def connect_params(
    profile: ServerProfile, endpoint: Endpoint, password: str | None, database: str | None
) -> ConnectParams:
    """Arguments for `pymssql._mssql.connect`. Everything that picks the target or sets DbaGuy's guarantees comes from
    the profile and this module, never from free-form options. Raises ConnectionFailed for settings that cannot work."""
    if "\\" in profile.host or "\\" in endpoint.host:  # through a tunnel the endpoint is local: check the profile too
        raise ConnectionFailed(_NAMED_INSTANCE)
    tds_version = _tds_version(profile.options)
    section = _tds_section(profile.ssl, endpoint)
    if profile.ssl.cert_file or profile.ssl.key_file:
        logger.warning("SQL Server does not use client certificates; ignoring them for profile {}", profile.name)
    kwargs = {
        "server": endpoint.host,
        "port": str(endpoint.port),
        "user": profile.username or "",
        "password": password or "",
        "database": database or profile.database or "",
        "appname": "DbaGuy",
        "charset": "UTF-8",
        "tds_version": tds_version,
        "encryption": _ENCRYPTION[profile.ssl.mode],
        "conn_properties": SESSION_DEFAULTS,
    }
    return ConnectParams(kwargs, section)


# ---- URL -> profile draft ----

# Keys are folded (see `_fold`) and mapped to one name each.
_SHARED_KEYS = {
    "user": "user", "username": "user", "password": "password", "database": "database", "databasename": "database",
    "encrypt": "encrypt", "encryption": "encrypt", "trustservercertificate": "trust", "tdsversion": "tds_version",
    "integratedsecurity": "integrated", "authentication": "authentication",
}
_JDBC_KEYS = {"servername": "server", "portnumber": "port"}  # JDBC's spellings of the address
_FLAG_ON = frozenset({"true", "yes"})
_ENCRYPT_MODES: dict[str, SslMode] = {  # the verifying choice depends on trustServerCertificate: see `_ssl_mode`
    "disable": "disable", "false": "prefer", "no": "prefer", "optional": "prefer",
}
_ENCRYPT_ON = frozenset({"true", "yes", "mandatory"})


def _fold(key: str) -> str:
    """`TrustServerCertificate`, `trust_server_certificate` and `trust-server-certificate` are one key."""
    return key.lower().replace("-", "").replace("_", "")


def _split_authority(text: str) -> tuple[SplitResult, int | None]:
    """The parts of `scheme://[user[:password]@]host[:port][/path][?query]`. Every message here is fixed text: urllib's
    own quotes the authority, password included."""
    try:
        split = urlsplit(text)
    except ValueError:
        raise ValidationFailed(MALFORMED_URL_MESSAGE) from None
    try:
        port = split.port
    except ValueError:
        raise ValidationFailed(f"{_INVALID_PORT}. {URL_ENCODING_HINT}") from None
    if port == 0:
        raise ValidationFailed(_INVALID_PORT)
    return split, port


def _port_number(text: str) -> int:
    if not (text.isascii() and text.isdigit() and 1 <= int(text) <= 65535):
        raise ValidationFailed(_INVALID_PORT)
    return int(text)


def _query_pairs(query: str) -> Iterable[tuple[str, str]]:
    """`k=v&k=v`, percent-decoded. A "+" stays a plus: a password is not form-encoded."""
    for part in query.split("&"):
        if part:
            key, _, value = part.partition("=")
            yield unquote(key), unquote(value).strip()


def _jdbc_pairs(properties: Iterable[str]) -> Iterable[tuple[str, str]]:
    """`key=value` after the host, kept as written; empty parts (a trailing `;`) are skipped."""
    for part in properties:
        if part.strip():
            key, equals, value = part.partition("=")
            if not equals or not key.strip():
                raise ValidationFailed("A JDBC SQL Server URL takes key=value properties after the host")
            yield key.strip(), value.strip()


def _ssl_mode(values: Mapping[str, str]) -> SslMode | None:
    value = values.get("encrypt")
    if value is None:
        return None
    key = value.strip().lower()
    if key in _ENCRYPT_MODES:
        return _ENCRYPT_MODES[key]
    if key in _ENCRYPT_ON:  # encrypt=true checks the certificate unless trustServerCertificate waives it
        return "require" if values.get("trust", "").strip().lower() in _FLAG_ON else "verify-full"
    if key == "strict":
        raise ValidationFailed("encrypt=strict (TDS 8.0) is not supported; use encrypt=true")
    raise ValidationFailed(f"Unknown encrypt value '{value}' in the SQL Server URL")


def _refuse_windows_logins(values: Mapping[str, str]) -> None:
    if values.get("integrated", "").strip().lower() in _FLAG_ON:
        raise ValidationFailed(_NO_WINDOWS_LOGIN)
    asked = values.get("authentication", "").strip().lower()  # blank asks for nothing
    if asked and asked != "sqlpassword":
        raise ValidationFailed(_NO_WINDOWS_LOGIN)


def _log_dropped(dropped: list[str], has_password: bool) -> None:
    if not dropped:
        return
    if has_password:  # a ';' or '&' inside the password splits it into pieces that look like unknown keys
        logger.warning("Dropping {} SQL Server URL parameters the driver does not take (names withheld: the URL "
                       "carries a password)", len(dropped))
    else:
        logger.warning("Dropping SQL Server URL parameters the driver does not take: {}", ", ".join(sorted(dropped)))


def parse_mssql_url(url: str) -> ParsedUrl:
    """`mssql://user:pass@host:1433/db?encrypt=true`, or JDBC's `sqlserver://host:1433;databaseName=db;user=u` (the
    registry strips a leading `jdbc:`). Parts the authority leaves empty may come from the properties; keys the
    driver does not take are dropped. Raises ValidationFailed with fixed text: the URL holds the password."""
    scheme, _, rest = url.partition("://")
    jdbc = ";" in rest.partition("?")[0]
    if jdbc:
        address, *properties = rest.split(";")
        split, port = _split_authority(f"{scheme}://{address}")
        pairs = _jdbc_pairs(properties)
    else:
        split, port = _split_authority(url)
        pairs = _query_pairs(split.query)
    keys = {**_SHARED_KEYS, **(_JDBC_KEYS if jdbc else {})}
    values: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in pairs:
        if name := keys.get(_fold(key)):
            values[name] = value
        else:
            dropped.append(key)
    _log_dropped(dropped, has_password="password" in values)
    _refuse_windows_logins(values)

    host = unquote(split.hostname) if split.hostname else values.get("server", "")
    if "\\" in host:
        raise ValidationFailed(_NAMED_INSTANCE)
    if port is None and "port" in values:
        port = _port_number(values["port"])
    if not host:
        raise ValidationFailed(f"A SQL Server URL needs a host, {_URL_HINT}")
    database = unquote(split.path.lstrip("/")) or values.get("database") or None
    username = unquote(split.username) if split.username else values.get("user") or None
    password = unquote(split.password) if split.password else values.get("password") or None
    ssl_fields: dict[str, Any] = {}
    if mode := _ssl_mode(values):
        ssl_fields["mode"] = mode
    draft = ServerProfileDraft(
        engine="mssql",
        name=f"{host}/{database}" if database else host,
        host=host,
        port=port,
        database=database,
        username=username,
        ssl=SslConfig(**ssl_fields),
        options={"tds_version": values["tds_version"]} if values.get("tds_version") else {},
    )
    return ParsedUrl(profile=draft, password=password)
