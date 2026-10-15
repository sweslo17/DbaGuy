from collections.abc import Iterable

from ..domain.errors import UnsupportedEngine, ValidationFailed
from ..domain.profiles import Engine, EngineInfo
from .base import DatabaseAdapter, ParsedUrl


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
        scheme = url.split(":", 1)[0].strip().lower() if ":" in url else ""
        adapter = self._by_scheme.get(scheme)
        if adapter is None:
            raise ValidationFailed(f"Unsupported URL scheme '{scheme}'")
        return adapter.parse_url(url.strip())

    def engines(self) -> list[EngineInfo]:
        return [adapter.info() for adapter in self._by_engine.values()]
