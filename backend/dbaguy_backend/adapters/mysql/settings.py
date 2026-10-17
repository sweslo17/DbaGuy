"""How a MySQL profile becomes mysql-connector arguments, and a MySQL URL a profile draft. Pure: no I/O."""

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import ParseResult, parse_qsl, unquote, urlparse

from loguru import logger
from mysql.connector.constants import DEFAULT_CONFIGURATION, ClientFlag

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.profiles import ServerProfile, ServerProfileDraft, SslConfig, SslMode
from ..base import MALFORMED_URL_MESSAGE, URL_ENCODING_HINT, Endpoint, ParsedUrl

URL_SCHEMES = ("mysql", "mysql+mysqlconnector", "mysql+pymysql")
CONNECT_TIMEOUT_S = 10
KILL_CONNECT_TIMEOUT_S = 3  # the worker holds its state lock while a cancel connects
_DEFAULTS: dict[str, Any] = {"connection_timeout": CONNECT_TIMEOUT_S, "charset": "utf8mb4"}
# Arguments that pick the target, carry secrets or decide DbaGuy's guarantees (one statement per call, no LOCAL INFILE,
# read-only not undone by init_command, pure driver, warnings as one message without extra queries or raising):
# never taken from free-form options or a URL.
RESERVED_OPTIONS = frozenset({
    "user", "password", "passwd", "password1", "password2", "password3", "host", "port", "database", "db",
    "unix_socket", "client_flags", "use_pure", "allow_local_infile", "allow_local_infile_in_path", "autocommit",
    "consume_results", "raw", "buffered", "converter_class", "dsn", "option_files", "option_groups", "init_command",
    "conn_attrs", "pool_name", "pool_size", "pool_reset_session", "ssl_disabled", "ssl_ca", "ssl_cert", "ssl_key",
    "ssl_verify_cert", "ssl_verify_identity", "get_warnings", "raise_on_warnings",
    # The driver's other spellings of reserved keys (`read_default_file` reads an option file that can set
    # init_command and the target) and a callable it imports by name and calls with text the server sends.
    "username", "read_default_file", "webauthn_callback",
    # Redirect the target (an SRV lookup, a failover list) or read a file the profile did not name.
    "dns_srv", "failover", "openid_token_file",
})
_SSL_MODES: dict[str, SslMode] = {  # MySQL ssl-mode, Connector/J sslMode and libpq sslmode spellings
    "disabled": "disable", "disable": "disable", "preferred": "prefer", "prefer": "prefer",
    "required": "require", "require": "require", "verify-ca": "verify-ca", "verify-identity": "verify-full",
    "verify-full": "verify-full",
}
_SSL_QUERY_KEYS = {  # keys are folded (see `_fold`); `sslrootcert` is libpq's name for the CA file
    "sslmode": "mode", "sslca": "ca_file", "sslrootcert": "ca_file", "sslcert": "cert_file", "sslkey": "key_file",
}
_VERIFYING_MODES = frozenset({"verify-ca", "verify-full"})
_TLS_MODES = frozenset({"require", *_VERIFYING_MODES})
_INTEGER = re.compile(r"-?\d+")
# Several hosts or Connector/J's `address=(host=h)(port=p)` / `(host=h,port=p)` forms in the authority. The check looks
# at the host part only: a password may hold commas and parentheses.
_HOST_LIST_CHARS = re.compile(r"[,()]")
_ADDRESS_FORM = re.compile(r"(?:^|@)\s*address\s*=", re.IGNORECASE)
_HOST_LIST_MESSAGE = (
    "A MySQL URL takes one host: lists of hosts and address=(...) forms are not supported, "
    "e.g. mysql://user@host:3306/db"
)
# The driver's own spelling of the same setting: it drops the alias when the canonical key is also given.
_CONNECT_TIMEOUT_ALIAS = "connect_timeout"


