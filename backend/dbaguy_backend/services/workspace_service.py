from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from ..domain.workspace import WorkspaceState, WorkspaceTab
from ..storage.json_files import JsonDocument, error_fields


def _drop_invalid_tabs(raw: dict[str, Any]) -> dict[str, Any]:
    """Judge each tab on its own: an invalid one (e.g. a kind from a newer build) is dropped with a warning, and the
    other tabs still load. A `tabs` that is not a list is left for the whole-file validation to reject."""
    tabs = raw.get("tabs")
    if not isinstance(tabs, list):
        return raw
    kept = []
    for index, tab in enumerate(tabs):
        try:
            WorkspaceTab.model_validate(tab, extra="ignore")
        except ValidationError as exc:  # field paths only: a tab holds the user's SQL
            logger.warning("Dropped tab {} of the saved workspace: invalid {}", index, error_fields(exc))
            continue
        kept.append(tab)
    return {**raw, "tabs": kept}


class WorkspaceService:
    """The open tabs in `workspace.json`. The frontend writes it whole every few seconds."""

    def __init__(self, path: Path) -> None:
        self._doc = JsonDocument(path, WorkspaceState, prune=_drop_invalid_tabs)

    def get(self) -> WorkspaceState:
        return self._doc.get()

    def put(self, state: WorkspaceState) -> None:
        self._doc.save(state)
        logger.debug("Workspace saved ({} tabs)", len(state.tabs))
