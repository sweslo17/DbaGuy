from __future__ import annotations

import itertools
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..domain.errors import ProfileNotFound, ProfileStoreError
from ..domain.profiles import AnyDraft, AnyProfile, Profile, profile_from_draft

_PROFILE: TypeAdapter[AnyProfile] = TypeAdapter(Profile)


class _Unreadable(ValueError):
    """profiles.json is not a profile file at all (not JSON, or not the expected shape)."""


@dataclass
class _Entry:
    """One stored profile. `raw` is the JSON value as found on disk: it is written back as it was, with the known
    fields of `profile` laid over it, so fields from a newer build survive. `profile` is None when this build cannot
    use the entry; it is then kept untouched."""

    raw: Any
    profile: AnyProfile | None


def _overlay(raw: dict[str, Any], model: BaseModel) -> dict[str, Any]:
    """`model`'s fields written over `raw`, keeping the fields this build does not know (nested models too)."""
    merged = dict(raw)
    for name, value in model.model_dump(mode="json").items():
        nested = getattr(model, name)
        if isinstance(nested, BaseModel):
            base = raw.get(name)
            merged[name] = _overlay(base if isinstance(base, dict) else {}, nested)
        else:
            merged[name] = value  # plain values and free-form dicts (options) are replaced as a whole
    return merged


def _error_fields(exc: ValidationError) -> str:
    """Where validation failed, as field paths only: never the values (they could be anything, e.g. a secret)."""
    return ", ".join(sorted({".".join(str(part) for part in err["loc"]) or "<entry>" for err in exc.errors()}))


class ProfileStore:
    """Connection profiles persisted as JSON. Never contains secrets."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._reported: set[tuple[int, str]] = set()  # invalid entries already logged, as (index, id)

    def list(self) -> list[AnyProfile]:
        with self._lock:
            profiles = [e.profile for e in self._load() if e.profile is not None]
        return sorted(profiles, key=lambda p: (p.folder.lower(), p.name.lower()))

    def get(self, profile_id: UUID) -> AnyProfile:
        with self._lock:
            return self._find(self._load(), profile_id).profile

    def create(self, draft: AnyDraft) -> AnyProfile:
        with self._lock:
            entries = self._load()
            profile = profile_from_draft(draft, uuid4())
            entries.append(_Entry(raw={}, profile=profile))
            self._save(entries)
        logger.info("Created profile '{}' ({})", profile.name, profile.engine)
        return profile

    def update(self, profile_id: UUID, draft: AnyDraft) -> AnyProfile:
        with self._lock:
            entries = self._load()
            entry = self._find(entries, profile_id)
            profile = profile_from_draft(draft, profile_id)
            if entry.profile.engine != profile.engine:
                entry.raw = {}  # unknown fields belonged to the old engine's shape
            entry.profile = profile
            self._save(entries)
        logger.info("Updated profile '{}'", profile.name)
        return profile

    def delete(self, profile_id: UUID) -> None:
        with self._lock:
            entries = self._load()
            entry = self._find(entries, profile_id)
            self._save([e for e in entries if e is not entry])
        logger.info("Deleted profile {}", profile_id)

    @staticmethod
    def _find(entries: list[_Entry], profile_id: UUID) -> _Entry:
        for entry in entries:
            if entry.profile is not None and entry.profile.id == profile_id:
                return entry
        raise ProfileNotFound(f"Profile {profile_id} not found")

    def _load(self) -> list[_Entry]:
        if not self._path.exists():
            return []
        try:
            raw_entries = self._read()
        except _Unreadable as exc:
            self._move_aside(str(exc))
            return []
        return [self._entry(index, raw) for index, raw in enumerate(raw_entries)]

    def _read(self) -> list[Any]:
        try:
            text = self._path.read_text("utf-8")
        except UnicodeDecodeError as exc:
            raise _Unreadable(f"not UTF-8: {exc.reason}") from exc
        except OSError as exc:
            logger.error("Cannot read {}: {}", self._path.name, exc)
            raise ProfileStoreError(f"Cannot read {self._path.name}: {exc.strerror or exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _Unreadable(f"not JSON: {exc}") from exc
        entries = data.get("profiles", []) if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise _Unreadable("no list of profiles")
        return entries

    def _entry(self, index: int, raw: Any) -> _Entry:
        try:
            return _Entry(raw, _PROFILE.validate_python(raw, extra="ignore"))  # a newer build may add fields
        except ValidationError as exc:
            profile_id = str(raw.get("id")) if isinstance(raw, dict) else "?"
            if (index, profile_id) not in self._reported:
                self._reported.add((index, profile_id))
                logger.warning(
                    "Skipping stored profile #{} (id {}): invalid {}; it is kept in the file as it is",
                    index, profile_id, _error_fields(exc),
                )
            return _Entry(raw, None)

    def _move_aside(self, reason: str) -> None:
        """Back the unreadable file up under a unique name, so that saving later never destroys it."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = next(
            candidate
            for counter in itertools.count(1)
            if not (candidate := self._path.with_name(f"{self._path.name}.corrupt-{stamp}-{counter}")).exists()
        )
        try:
            self._path.replace(backup)
        except OSError as exc:
            logger.error("{} is unreadable ({}) and could not be moved aside: {}", self._path.name, reason, exc)
            raise ProfileStoreError(
                f"{self._path.name} is unreadable and could not be moved aside: {exc.strerror or exc}"
            ) from exc
        logger.error("{} is unreadable ({}); moved it to {}", self._path.name, reason, backup.name)

    def _save(self, entries: list[_Entry]) -> None:
        stored = [e.raw if e.profile is None else _overlay(e.raw if isinstance(e.raw, dict) else {}, e.profile)
                  for e in entries]
        payload = {"version": self.VERSION, "profiles": stored}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.error("Cannot write {}: {}", self._path.name, exc)
            raise ProfileStoreError(f"Cannot save {self._path.name}: {exc.strerror or exc}") from exc
