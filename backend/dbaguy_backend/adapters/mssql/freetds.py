"""DbaGuy's own freetds.conf (spec §3.6). FreeTDS reads the file named by FREETDSCONF at every login, and every SQL
Server login names one of this file's sections (`connect_params` sets `server` to the section's name): FreeTDS finds
its settings here and never reads another file, so neither a section a host has in ~/.freetds.conf or the system file,
nor their [global] section, applies. The section's name ends in `:port` on purpose: FreeTDS first looks for a section
named `server:port` in every file, reading each file's [global] section on the way, and only then for `server` alone.
A section also carries what pymssql has no parameter for: the CA file and the host-name check of verify-ca and
verify-full."""

import hashlib
import os
import re
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
# What freetds.conf cannot carry in a value: it starts a comment, and leading, trailing and repeated white space is
# not kept. (Line breaks are refused first, with their own message.)
_UNCARRIABLE = re.compile(r"[#;]|[^\S ]|  |^ | $")


@dataclass(frozen=True)
class TdsSection:
    host: str
    port: int
    ca_file: str = ""  # expanded path; empty: the login encrypts as asked but does not check the certificate
    check_hostname: bool = False  # verify-full; verify-ca checks the CA only

    @property
    def name(self) -> str:
        digest = hashlib.sha256(f"{self.host}\0{self.port}\0{self.ca_file}\0{self.check_hostname}".encode()).hexdigest()
        return f"dbaguy_{digest[:16]}:{self.port}"


def _render(sections: dict[str, TdsSection]) -> str:
    parts = [_HEADER]
    for name, s in sections.items():
        parts.append(f"\n[{name}]\n\thost = {s.host}\n\tport = {s.port}\n\ttds version = 7.4\n")
        if s.ca_file:  # the encryption level of a login without a CA file is the `encryption` argument's alone
            parts.append(
                f"\tencryption = require\n\tca file = {s.ca_file}\n"
                f"\tcheck certificate hostname = {'yes' if s.check_hostname else 'no'}\n"
            )
    return "".join(parts)


class FreeTdsConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sections: dict[str, TdsSection] = {}

    def render(self) -> str:
        return _render(self._sections)

    def write(self) -> None:
        self._write(self._sections)

    def _write(self, sections: dict[str, TdsSection]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with atomic_text_file(self.path) as fp:
                fp.write(_render(sections))
        except OSError as exc:
            raise ConnectionFailed(
                f"Cannot write DbaGuy's freetds.conf at {self.path}: {exc.strerror or type(exc).__name__}"
            ) from exc

    def section_for(self, target: TdsSection) -> str:
        """The section's name; the file is rewritten (atomically) when the section is new, and the section counts as
        known only once the file holds it."""
        values = (target.host, target.ca_file)
        if any(c in value for value in values for c in "\r\n"):
            raise ConnectionFailed("The host name or CA file path holds a line break, which freetds.conf cannot carry")
        if any(_UNCARRIABLE.search(value) for value in values):
            raise ConnectionFailed(
                "The host name or CA file path holds a '#', a ';' or repeated white space, which freetds.conf cannot carry"
            )
        if target.ca_file and not os.path.isfile(target.ca_file):
            raise ConnectionFailed(f"CA file not found: {target.ca_file}")
        with self._lock:
            if target.name not in self._sections:
                sections = {**self._sections, target.name: target}
                self._write(sections)
                self._sections = sections
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
    it; later calls return the same config. Call it before the login: FreeTDS reads the variable at login. Raises
    ConnectionFailed when the file cannot be written."""
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
