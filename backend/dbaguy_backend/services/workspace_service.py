from collections.abc import Callable
from itertools import islice
from pathlib import Path
from typing import Any, Literal, NamedTuple

from loguru import logger
from pydantic import TypeAdapter, ValidationError

from ..domain.edits import StagedEditsFile
from ..domain.well_formed import has_lone_surrogate, well_formed
from ..domain.workspace import (
    MAX_PARAM_DRAFTS,
    DocumentCas,
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
_CAS = TypeAdapter(DocumentCas | None)
_STAGED = TypeAdapter(StagedEditsFile | None)
_Extra = Literal["ignore"] | None  # as for `model_validate`: the file may come from a newer build, a request may not


class _Repair(NamedTuple):
    tab: Any
    drafts_removed: int = 0
    context_reset: bool = False
    cas_reset: bool = False
    staged_reset: bool = False
    texts_repaired: int = 0


def _well_formed_value(value: Any, depth: int = 0) -> tuple[Any, int]:
    """`value` with each lone surrogate in its strings replaced by U+FFFD, and how many strings held one. Lists and
    objects are read three levels down (the deepest text of a tab is a path segment's name); object names are not."""
    if isinstance(value, str):
        return (well_formed(value), 1) if has_lone_surrogate(value) else (value, 0)
    if depth < 3 and isinstance(value, list):
        items = [_well_formed_value(item, depth + 1) for item in value]
        count = sum(found for _, found in items)
        return ([item for item, _ in items], count) if count else (value, 0)
    if depth < 3 and isinstance(value, dict):
        fields = {name: _well_formed_value(item, depth + 1) for name, item in value.items()}
        count = sum(found for _, found in fields.values())
        return ({name: item for name, (item, _) in fields.items()}, count) if count else (value, 0)
    return value, 0


def _well_formed_fields(fields: dict[str, Any], skip: str) -> tuple[dict[str, Any], int]:
    """`fields` (a tab, or the workspace besides its tabs) with each lone surrogate in its text replaced by U+FFFD (as
    the frontend does when it writes the workspace), and how many strings held one. pydantic takes one in a plain `str`
    (an editor buffer, a title, the active tab's id), but `workspace.json` cannot be written with it: the whole save
    would fail, for every tab. The text is the user's own, so it is kept, not dropped. The field `skip` is left alone:
    a tab's `staged_edits` (changing a value there would change what a commit writes; its model refuses a lone
    surrogate instead, and the repair then drops the staged edits) and the workspace's `tabs` (each is repaired)."""
    fixed, count = {}, 0
    for name, value in fields.items():
        fixed[name], found = (value, 0) if name == skip else _well_formed_value(value)
        count += found
    return (fixed, count) if count else (fields, 0)


def _valid(adapter: TypeAdapter[Any], value: Any, extra: _Extra) -> bool:
    try:
        adapter.validate_python(value, extra=extra)
    except ValidationError:
        return False
    return True


def _repair_tab(tab: Any, extra: _Extra, texts_only: bool) -> _Repair:
    """The tab without what is wrong in its saved parameter drafts, query context, document CAS and staged grid edits:
    a draft with a name or a size the model refuses is removed (and the drafts beyond the limit), a context it refuses
    goes back to the profile's default, a CAS it refuses is forgotten (the document tab then reads as a new document),
    and staged edits it refuses (or on a tab that is not a table tab) are dropped, the tab kept (B40: one bad entry must
    not cost every tab its saved state). A lone surrogate in any text is replaced (`_well_formed_fields`). Everything else,
    the buffer first, stays as it was. `texts_only`: only the text is repaired (the model takes a lone surrogate; it
    refuses everything else this repairs, so a state it takes needs nothing else, `validated_workspace`)."""
    if not isinstance(tab, dict):
        return _Repair(tab)
    tab, texts_repaired = _well_formed_fields(tab, skip="staged_edits")
    if texts_only:
        return _Repair(tab, texts_repaired=texts_repaired)
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
    cas_reset = tab.get("document_cas") is not None and not _valid(_CAS, tab["document_cas"], extra)
    if cas_reset:
        repaired = {**repaired, "document_cas": None}
    staged = tab.get("staged_edits")
    staged_reset = staged is not None and (tab.get("kind") != "table" or not _valid(_STAGED, staged, extra))
    if staged_reset:
        repaired = {**repaired, "staged_edits": None}
    return _Repair(repaired, removed, reset, cas_reset, staged_reset, texts_repaired)


def _repaired_tabs(raw: Any, where: str, extra: _Extra, texts_only: bool = False) -> tuple[Any, list[str]]:
    """`raw` (a workspace object) with every tab repaired (`_repair_tab`), and the WARNING lines that say so: one per
    repair, counting what it took out and never naming or showing it (a name or a draft is the user's text). Anything
    else is returned as is."""
    if not isinstance(raw, dict):
        return raw, []
    raw, texts_repaired = _well_formed_fields(raw, skip="tabs")  # the active tab's id, the sidebar's tab
    warnings = [f"Repaired the {where}: replaced a lone surrogate in {texts_repaired} text(s)"] if texts_repaired else []
    tabs = raw.get("tabs")
    if not isinstance(tabs, list):
        return raw, warnings
    repaired = []
    for index, tab in enumerate(tabs):
        result = _repair_tab(tab, extra, texts_only)
        parts = (f"removed {result.drafts_removed} parameter draft(s)" if result.drafts_removed else "",
                 "reset the query context" if result.context_reset else "",
                 "reset the document CAS" if result.cas_reset else "",
                 "dropped the staged edits" if result.staged_reset else "",
                 f"replaced a lone surrogate in {result.texts_repaired} text(s)" if result.texts_repaired else "")
        if any(parts):
            warnings.append(f"Repaired tab {index} of the {where}: " + " and ".join(part for part in parts if part))
        repaired.append(result.tab)
    return {**raw, "tabs": repaired}, warnings


def _logged(repaired: tuple[Any, list[str]]) -> Any:
    state, warnings = repaired
    for warning in warnings:
        logger.warning("{}", warning)
    return state


_SAVED = "workspace being saved"


def repaired_workspace(raw: Any) -> Any:
    """For `PUT /workspace`, before validation: one bad draft name must not cost the whole save (the frontend already
    filters names, B40; this is the backstop)."""
    return _logged(_repaired_tabs(raw, _SAVED, None))


def validated_workspace(raw: Any, handler: Callable[[Any], WorkspaceState]) -> WorkspaceState:
    """`PUT /workspace`'s body (a wrap validator): validated once, and only when the model refuses it repaired
    (`repaired_workspace`) and validated again. The repair checks a staged set with the model's own definition, so
    repairing before every validation validated an 8 MiB set twice per save (0.3–0.4 s). A lone surrogate in a text is
    replaced first either way: the model takes one in a plain `str`, and `workspace.json` could not be written with it
    (K7). What is kept, and every WARNING, are as when every save was repaired first: the model refuses everything else
    the repair changes."""
    well_formed, warnings = _repaired_tabs(raw, _SAVED, None, texts_only=True)
    try:
        state = handler(well_formed)
    except ValidationError:
        return handler(repaired_workspace(raw))  # its WARNINGs include the text repairs
    return _logged((state, warnings))


def _drop_invalid_tabs(raw: dict[str, Any]) -> dict[str, Any]:
    """Judge each tab on its own: a bad parameter draft or context is repaired (`_repair_tab`), and a tab that is still
    invalid (e.g. a kind from a newer build) is dropped with a warning, so the other tabs still load. A `tabs` that is
    not a list is left for the whole-file validation to reject."""
    raw = _logged(_repaired_tabs(raw, "saved workspace", "ignore"))
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
