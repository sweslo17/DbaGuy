"""DbaGuy's own freetds.conf (spec §3.6). FreeTDS reads the file named by FREETDSCONF at every login; pointing it at a
file the app owns keeps ~/.freetds.conf and the system file out of DbaGuy's logins. Sections exist only for logins that
verify the server's certificate: pymssql has no parameter for a CA file or a host-name check."""

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from ...domain.errors import ConnectionFailed
from ...storage.atomic_file import atomic_text_file

_HEADER = (
    "# Written by DbaGuy: FreeTDS reads it at every SQL Server login. Edits are overwritten.\n"
    "[global]\n\ttds version = 7.4\n"
)


@dataclass(frozen=True)
class TdsSection:
    host: str
    port: int
    ca_file: str  # expanded path
    check_hostname: bool  # verify-full; verify-ca checks the CA only

    @property
    def name(self) -> str:
        digest = hashlib.sha256(f"{self.host}\0{self.port}\0{self.ca_file}\0{self.check_hostname}".encode()).hexdigest()
        return f"dbaguy_{digest[:16]}"


class FreeTdsConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sections: dict[str, TdsSection] = {}

    def render(self) -> str:
        parts = [_HEADER]
        for name, s in self._sections.items():
            parts.append(
                f"\n[{name}]\n\thost = {s.host}\n\tport = {s.port}\n\ttds version = 7.4\n\tencryption = require\n"
                f"\tca file = {s.ca_file}\n\tcheck certificate hostname = {'yes' if s.check_hostname else 'no'}\n"
            )
        return "".join(parts)

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_text_file(self.path) as fp:
            fp.write(self.render())

    def section_for(self, target: TdsSection) -> str:
        """The section's name; the file is rewritten (atomically) when the section is new."""
        if any(c in value for value in (target.host, target.ca_file) for c in "\r\n"):
            raise ConnectionFailed("The host name or CA file path holds a line break, which freetds.conf cannot carry")
        if not os.path.isfile(target.ca_file):
            raise ConnectionFailed(f"CA file not found: {target.ca_file}")
        with self._lock:
            if target.name not in self._sections:
                self._sections[target.name] = target
                self.write()
                logger.debug("freetds.conf section {} for {}:{}", target.name, target.host, target.port)
        return target.name


_lock = threading.Lock()
_location: Path | None = None
_active: FreeTdsConfig | None = None


def use(path: Path) -> None:
    """Record where the file goes. Nothing is written until the first SQL Server login (`active`): building the app
    never touches the data dir. A later call before the first `active()` wins."""
    global _location
    with _lock:
        _location = path


def active() -> FreeTdsConfig:
    """The config every SQL Server login reads. The first call writes the file at the recorded location (a fresh
    temporary directory when none was recorded: tests and ad-hoc runs without a container) and points FREETDSCONF at
    it; later calls return the same config. Call it before the login: FreeTDS reads the variable at login."""
    global _active
    with _lock:
        if _active is None:
            path = _location or Path(tempfile.mkdtemp(prefix="dbaguy-freetds-")) / "freetds.conf"
            config = FreeTdsConfig(path)
            config.write()
            os.environ["FREETDSCONF"] = str(path)
            _active = config
            logger.info("SQL Server logins read {}", path)
        return _active
