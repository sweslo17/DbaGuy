"""How a SQL Server profile becomes pymssql._mssql.connect arguments, and a SQL Server URL a profile draft. Pure: no I/O."""

import os
import unicodedata
from collections.abc import Iterator, Mapping
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
_TDS_VERSION_MESSAGE = "tds_version must be one of 7.0-7.4"
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
_MIXED_FORMS = "This URL mixes the ?a=b&c=d and ;a=b forms. Use one form."
_KEY_VALUE = "A JDBC SQL Server URL takes key=value properties after the host"
_OPEN_BRACE = "A {...} value in the JDBC URL is not closed"
_FULLWIDTH_SEMICOLON = "；"  # what an IME types for ";"
# A URL path after the host is an instance name in these schemes (JDBC, go-mssqldb) and the database in SQLAlchemy's.
_INSTANCE_PATH_SCHEMES = frozenset({"sqlserver"})


# ---- profile -> pymssql ----


@dataclass(frozen=True)
class ConnectParams:
    kwargs: dict[str, Any]  # for pymssql._mssql.connect
    tds_section: TdsSection  # the adapter registers it (`freetds.active().section_for`) before the login


def _valid_tds_version(value: str) -> str | None:
    version = value.strip()
    return version if version in _TDS_VERSIONS else None


def _tds_version(options: Mapping[str, str]) -> str:
    """The `tds_version` option; options are an allow-list of that one key (key case-insensitive)."""
    version = TDS_VERSION
    ignored = []
    for key, value in options.items():
        if key.lower() == "tds_version":
            version = value
        else:
            ignored.append(key)
    if ignored:  # names only: the values may hold secrets
        logger.warning(
            "Ignoring SQL Server connection options (only tds_version is taken): {}", ", ".join(sorted(ignored))
        )
    if (valid := _valid_tds_version(version)) is None:
        raise ConnectionFailed(_TDS_VERSION_MESSAGE)
    return valid


