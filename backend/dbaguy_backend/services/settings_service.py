import os
from pathlib import Path

from loguru import logger

from ..domain.errors import StorageError, ValidationFailed
from ..domain.settings import AppSettings
from ..storage.json_files import JsonDocument


class SettingsService:
    """The app preferences in `settings.json`, read once and kept in memory."""

    def __init__(self, path: Path, default_scripts_dir: Path) -> None:
        self._doc = JsonDocument(path, AppSettings)
        self._default_scripts_dir = default_scripts_dir

    def get(self) -> AppSettings:
        return self._doc.get()

    def update(self, value: AppSettings) -> AppSettings:
        if value.scripts_dir is not None:
            scripts_dir = self._expand(value.scripts_dir)  # rejects what is not an absolute path
            if os.path.exists(scripts_dir) and not os.path.isdir(scripts_dir):
                raise ValidationFailed(f"{scripts_dir} exists and is not a folder")
        self._doc.save(value)
        logger.info("Settings saved")
        return value

    def scripts_root(self) -> Path:
        """The scripts folder: the configured one or the default, created when missing."""
        configured = self.get().scripts_dir
        root = self._expand(configured) if configured else self._default_scripts_dir
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create the scripts folder {}: {}", root, exc)
            raise StorageError(f"Cannot create the scripts folder {root}: {exc.strerror or exc}") from exc
        return root

    @staticmethod
    def _expand(configured: str) -> Path:
        """The configured scripts folder with `~` expanded; it must be an absolute path."""
        try:
            path = Path(configured).expanduser()
        except RuntimeError as exc:  # `~someone` for a user that does not exist
            raise ValidationFailed("The scripts folder starts with an unknown home folder") from exc
        if not path.is_absolute():
            raise ValidationFailed("The scripts folder must be an absolute path")
        return path
