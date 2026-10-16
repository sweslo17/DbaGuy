from pathlib import Path

from loguru import logger

from ..domain.workspace import WorkspaceState
from ..storage.json_files import JsonDocument


class WorkspaceService:
    """The open tabs in `workspace.json`. The frontend writes it whole every few seconds."""

    def __init__(self, path: Path) -> None:
        self._doc = JsonDocument(path, WorkspaceState)

    def get(self) -> WorkspaceState:
        return self._doc.get()

    def put(self, state: WorkspaceState) -> None:
        self._doc.save(state)
        logger.debug("Workspace saved ({} tabs)", len(state.tabs))
