"""Manual field sampling (R-04-11): the field paths of a keyspace's documents, read on the profile's metadata connection
and kept per profile and keyspace until sampled again or the profile changes. Nothing samples by itself."""

import threading
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, FieldSampling
from ..domain.couchbase import FieldSample, Keyspace
from ..domain.errors import ValidationFailed
from .couchbase_service import server_errors_are_final
from .metadata_service import MetadataService
from .settings_service import SettingsService

_CANNOT_SAMPLE = "This connection cannot sample documents"


class FieldSampleService:
    def __init__(self, metadata: MetadataService, settings: SettingsService) -> None:
        self._metadata = metadata
        self._settings = settings
        self._samples: dict[tuple[UUID, Keyspace], FieldSample] = {}
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
            self._samples[(profile_id, keyspace)] = sample
        logger.info("Sampled {} documents of {}.{}.{} in {} ms ({} field paths)", sample.documents, keyspace.bucket,
                    keyspace.scope, keyspace.collection, sample.elapsed_ms, len(sample.fields))
        return sample

    def cached(self, profile_id: UUID, keyspace: Keyspace) -> FieldSample | None:
        with self._lock:
            return self._samples.get((profile_id, keyspace))

    def invalidate(self, profile_id: UUID) -> None:
        """The profile changed or went away: its samples may describe another cluster."""
        with self._lock:
            for key in [key for key in self._samples if key[0] == profile_id]:
                del self._samples[key]
