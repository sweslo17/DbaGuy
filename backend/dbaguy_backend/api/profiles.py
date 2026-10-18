from uuid import UUID

from fastapi import APIRouter, Depends, Response

from ..container import AppContainer
from ..domain.profiles import (
    ConnectionTestRequest,
    ConnectionTestResult,
    ImportUrlRequest,
    ImportUrlResponse,
    ProfileEntry,
    ProfileSecretsInput,
    ProfileUpsertRequest,
)
from .deps import get_container, require_token

router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(require_token)])


def _forget_connections_and_schema(container: AppContainer, profile_id: UUID) -> None:
    """After a profile changed or went away: drop its metadata connections, cached schema snapshots and field samples."""
    container.metadata.invalidate(profile_id)
    container.schema_cache.invalidate(profile_id)
    container.field_samples.invalidate(profile_id)


@router.get("", response_model=list[ProfileEntry])
def list_profiles(container: AppContainer = Depends(get_container)) -> list[ProfileEntry]:
    return container.profiles.list_entries()


@router.post("", response_model=ProfileEntry, status_code=201)
def create_profile(body: ProfileUpsertRequest, container: AppContainer = Depends(get_container)) -> ProfileEntry:
    return container.profiles.create(body)


@router.put("/{profile_id}", response_model=ProfileEntry)
def update_profile(
    profile_id: UUID, body: ProfileUpsertRequest, container: AppContainer = Depends(get_container)
) -> ProfileEntry:
    entry = container.profiles.update(profile_id, body)
    _forget_connections_and_schema(container, profile_id)
    return entry


@router.delete("/{profile_id}", status_code=204)
def delete_profile(profile_id: UUID, container: AppContainer = Depends(get_container)) -> Response:
    container.profiles.get(profile_id)  # 404 before touching sessions
    container.sessions.close_for_profile(profile_id)
    _forget_connections_and_schema(container, profile_id)
    container.profiles.delete(profile_id)
    return Response(status_code=204)


@router.post("/{profile_id}/unlock", status_code=204)
def unlock_profile(
    profile_id: UUID, body: ProfileSecretsInput, container: AppContainer = Depends(get_container)
) -> Response:
    container.profiles.unlock(profile_id, body)
    container.schema_cache.invalidate(profile_id)  # a remembered "password_required" must not outlive the unlock
    return Response(status_code=204)


@router.post("/import-url", response_model=ImportUrlResponse)
def import_url(body: ImportUrlRequest, container: AppContainer = Depends(get_container)) -> ImportUrlResponse:
    parsed = container.registry.parse_url(body.url)
    return ImportUrlResponse(profile=parsed.profile, password=parsed.password)


@router.post("/test", response_model=ConnectionTestResult)
def test_connection(body: ConnectionTestRequest, container: AppContainer = Depends(get_container)) -> ConnectionTestResult:
    return container.connections.test(body)
