"""Manual field sampling (R-04-11): the field paths of a keyspace's documents, read on the profile's metadata connection
and kept per profile and keyspace until sampled again, the profile changes, or SAMPLES_PER_PROFILE_MAX newer samples of
other keyspaces push it out. Nothing samples by itself."""

import threading
from collections import OrderedDict
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, FieldSampling
from ..domain.couchbase import FieldSample, Keyspace
from ..domain.errors import ValidationFailed
from .metadata_errors import server_errors_are_final
from .metadata_service import MetadataService
from .settings_service import SettingsService

_CANNOT_SAMPLE = "This connection cannot sample documents"
# A sample holds up to FIELD_PATHS_MAX paths: without a bound, sampling through a cluster's collections (300 in the dev
# cluster's `many` scope alone) would keep every one of them for as long as the app runs.
SAMPLES_PER_PROFILE_MAX = 100


class FieldSampleService:
    def __init__(self, metadata: MetadataService, settings: SettingsService) -> None:
        self._metadata = metadata
        self._settings = settings
        # per profile, the least recently used keyspace first (a sample, or the cache answering for it, is a use)
        self._samples: dict[UUID, OrderedDict[Keyspace, FieldSample]] = {}
        self._lock = threading.Lock()

    def sample(self, profile_id: UUID, keyspace: Keyspace, size: int | None) -> FieldSample:
        """Read a sample of `keyspace` (`size` documents; None: the setting's `couchbase.sample_size`) and keep it as the
        keyspace's current one. A failed sample keeps the previous one. The server's error answer (a missing keyspace, a
        privilege) is raised as it is, and not sent a second time on a fresh connection (B39)."""
        requested = size if size is not None else self._settings.get().couchbase.sample_size

        def run(connection: DatabaseConnection) -> FieldSample:
            if not isinstance(connection, FieldSampling):
                raise ValidationFailed(_CANNOT_SAMPLE)
            with server_errors_are_final():
                return connection.sample_fields(keyspace, requested)

        sample = self._metadata.with_connection(profile_id, None, run)
        with self._lock:
            kept = self._samples.setdefault(profile_id, OrderedDict())
            kept[keyspace] = sample
            kept.move_to_end(keyspace)
            while len(kept) > SAMPLES_PER_PROFILE_MAX:
                kept.popitem(last=False)
        logger.info("Sampled {} documents of {}.{}.{} in {} ms ({} field paths)", sample.documents, keyspace.bucket,
                    keyspace.scope, keyspace.collection, sample.elapsed_ms, len(sample.fields))
        return sample

    def cached(self, profile_id: UUID, keyspace: Keyspace) -> FieldSample | None:
        with self._lock:
            kept = self._samples.get(profile_id)
            if kept is None or keyspace not in kept:
                return None
            kept.move_to_end(keyspace)
            return kept[keyspace]

    def invalidate(self, profile_id: UUID) -> None:
        """The profile changed or went away: its samples may describe another cluster."""
        with self._lock:
            self._samples.pop(profile_id, None)
