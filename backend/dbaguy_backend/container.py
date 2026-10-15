from .config import Settings


class AppContainer:
    """Builds and owns every long-lived service. Later tasks add services here."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def start(self) -> None:
        """Start background housekeeping. No-op until sessions exist."""

    def close(self) -> None:
        """Release all resources (sessions, tunnels)."""
