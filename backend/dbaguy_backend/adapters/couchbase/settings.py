"""How a Couchbase profile becomes connection settings, and a Couchbase URL a profile draft (spec §3.6.1, §3.6.2,
§3.6.4). No network: `tls_context` reads the certificate files, everything else is text.

IPv6 hosts are written `[::1]` in a profile and a URL and handed on without brackets (`seed_hosts`): `getaddrinfo`
refuses `[::1]`, and the HTTP layer adds the brackets to a label or a Host header itself (ruling R-04a-C18)."""

import os
import re
import ssl
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote

from loguru import logger

from ...domain.errors import ConnectionFailed, ValidationFailed
from ...domain.execution import QueryContext
from ...domain.profiles import ServerProfile, ServerProfileDraft, SslConfig, SslMode
from ...sql.dialects import keyspace_path
from ..base import MALFORMED_URL_MESSAGE, Endpoint, ParsedUrl

URL_SCHEMES = ("couchbase", "couchbases", "http", "https")
QUERY_PORT = 8093
QUERY_TLS_PORT = 18093
MANAGER_PORT = 8091
MANAGER_TLS_PORT = 18091

_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
_TLS_SCHEMES = frozenset({"couchbases", "https"})
_PORT_OPTION = "manager_port"
_DISCOVER_OPTION = "discover_nodes"
_KNOWN_OPTIONS = (_PORT_OPTION, _DISCOVER_OPTION)
_CA_KEYS = frozenset({"certpath", "truststorepath"})
_PORT_TEXT = re.compile(r"[0-9]{1,5}")  # ASCII digits only: str.isdigit() takes "²", int() takes fullwidth digits
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # a URL parameter key that is safe to name in the log
# The authority (up to the first `/`, `?` or `#`), the path, the query; the fragment is dropped.
_URL_PARTS = re.compile(r"([^/?#]*)([^?#]*)(?:\?([^#]*))?")
_URL_START = ", ".join(f"{scheme}://" for scheme in URL_SCHEMES)


def seed_hosts(host: str) -> list[str]:
    """`cb1, cb2` and `[::1]` as host names, without brackets and spaces. An empty item (`a,,b`, a trailing comma) is
    a typo that would otherwise connect to nothing."""
    items = [item.strip() for item in host.split(",")]
    hosts = [item[1:-1].strip() if item.startswith("[") and item.endswith("]") else item for item in items]
    if not all(hosts):
        raise ValidationFailed("A host list has an empty item: write hosts as cb1,cb2")
    return hosts


def uses_tls(config: SslConfig) -> bool:
    """`prefer` is `disable`: Couchbase's TLS is another port, so there is nothing to try (D-04-7)."""
    return config.mode in _TLS_MODES


def _default_query_port(tls: bool) -> int:
    return QUERY_TLS_PORT if tls else QUERY_PORT


def query_port(profile: ServerProfile) -> int:
    return profile.port or _default_query_port(uses_tls(profile.ssl))


def endpoint_target(profile: ServerProfile) -> tuple[str, int]:
    """Where ConnectionService connects or tunnels to: the first seed's query port. A tunnel reaches one node."""
    seeds = seed_hosts(profile.host)
    if profile.ssh.enabled and len(seeds) > 1:
        raise ValidationFailed("An SSH tunnel reaches one node: give a single host")
    return seeds[0], query_port(profile)


def _no_passphrase() -> bytes:
    return b""


@contextmanager
def _reading(what: str, hint: str = "") -> Iterator[None]:
    """A file-reading step whose failure names the kind of file (never the path) and what the system or OpenSSL said."""
    try:
        yield
    except (OSError, ssl.SSLError) as exc:
        reason = exc.strerror or str(exc) or type(exc).__name__
        raise ConnectionFailed(f"Could not read the {what}: {reason}{f' ({hint})' if hint else ''}") from exc


