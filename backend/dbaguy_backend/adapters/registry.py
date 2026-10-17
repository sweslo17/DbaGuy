import re
from collections.abc import Iterable

from pydantic import ValidationError

from ..domain.errors import UnsupportedEngine, ValidationFailed
from ..domain.profiles import Engine, EngineInfo
from .base import DatabaseAdapter, ParsedUrl

_JDBC_PREFIX = re.compile(r"^jdbc:", re.IGNORECASE)  # connection strings pasted from JDBC tools


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
            raise ValidationFailed(f"Unsupported URL scheme '{scheme}'")
        try:
            return adapter.parse_url(url)
        except ValidationError as exc:  # the parsed parts do not make a valid profile, e.g. a port outside 1-65535
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"])
            raise ValidationFailed(f"Invalid connection URL: {field}: {first['msg']}") from None
        except ValueError as exc:  # urllib: a port that is not a number or out of range, a malformed host
            raise ValidationFailed(f"Invalid connection URL: {exc}") from None

    def engines(self) -> list[EngineInfo]:
        return [adapter.info() for adapter in self._by_engine.values()]
