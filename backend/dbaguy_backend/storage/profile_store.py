from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from loguru import logger
from pydantic import TypeAdapter, ValidationError

from ..domain.errors import ProfileNotFound
from ..domain.profiles import AnyDraft, AnyProfile, Profile, profile_from_draft

_PROFILES: TypeAdapter[list[AnyProfile]] = TypeAdapter(list[Profile])


class ProfileStore:
    """Connection profiles persisted as JSON. Never contains secrets."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()

    def list(self) -> list[AnyProfile]:
        with self._lock:
            return sorted(self._load(), key=lambda p: (p.folder.lower(), p.name.lower()))

    def get(self, profile_id: UUID) -> AnyProfile:
        with self._lock:
            for profile in self._load():
                if profile.id == profile_id:
                    return profile
        raise ProfileNotFound(f"Profile {profile_id} not found")

    def create(self, draft: AnyDraft) -> AnyProfile:
        with self._lock:
            profiles = self._load()
            profile = profile_from_draft(draft, uuid4())
            profiles.append(profile)
            self._save(profiles)
        logger.info("Created profile '{}' ({})", profile.name, profile.engine)
        return profile

    def update(self, profile_id: UUID, draft: AnyDraft) -> AnyProfile:
        with self._lock:
            profiles = self._load()
            for index, existing in enumerate(profiles):
                if existing.id == profile_id:
                    profiles[index] = profile_from_draft(draft, profile_id)
                    self._save(profiles)
                    logger.info("Updated profile '{}'", profiles[index].name)
                    return profiles[index]
        raise ProfileNotFound(f"Profile {profile_id} not found")

    def delete(self, profile_id: UUID) -> None:
        with self._lock:
            profiles = self._load()
            remaining = [p for p in profiles if p.id != profile_id]
            if len(remaining) == len(profiles):
                raise ProfileNotFound(f"Profile {profile_id} not found")
            self._save(remaining)
        logger.info("Deleted profile {}", profile_id)

    def _load(self) -> list[AnyProfile]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            return _PROFILES.validate_python(raw.get("profiles", []))
        except (ValueError, ValidationError, AttributeError) as exc:
            backup = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
            self._path.replace(backup)
            logger.error("profiles.json is unreadable ({}); moved it to {}", exc, backup.name)
            return []

    def _save(self, profiles: list[AnyProfile]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.VERSION, "profiles": _PROFILES.dump_python(profiles, mode="json")}
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._path)