def tls_context(config: SslConfig) -> ssl.SSLContext | None:
    """disable / prefer: none (plain HTTP); require: TLS without checks; verify-ca: the CA file (or OpenSSL's default
    bundle — not the macOS keychain, so it fails closed without one); verify-full: also the host name (D-04-7)."""
    if not uses_tls(config):
        return None
    if bool(config.cert_file) != bool(config.key_file):
        raise ConnectionFailed("Give both a client certificate and its key, or neither")
    cafile = os.path.expanduser(config.ca_file) if config.ca_file and config.mode != "require" else None
    with _reading("CA file"):
        context = ssl.create_default_context(cafile=cafile)
    if config.mode != "verify-full":
        context.check_hostname = False
    if config.mode == "require":
        context.verify_mode = ssl.CERT_NONE
    if config.cert_file and config.key_file:
        # `password=`: without it OpenSSL asks for a passphrase on the terminal, and a sidecar has nobody to ask.
        with _reading("client certificate and key", hint="a key protected by a passphrase is not supported"):
            context.load_cert_chain(
                os.path.expanduser(config.cert_file), os.path.expanduser(config.key_file), password=_no_passphrase
            )
    return context


def request_context(context: QueryContext) -> str:
    """The `query_context` field of a request: `default:` + the quoted bucket and scope."""
    return "default:" + keyspace_path(context.bucket, context.scope)


@dataclass(frozen=True)
class CouchbaseSettings:
    """What a connection needs, decided once from the profile. `seeds` and `port` are where to connect when no node is
    discovered (an SSH tunnel: the local end); `server_hostname` is the real host name to check a certificate against
    when the seeds are a tunnel's local address."""

    seeds: tuple[str, ...]
    port: int
    tls: ssl.SSLContext | None
    tls_mode: SslMode
    server_hostname: str | None
    discover: bool
    manager_port: int
    default_context: QueryContext | None
    read_only: bool
    username: str
    tunnel: bool


@dataclass(frozen=True)
class _Options:
    manager_port: int | None
    discover: bool


def _port(text: str) -> int | None:
    """A TCP port written with ASCII digits, else None."""
    if _PORT_TEXT.fullmatch(text) and 1 <= (port := int(text)) <= 65535:
        return port
    return None


def _options(options: Mapping[str, str]) -> _Options:
    """The profile's free-form options. A blank value is "not set" (the dialog leaves a field empty), not an error.
    Errors name the option, never its value."""
    known: dict[str, str] = {}
    unknown: list[str] = []
    for key, value in options.items():
        if (folded := key.strip().lower()) in _KNOWN_OPTIONS:
            known[folded] = value.strip()
        else:
            unknown.append(key)
    if unknown:
        logger.warning("Ignoring Couchbase connection options: {}", ", ".join(sorted(unknown)))
    manager_port = None
    if known.get(_PORT_OPTION):
        manager_port = _port(known[_PORT_OPTION])
        if manager_port is None:
            raise ConnectionFailed(f"The Couchbase option {_PORT_OPTION} must be a port number (1-65535)")
    flag = known.get(_DISCOVER_OPTION, "").lower()
    if flag not in ("", "true", "false"):
        raise ConnectionFailed(f"The Couchbase option {_DISCOVER_OPTION} must be true or false")
    return _Options(manager_port, flag != "false")


def _default_context(text: str | None) -> QueryContext | None:
    try:
        return QueryContext.parse(text)
    except ValueError:  # pydantic's ValidationError too: its text would repeat the input
        raise ConnectionFailed(
            "The default query context is not a bucket or bucket.scope "
            "(write a bucket name that holds a dot in back-ticks)"
        ) from None


def connection_settings(profile: ServerProfile, endpoint: Endpoint, database: str | None) -> CouchbaseSettings:
    """Raises ConnectionFailed for settings that cannot work. `endpoint` matters only with an SSH tunnel (D-04-6: the
    tunnel is "this node": no discovery); otherwise the profile alone decides. `database` (a tab opened from the
    history) replaces the profile's default query context."""
    if not (profile.username or "").strip():
        raise ConnectionFailed("Couchbase needs a user name: the query service does not take anonymous requests")
    options = _options(profile.options)
    tls = tls_context(profile.ssl)
    tunnel = profile.ssh.enabled
    port = endpoint.port if tunnel else query_port(profile)
    return CouchbaseSettings(
        seeds=(endpoint.host,) if tunnel else tuple(seed_hosts(profile.host)),
        port=port,
        tls=tls,
        tls_mode=profile.ssl.mode,
        server_hostname=endpoint.tls_server_name if tunnel else None,
        # A tunnel is "this node", and a port of one's own (a load balancer, a proxy) is "this endpoint": discovery
        # would replace either (D-04-6).
        discover=not tunnel and options.discover and port == _default_query_port(tls is not None),
        manager_port=options.manager_port or (MANAGER_TLS_PORT if tls is not None else MANAGER_PORT),
        default_context=_default_context(database if database is not None else profile.database),
        read_only=profile.read_only,
        username=profile.username,
        tunnel=tunnel,
    )


