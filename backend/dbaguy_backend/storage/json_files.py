"""JSON documents on disk: atomic writes, unknown fields kept, unreadable files moved aside instead of overwritten."""

from __future__ import annotations

import itertools
import json
import threading
import time
from pathlib import Path
from typing import Any, Generic, TypeVar

from loguru import logger
from pydantic import BaseModel, ValidationError

from ..domain.errors import DomainError, StorageError
from .atomic_file import atomic_text_file

M = TypeVar("M", bound=BaseModel)


def overlay(raw: dict[str, Any], model: BaseModel) -> dict[str, Any]:
    """`model`'s fields written over `raw`, keeping the fields this build does not know (nested models too)."""
    merged = dict(raw)
    for name, value in model.model_dump(mode="json").items():
        nested = getattr(model, name)
        if isinstance(nested, BaseModel):
            base = raw.get(name)
            merged[name] = overlay(base if isinstance(base, dict) else {}, nested)
        else:
            merged[name] = value  # plain values, lists and free-form dicts are replaced as a whole
    return merged


def error_fields(exc: ValidationError) -> str:
    """Where validation failed, as field paths only: never the values (they could be anything, e.g. a secret)."""
    return ", ".join(sorted({".".join(str(part) for part in err["loc"]) or "<entry>" for err in exc.errors()}))


def move_aside(path: Path, reason: str, error: type[DomainError]) -> None:
    """Back the unreadable file up under a unique name, so that saving later never destroys it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = next(
        candidate
        for counter in itertools.count(1)
        if not (candidate := path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")).exists()
    )
    try:
        path.replace(backup)
    except OSError as exc:
        logger.error("{} is unreadable ({}) and could not be moved aside: {}", path.name, reason, exc)
        raise error(f"{path.name} is unreadable and could not be moved aside: {exc.strerror or exc}") from exc
    logger.error("{} is unreadable ({}); moved it to {}", path.name, reason, backup.name)


def write_text_atomic(path: Path, text: str, error: type[DomainError]) -> None:
    """Replace `path` with `text` in one step (parent folders are created). A failure leaves the old file as it was."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_text_file(path) as fp:
            fp.write(text)
    except (OSError, UnicodeEncodeError) as exc:  # the second: text that is not encodable, e.g. a lone surrogate
        logger.error("Cannot write {}: {}", path.name, exc)
        raise error(f"Cannot save {path.name}: {getattr(exc, 'strerror', None) or exc}") from exc


def write_json_atomic(path: Path, payload: Any, error: type[DomainError]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False), error)


class JsonDocument(Generic[M]):
    """One pydantic model stored as a JSON file. Fields this build does not know survive a round trip; an unreadable
    or invalid file is moved aside and the defaults are used. The value is read once and kept in memory."""

    def __init__(self, path: Path, model: type[M]) -> None:
        self._path = path
        self._model = model
        self._raw: dict[str, Any] = {}  # the file as found; the model's fields are laid over it when saving
        self._value: M | None = None  # None until the file was read
        self._lock = threading.Lock()

    def load(self) -> M:
        """Read the file again."""
        with self._lock:
            return self._read()

    def get(self) -> M:
        """The value as of the last read or save; the file is read the first time."""
        with self._lock:
            return self._value if self._value is not None else self._read()

    def save(self, value: M) -> None:
        with self._lock:
            if self._value is None:
                self._read()  # a file nobody has read yet: keep what it holds that this build does not know
            payload = overlay(self._raw, value)
            write_json_atomic(self._path, payload, StorageError)
            self._raw, self._value = payload, value

    def _read(self) -> M:
        self._raw = {}
        if not self._path.exists():
            self._value = self._model()
            return self._value
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            value = self._model.model_validate(raw, extra="ignore")  # a newer build may add fields
        except ValidationError as exc:  # before ValueError, which it is a subclass of
            move_aside(self._path, f"invalid {error_fields(exc)}", StorageError)
            value = self._model()
        except ValueError as exc:  # not UTF-8 (UnicodeDecodeError), not JSON, or not an object
            move_aside(self._path, str(exc), StorageError)
            value = self._model()
        except OSError as exc:
            logger.error("Cannot read {}: {}", self._path.name, exc)
            raise StorageError(f"Cannot read {self._path.name}: {exc.strerror or exc}") from exc
        else:
            self._raw = raw
        self._value = value
        return value
