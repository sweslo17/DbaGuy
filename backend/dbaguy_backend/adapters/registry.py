import re
from collections.abc import Iterable

from pydantic import ValidationError

from ..domain.errors import UnsupportedEngine, ValidationFailed
from ..domain.profiles import Engine, EngineInfo
from .base import MALFORMED_URL_MESSAGE, DatabaseAdapter, ParsedUrl

_JDBC_PREFIX = re.compile(r"^jdbc:", re.IGNORECASE)  # connection strings pasted from JDBC tools
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,30}")


class AdapterRegistry:
    """Maps engines and URL schemes to adapters. New engines register here; nothing else changes."""

    def __init__(self, adapters: Iterable[DatabaseAdapter]) -> None:
        self._by_engine: dict[Engine, DatabaseAdapter] = {}
        self._by_scheme: dict[str, DatabaseAdapter] = {}
        for adapter in adapters:
            self._by_engine[adapter.engine] = adapter
            for scheme in adapter.url_schemes:
                self._by_scheme[scheme] = adapter

    def get(self, engine: Engine | str) -> DatabaseAdapter:
        try:
            return self._by_engine[Engine(engine)]
        except (KeyError, ValueError):
            raise UnsupportedEngine(f"Engine '{engine}' is not supported yet") from None

    def parse_url(self, url: str) -> ParsedUrl:
        url = _JDBC_PREFIX.sub("", url.strip(), count=1)
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        adapter = self._by_scheme.get(scheme)
        if adapter is None:
            # Echo the scheme only when it looks like one: a pasted key=value string would otherwise put its
            # password into the message (and the "rejected" log line).
            shown = f" '{scheme}'" if _SCHEME.fullmatch(scheme) else ""
            raise ValidationFailed(f"Unsupported URL scheme{shown}")
        try:
            return adapter.parse_url(url)
        except ValidationError as exc:  # the parsed parts do not make a valid profile, e.g. a port outside 1-65535
            # Only the model's own field name: neither the message nor the input value (could be a password) is echoed.
            location = exc.errors()[0]["loc"]
            raise ValidationFailed(f"The URL gives an invalid value for '{location[0] if location else 'profile'}'") from None
        except ValueError:  # urllib: a port that is not a number or out of range, a malformed authority
            raise ValidationFailed(MALFORMED_URL_MESSAGE) from None

    def engines(self) -> list[EngineInfo]:
        return [adapter.info() for adapter in self._by_engine.values()]
