from itertools import islice
from pathlib import Path
from typing import Any, Literal, NamedTuple

from loguru import logger
from pydantic import TypeAdapter, ValidationError

from ..domain.workspace import (
    MAX_PARAM_DRAFTS,
    ParamDraft,
    ParamName,
    TabQueryContext,
    WorkspaceState,
    WorkspaceTab,
)
from ..storage.json_files import JsonDocument, error_fields

# Each judged with the model's own definition of it, so what is kept is exactly what the model accepts
_NAME = TypeAdapter(ParamName)
_DRAFT = TypeAdapter(ParamDraft)
_CONTEXT = TypeAdapter(TabQueryContext | None)
_Extra = Literal["ignore"] | None  # as for `model_validate`: the file may come from a newer build, a request may not


class _Repair(NamedTuple):
    tab: Any
    drafts_removed: int = 0
    context_reset: bool = False


def _valid(adapter: TypeAdapter[Any], value: Any, extra: _Extra) -> bool:
    try:
        adapter.validate_python(value, extra=extra)
    except ValidationError:
        return False
    return True


def _repair_tab(tab: Any, extra: _Extra) -> _Repair:
    """The tab without what is wrong in its saved parameter drafts and query context, which are conveniences: a draft
    with a name or a size the model refuses is removed (and the drafts beyond the limit), and a context it refuses
    goes back to the profile's default. Everything else, the SQL buffer first, stays as it was."""
    if not isinstance(tab, dict):
        return _Repair(tab)
    repaired, removed, reset = tab, 0, False
    drafts = tab.get("params")
    if drafts is not None:
        valid = {} if not isinstance(drafts, dict) else {
            name: text for name, text in drafts.items() if _valid(_NAME, name, extra) and _valid(_DRAFT, text, extra)
        }
        kept = dict(islice(valid.items(), MAX_PARAM_DRAFTS))
        removed = (len(drafts) if isinstance(drafts, dict) else 1) - len(kept)  # drafts that are not a dict: one
        if removed:
            repaired = {**repaired, "params": kept or None}
    if tab.get("query_context") is not None and not _valid(_CONTEXT, tab["query_context"], extra):
        repaired, reset = {**repaired, "query_context": None}, True
    return _Repair(repaired, removed, reset)


def _repaired_tabs(raw: Any, where: str, extra: _Extra) -> Any:
    """`raw` (a workspace object) with every tab repaired (`_repair_tab`); each repair is one WARNING that counts what
    it took out and never names or shows it (a name or a draft is the user's text). Anything else is returned as is."""
    tabs = raw.get("tabs") if isinstance(raw, dict) else None
    if not isinstance(tabs, list):
        return raw
    repaired = []
    for index, tab in enumerate(tabs):
        result = _repair_tab(tab, extra)
        if result.drafts_removed or result.context_reset:
            logger.warning("Repaired tab {} of the {}: {}", index, where, " and ".join(
                part for part in (f"removed {result.drafts_removed} parameter draft(s)" if result.drafts_removed else "",
                                  "reset the query context" if result.context_reset else "") if part))
        repaired.append(result.tab)
    return {**raw, "tabs": repaired}


def repaired_workspace(raw: Any) -> Any:
    """For `PUT /workspace`, before validation: one bad draft name must not cost the whole save (the frontend already
    filters names, B40; this is the backstop)."""
    return _repaired_tabs(raw, "workspace being saved", None)


def _drop_invalid_tabs(raw: dict[str, Any]) -> dict[str, Any]:
    """Judge each tab on its own: a bad parameter draft or context is repaired (`_repair_tab`), and a tab that is still
    invalid (e.g. a kind from a newer build) is dropped with a warning, so the other tabs still load. A `tabs` that is
    not a list is left for the whole-file validation to reject."""
    raw = _repaired_tabs(raw, "saved workspace", "ignore")
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