def _fixed() -> dict[str, Any]:
    """Fresh each time: the driver adds its own entries to `conn_attrs`."""
    return {
        "use_pure": True,
        "autocommit": True,
        "client_flags": [-ClientFlag.MULTI_STATEMENTS],  # the server then rejects a second statement (1064)
        "allow_local_infile": False,
        "consume_results": False,
        "conn_attrs": {"program_name": "DbaGuy"},
    }


def _is_reserved(key: str) -> bool:
    return key.lower() in RESERVED_OPTIONS


def _option_value(value: str) -> str | int | bool:
    """Profile options are strings; the driver wants real booleans and numbers. Options that take a list, such as
    `tls_versions`, are given in the driver's own string form: `[TLSv1.2,TLSv1.3]`."""
    text = value.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if _INTEGER.fullmatch(text):
        return int(text)
    return value


def _driver_options(options: Mapping[str, str]) -> dict[str, Any]:
    ignored = [key for key in options if _is_reserved(key)]
    if ignored:
        logger.warning("Ignoring reserved MySQL connection options: {}", ", ".join(sorted(ignored)))
    coerced = {key: _option_value(value) for key, value in options.items() if not _is_reserved(key)}
    if _CONNECT_TIMEOUT_ALIAS in coerced:
        coerced.setdefault("connection_timeout", coerced.pop(_CONNECT_TIMEOUT_ALIAS))
    return coerced


def requires_tls(ssl: SslConfig) -> bool:
    """`require` and up fail when the server does not end up speaking TLS (the driver alone only tries)."""
    return ssl.mode in _TLS_MODES


def _tls_params(ssl: SslConfig, endpoint: Endpoint) -> dict[str, Any]:
    if ssl.mode == "disable":
        return {"ssl_disabled": True}
    if ssl.mode == "verify-full" and endpoint.tls_server_name:
        raise ConnectionFailed(
            "verify-full checks the server's host name, which mysql-connector cannot do through an SSH tunnel "
            "(it would check 127.0.0.1); use verify-ca, or connect without the tunnel"
        )
    if bool(ssl.cert_file) != bool(ssl.key_file):
        raise ConnectionFailed("Give both a client certificate and its key, or neither")
    params: dict[str, Any] = {}
    if ssl.mode in _VERIFYING_MODES:  # the CA only matters when the certificate is checked
        params["ssl_verify_cert"] = True
        if ssl.ca_file:  # without one the driver uses the system trust store
            params["ssl_ca"] = os.path.expanduser(ssl.ca_file)
    if ssl.mode == "verify-full":
        params["ssl_verify_identity"] = True
    if ssl.cert_file and ssl.key_file:
        params["ssl_cert"] = os.path.expanduser(ssl.cert_file)
        params["ssl_key"] = os.path.expanduser(ssl.key_file)
    return params  # prefer / require: the driver tries TLS without verifying; `require` is checked after connecting


def connect_params(profile: ServerProfile, endpoint: Endpoint, password: str | None, database: str | None) -> dict[str, Any]:
    """Arguments for `mysql.connector.connect`. Free-form options only override the defaults; everything that picks
    the target or sets DbaGuy's guarantees comes last and wins. Raises ConnectionFailed for TLS settings that cannot work."""
    params: dict[str, Any] = {
        **_DEFAULTS,
        **_driver_options(profile.options),
        **_tls_params(profile.ssl, endpoint),
        **_fixed(),
        "host": endpoint.host,
        "port": endpoint.port,
        "user": profile.username or "",
        "password": password or "",
    }
    if name := database or profile.database:
        params["database"] = name
    return params