def _host_and_port(item: str) -> tuple[str, int | None]:
    """`host`, `host:port`, `[v6]` or `[v6]:port`. Every refusal is fixed text: the URL holds a password."""
    if item.startswith("["):
        close = item.find("]")
        rest = item[close + 1:]
        if close < 0 or (rest and not rest.startswith(":")):
            raise ValidationFailed(MALFORMED_URL_MESSAGE)
        host, port_text = item[1:close], rest[1:]
    else:
        host, _, port_text = item.partition(":")  # an IPv6 address without brackets leaves a "port" that is no number
    if not port_text:
        return host, None
    if not _PORT_TEXT.fullmatch(port_text):
        raise ValidationFailed(MALFORMED_URL_MESSAGE)
    if (port := _port(port_text)) is None:
        raise ValidationFailed("Invalid port in the Couchbase URL")
    return host, port


def _shown_key(key: str) -> str:
    """A URL parameter key as the log may show it: a name, not whatever a pasted string put there."""
    return key if _PLAIN_KEY.fullmatch(key) else "(unnamed)"


def parse_couchbase_url(url: str) -> ParsedUrl:
    """`couchbase://[user[:password]@]h1[:port],h2…[/bucket[.scope]][?…]`, `couchbases://…`, `http(s)://host:port/…`
    (spec §3.6.2). A `couchbase://` port is the key-value port (or the old manager port), never the query port, so
    it is dropped. Messages and logs never repeat the URL: its authority may hold a password."""
    scheme, separator, rest = url.partition("://")
    scheme = scheme.lower()
    if not separator or scheme not in URL_SCHEMES:
        raise ValidationFailed(f"A Couchbase URL starts with one of {_URL_START}")
    authority, path, query = _URL_PARTS.match(rest).groups()
    userinfo, _, hosts = authority.rpartition("@")
    username, _, password = userinfo.partition(":")
    items = [_host_and_port(item.strip()) for item in hosts.split(",")]
    if not all(host for host, _ in items):
        raise ValidationFailed("A Couchbase URL needs a host, e.g. couchbase://cb1,cb2/bucket")
    direct = scheme in ("http", "https")  # one query node at an explicit port, not a cluster's seed list
    if direct and len(items) > 1:
        raise ValidationFailed("An http(s) URL names one query node, e.g. http://host:8093")
    ssl_fields: dict[str, str] = {"mode": "verify-full" if scheme in _TLS_SCHEMES else "disable"}
    dropped: list[str] = []
    for key, value in parse_qsl(query or "", keep_blank_values=True):
        if key.lower() in _CA_KEYS:
            if value:
                ssl_fields["ca_file"] = value
        elif key.lower() == "ssl" and value.lower() == "no_verify":
            ssl_fields["mode"] = "require"
        else:
            dropped.append(key)
    if dropped:
        names = sorted({_shown_key(key) for key in dropped})
        logger.warning("Dropping Couchbase URL parameters DbaGuy does not take: {}", ", ".join(names))
    database = unquote(path.removeprefix("/").removesuffix("/"))
    if "/" in database:
        raise ValidationFailed("The path of a Couchbase URL is /bucket or /bucket.scope")
    shown = [f"[{host}]" if ":" in host else host for host, _ in items]  # IPv6 keeps its brackets in the profile
    draft = ServerProfileDraft(
        engine="couchbase",
        name=f"{shown[0]}/{database}" if database else shown[0],
        host=",".join(shown),
        port=items[0][1] if direct else None,
        database=database or None,
        username=unquote(username) or None,
        ssl=SslConfig(**ssl_fields),
    )
    return ParsedUrl(profile=draft, password=unquote(password) or None)