def _tds_section(ssl: SslConfig, endpoint: Endpoint) -> TdsSection:
    """The section of DbaGuy's freetds.conf this login names. A login that verifies the certificate needs one anyway
    (pymssql has no parameter for a CA file or a host name check); the others get one so that FreeTDS never reads a
    section for the host from the user's own files. Raises ConnectionFailed for settings that cannot work."""
    if ssl.mode not in _VERIFYING_MODES:
        return TdsSection(endpoint.host, endpoint.port)
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
    the profile and this module, never from free-form options. `server` is the name of the login's freetds.conf
    section, which carries the host and port (an IPv6 host included: pymssql would not add the port to it), so a
    verifying login cannot happen without its section: without it FreeTDS reads the name as a host and fails.
    Raises ConnectionFailed for settings that cannot work."""
    if "\\" in profile.host or "\\" in endpoint.host:  # through a tunnel the endpoint is local: check the profile too
        raise ConnectionFailed(_NAMED_INSTANCE)
    tds_version = _tds_version(profile.options)
    section = _tds_section(profile.ssl, endpoint)
    if profile.ssl.cert_file or profile.ssl.key_file:
        logger.warning("SQL Server does not use client certificates; ignoring them for profile {}", profile.name)
    kwargs = {
        "server": section.name,
        "port": str(section.port),
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
    "user": "user", "username": "user", "userid": "user", "uid": "user",
    "password": "password", "pwd": "password",
    "database": "database", "databasename": "database", "instancename": "instance",
    "encrypt": "encrypt", "encryption": "encrypt", "trustservercertificate": "trust", "tdsversion": "tds_version",
    "integratedsecurity": "integrated", "authentication": "authentication",
}
_JDBC_KEYS = {"servername": "server", "portnumber": "port"}  # JDBC's spellings of the address
_FLAG_ON = frozenset({"true", "yes"})
# encrypt values: JDBC's and go-mssqldb's, and pymssql's own `encryption` (off, request, require). The verifying
# choice for `true` depends on trustServerCertificate: see `_ssl_mode`.
_ENCRYPT_MODES: dict[str, SslMode] = {
    "disable": "disable", "off": "disable",
    "false": "prefer", "no": "prefer", "optional": "prefer", "request": "prefer",
    "require": "require",
}
_ENCRYPT_ON = frozenset({"true", "yes", "mandatory"})
_PAIR_SEPARATORS = {False: ";&", True: ";&?"}  # what must not appear in a value, by form (False: URL, True: JDBC)


def _fold(key: str) -> str:
    """`TrustServerCertificate`, `trust_server_certificate` and `User ID` are `trustservercertificate` and `userid`."""
    return "".join(key.lower().split()).replace("-", "").replace("_", "")


def _is_password_key(key: str) -> bool:
    return _SHARED_KEYS.get(_fold(key)) == "password"


def _reject_mixed_forms(separators: str, *texts: str) -> None:
    """A value that holds the other form's separators is the rest of a URL, not a value: `?encrypt=true;password=x`
    would otherwise import `true;password=x` (the password into a field that is stored in plain text). Checked after
    NFKC so that the fullwidth ＆ and ？ count too. Fixed text: the value may hold the password."""
    for text in texts:
        if any(c in separators for c in unicodedata.normalize("NFKC", text)):
            raise ValidationFailed(_MIXED_FORMS)


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


def _query_pairs(query: str) -> Iterator[tuple[str, str]]:
    """`k=v&k=v`, percent-decoded. A "+" stays a plus: a password is not form-encoded. Only the password may hold `;`."""
    for part in query.split("&"):
        if part:
            raw_key, _, raw_value = part.partition("=")
            key = unquote(raw_key)
            _reject_mixed_forms(_PAIR_SEPARATORS[False], raw_key, "" if _is_password_key(key) else raw_value)
            yield key, unquote(raw_value).strip()


def _braced(text: str, start: int) -> tuple[str, int]:
    """The `{...}` value that opens at `text[start]` (`}}` is a literal `}`) and the position after its closing brace."""
    chars: list[str] = []
    pos = start + 1
    while pos < len(text):
        if text[pos] == "}":
            if text[pos + 1:pos + 2] != "}":
                return "".join(chars), pos + 1
            pos += 1
        chars.append(text[pos])
        pos += 1
    raise ValidationFailed(_OPEN_BRACE)


def _jdbc_pairs(text: str) -> Iterator[tuple[str, str]]:
    """`key=value;key={a;b}` after the host: values are kept as written (stripped), a `{...}` value may hold `;`.
    Empty parts (a trailing `;`) are skipped. Only the password may hold `&` and `?` outside braces."""
    pos, end = 0, len(text)
    while pos < end:
        semicolon = text.find(";", pos)
        equals = text.find("=", pos)
        if equals < 0 or 0 <= semicolon < equals:  # a part without "=": fine only when blank
            stop = end if semicolon < 0 else semicolon
            if text[pos:stop].strip():
                raise ValidationFailed(_KEY_VALUE)
            pos = stop + 1
            continue
        key = text[pos:equals].strip()
        if not key:
            raise ValidationFailed(_KEY_VALUE)
        pos = equals + 1
        while pos < end and text[pos].isspace():
            pos += 1
        if pos < end and text[pos] == "{":
            value, pos = _braced(text, pos)
            semicolon = text.find(";", pos)
            stop = end if semicolon < 0 else semicolon
            if text[pos:stop].strip():
                raise ValidationFailed(_KEY_VALUE)
            _reject_mixed_forms(_PAIR_SEPARATORS[True], key)  # the braces quoted the value: any character is its own
        else:
            semicolon = text.find(";", pos)
            stop = end if semicolon < 0 else semicolon
            value = text[pos:stop].strip()
            _reject_mixed_forms(_PAIR_SEPARATORS[True], key, "" if _is_password_key(key) else value)
        pos = stop + 1
        yield key, value


def _ssl_mode(values: Mapping[str, str], jdbc: bool) -> SslMode | None:
    trusts = values.get("trust", "").strip().lower() in _FLAG_ON
    value = values.get("encrypt")
    if value is None:
        # No encrypt key. JDBC drivers (mssql-jdbc 10.2+, Microsoft.Data.SqlClient) encrypt by default, and
        # trustServerCertificate=true asks for exactly that, unverified. A plain mssql:// URL asks for nothing.
        return "require" if jdbc or trusts else None
    key = value.strip().lower()
    if key in _ENCRYPT_MODES:
        return _ENCRYPT_MODES[key]
    if key in _ENCRYPT_ON:  # encrypt=true checks the certificate unless trustServerCertificate waives it
        return "require" if trusts else "verify-full"
    if key == "strict":
        raise ValidationFailed("encrypt=strict (TDS 8.0) is not supported; use encrypt=true")
    raise ValidationFailed(  # fixed text: the "value" may be the rest of a URL, password included
        "Unknown encrypt value in the SQL Server URL (use true, false, disable, off, request or require)"
    )


def _refuse_windows_logins(values: Mapping[str, str]) -> None:
    if values.get("integrated", "").strip().lower() in _FLAG_ON:
        raise ValidationFailed(_NO_WINDOWS_LOGIN)
    asked = values.get("authentication", "").strip().lower()  # blank asks for nothing
    if asked and asked != "sqlpassword":
        raise ValidationFailed(_NO_WINDOWS_LOGIN)


def parse_mssql_url(url: str) -> ParsedUrl:
    """`mssql://user:pass@host:1433/db?encrypt=true`, or JDBC's `sqlserver://host:1433;databaseName=db;user=u` (the
    registry strips a leading `jdbc:`). Parts the authority leaves empty may come from the properties; keys the
    driver does not take are dropped. Raises ValidationFailed with fixed text, never a part of the URL: it holds the
    password, and a URL that mixes the `?a=b&c=d` and `;a=b` forms can hide it in any value."""
    if _FULLWIDTH_SEMICOLON in url:
        raise ValidationFailed(MALFORMED_URL_MESSAGE)
    scheme, _, rest = url.partition("://")
    jdbc = ";" in rest.partition("?")[0]
    if jdbc:
        address, _, properties = rest.partition(";")
        split, port = _split_authority(f"{scheme}://{address}")
        pairs = _jdbc_pairs(properties)
    else:
        split, port = _split_authority(url)
        pairs = _query_pairs(split.query)
    keys = {**_SHARED_KEYS, **(_JDBC_KEYS if jdbc else {})}
    values: dict[str, str] = {}
    dropped = 0
    for key, value in pairs:
        if name := keys.get(_fold(key)):
            values[name] = value
        else:
            dropped += 1
    if dropped:  # a count, never names: a ';' or '&' inside the password splits it into pieces that look like keys
        logger.warning("Dropping {} SQL Server URL parameters the driver does not take", dropped)
    _refuse_windows_logins(values)
    if values.get("instance", "").strip() or (split.scheme in _INSTANCE_PATH_SCHEMES and split.path.strip("/")):
        raise ValidationFailed(_NAMED_INSTANCE)

    _reject_mixed_forms(_PAIR_SEPARATORS[jdbc], split.hostname or "", split.path)
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
    tds_version = values.get("tds_version", "").strip()
    if tds_version and _valid_tds_version(tds_version) is None:
        raise ValidationFailed(_TDS_VERSION_MESSAGE)
    ssl_fields: dict[str, Any] = {}
    if mode := _ssl_mode(values, jdbc):
        ssl_fields["mode"] = mode
    draft = ServerProfileDraft(
        engine="mssql",
        name=f"{host}/{database}" if database else host,
        host=host,
        port=port,
        database=database,
        username=username,
        ssl=SslConfig(**ssl_fields),
        options={"tds_version": tds_version} if tds_version else {},
    )
    return ParsedUrl(profile=draft, password=password)