def kill_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments for the short-lived connection that sends KILL QUERY: the same server and login, no default database.
    The caller adds a fresh `conn_attrs` for every connection."""
    kill = {key: value for key, value in params.items() if key not in ("database", "conn_attrs")}
    kill["connection_timeout"] = KILL_CONNECT_TIMEOUT_S
    kill["client_flags"] = list(params["client_flags"])
    return kill


def _split_url(url: str) -> tuple[ParseResult, int | None]:
    """The parts of a MySQL URL. Every message here is fixed text: urllib's own quotes the authority, password included."""
    try:
        parsed = urlparse(url)
    except ValueError:
        raise ValidationFailed(MALFORMED_URL_MESSAGE) from None
    if _HOST_LIST_CHARS.search(parsed.netloc.rpartition("@")[2]) or _ADDRESS_FORM.search(parsed.netloc):
        raise ValidationFailed(_HOST_LIST_MESSAGE)
    try:
        port = parsed.port
    except ValueError:
        raise ValidationFailed(f"Invalid port in the MySQL URL. {URL_ENCODING_HINT}") from None
    if port == 0:
        raise ValidationFailed("Invalid port in the MySQL URL")
    return parsed, port


def _ssl_mode(value: str) -> SslMode:
    try:
        return _SSL_MODES[value.strip().lower().replace("_", "-")]
    except KeyError:
        raise ValidationFailed(f"Unknown ssl-mode '{value}' in the MySQL URL") from None


def _fold(key: str) -> str:
    """`ssl-mode`, `ssl_mode` and `sslMode` are one key."""
    return key.lower().replace("-", "").replace("_", "")


_JDBC_SSL_FLAGS = frozenset({"usessl", "requiressl", "verifyservercertificate"})


_FLAGS = {"true": True, "yes": True, "false": False, "no": False}


def _flag(value: str) -> bool | None:
    return _FLAGS.get(value.strip().lower())  # Connector/J's booleans also take yes/no


def _jdbc_ssl_mode(flags: Mapping[str, bool]) -> SslMode | None:
    """Connector/J's switches when the URL names no ssl-mode. A pasted URL never gets weaker TLS than it asked for:
    `verifyServerCertificate` means verify-ca (the system trust store, no fallback), `requireSSL` at least require."""
    if flags.get("usessl") is False:
        return "disable"
    if flags.get("verifyservercertificate"):
        return "verify-ca"
    if flags.get("requiressl") or flags.get("usessl"):
        return "require"
    return None


def parse_mysql_url(url: str) -> ParsedUrl:
    """`mysql://user:pass@host:3306/db?ssl-mode=REQUIRED`, with the `jdbc:` prefix already stripped by the registry.
    Parts the authority leaves empty may come from the query (`user`, `password`); query keys the driver does not know,
    and the reserved ones, are dropped."""
    parsed, port = _split_url(url)
    host = parsed.hostname
    if not host:
        raise ValidationFailed("A MySQL URL needs a host, e.g. mysql://user@host:3306/db")
    ssl_fields: dict[str, str] = {}
    jdbc_flags: dict[str, bool] = {}  # Connector/J's TLS switches
    query_user = query_password = None
    options: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in parse_qsl(parsed.query):
        folded = _fold(key)
        if folded in _SSL_QUERY_KEYS:
            ssl_fields[_SSL_QUERY_KEYS[folded]] = value
        elif folded in _JDBC_SSL_FLAGS:
            if (flag := _flag(value)) is not None:
                jdbc_flags[folded] = flag
        elif folded in ("user", "username"):
            query_user = value
        elif folded in ("password", "passwd"):
            query_password = value
        elif key in DEFAULT_CONFIGURATION and not _is_reserved(key):
            options[key] = value
        else:
            dropped.append(key)
    if dropped:
        logger.warning("Dropping MySQL URL parameters the driver does not take: {}", ", ".join(sorted(dropped)))
    if "mode" in ssl_fields:
        ssl_fields["mode"] = _ssl_mode(ssl_fields["mode"])
    elif mode := _jdbc_ssl_mode(jdbc_flags):
        ssl_fields["mode"] = mode
    database = unquote(parsed.path.lstrip("/")) or None
    username = unquote(parsed.username) if parsed.username else query_user
    password = unquote(parsed.password) if parsed.password else query_password
    draft = ServerProfileDraft(
        engine="mysql",
        name=f"{host}/{database}" if database else host,
        host=host,
        port=port,
        database=database,
        username=username,
        ssl=SslConfig(**ssl_fields),
        options=options,
    )
    return ParsedUrl(profile=draft, password=password)
