"""The SQL a grid commit sends (spec ⑤ §3.3, D-05-3): one statement per staged row — every DELETE, then every UPDATE,
then every INSERT, each group in staging order — between the dialect's transaction frame. Values, keys and original
values go through `literals`, names through `dialects`. Pure: the preview builds it on the request thread, and the
worker later sends exactly these texts (R-05-7)."""

from dataclasses import dataclass

from ..domain.edits import (
    CellInput, EditChanges, EditColumn, EditCounts, EditProblem, EditProblemCode, EditTarget, EditWarning,
    PlannedStatement, RowDelete, RowInsert, RowUpdate, UncheckedColumn,
)
from .dialects import Dialect, insert_defaults, qualified, quote_ident, script_text, transaction_frame
from .literals import (
    LiteralStyle, UnmatchedReason, ValueProblem, cell_text, input_literal, match_condition, unmatched_reason,
)
from .select_builder import relation_from_path

_UNKNOWN_COLUMN = "No column {name} in this table (it may have been dropped or renamed)"
_READ_ONLY_COLUMN = "{name} cannot be written: {reason}"
_READ_ONLY_HERE = "read-only here"  # a non-writable column the adapter gave no reason
_NOT_NULLABLE = "{name} cannot be NULL"
_DEFAULT_ONLY_NEW = "DEFAULT applies only to a new row"
_KEY_MESSAGES: dict[UnmatchedReason | None, str] = {  # key_incomplete; None: a NULL key cell (ruling E4)
    "preview_only": "This row cannot be identified: its key column {name} was not loaded completely",
    "unsendable": "This row cannot be identified: its key column {name} holds a character that cannot be sent",
    None: "This row cannot be identified: its key column {name} is NULL",
}
_UNCHECKED_REASONS: dict[UnmatchedReason, str] = {  # ruling E22, pre-flight L6, ruling G2
    "preview_only": "only a preview of the value was loaded",
    "unsendable": "the loaded value holds a character that cannot be sent",
}
_NOT_EXACT = "{type} values cannot be compared exactly"
_NON_TRANSACTIONAL = (
    "This table's storage engine cannot roll back: if a statement fails, the statements before it stay applied"
)
_UNCHECKED = "Not checked for changes made since loading: {columns}"
_MISSING_REQUIRED = "New row: no value for {columns} (NOT NULL, no default); the server will probably refuse it"
_KEY_COLLISION = "This change moves a row onto the key of another changed row: commit these changes separately"


@dataclass(frozen=True)
class DmlPlan:
    table: str
    begin: str
    statements: list[PlannedStatement]
    commit: str
    problems: list[EditProblem]
    warnings: list[EditWarning]
    counts: EditCounts
    script: str


@dataclass(frozen=True, slots=True)
class _KeyChange:
    """An UPDATE's key as the WHERE terms that pin it: the row as loaded (`original`) and where the UPDATE leaves it
    (`new`: the SET term of each key column it sets, the original term of the others)."""

    change_id: str
    original: tuple[str, ...]
    new: tuple[str, ...]

    @property
    def moves(self) -> bool:
        return self.new != self.original


def _colliding(updates: list[_KeyChange]) -> list[str]:
    """The UPDATEs refused because one moves a row onto the key another UPDATE of the plan pins (ruling G7): after a
    concurrent delete of that row, the other UPDATE's WHERE would match the moved row, both would report one row and
    the commit would keep a wrong result. Each pair is reported once, on its later change when that one moves a row
    (a swap: the second), otherwise on the change that moves (the message speaks of a move). A move onto a DELETEd
    key is no collision: every DELETE runs first (D-05-3). Keys compare as the terms the statements send, so notations
    that differ (`2.0` for `2`) or a collation's equal texts are left to the server's unique check."""
    pinned: dict[tuple[str, ...], list[int]] = {}
    for index, update in enumerate(updates):
        pinned.setdefault(update.original, []).append(index)
    refused: set[int] = set()
    for index, update in enumerate(updates):
        if update.moves:
            for other in pinned.get(update.new, ()):  # never `index`: a move's new key is not its own original
                later = max(index, other)
                refused.add(later if updates[later].moves else index)
    return [updates[index].change_id for index in sorted(refused)]


class _Builder:
    def __init__(self, dialect: Dialect, target: EditTarget) -> None:
        if not target.editable:
            raise ValueError("this table cannot be edited")
        self.dialect = dialect
        self.target = target
        schema, name = relation_from_path(target.path)
        self.table = qualified(dialect, schema, name)
        self.columns = {column.name: column for column in target.columns}
        escapes = None if target.backslash_escapes else False
        self.styles = {c.name: LiteralStyle(national=c.unicode, backslash_escapes=escapes) for c in target.columns}
        self.problems: list[EditProblem] = []
        self.keys: list[_KeyChange] = []  # every UPDATE whose original and new key are both known, in plan order

    def _problem(self, change_id: str, column: str | None, code: EditProblemCode, message: str) -> None:
        self.problems.append(EditProblem(change_id=change_id, column=column, code=code, message=message))

    def _column(self, change_id: str, name: str) -> EditColumn | None:
        column = self.columns.get(name)
        if column is None:
            self._problem(change_id, name, "unknown_column", _UNKNOWN_COLUMN.format(name=name))
        return column

    def _key_terms(self, change_id: str, key: dict) -> list[str] | None:
        terms = []
        for name in self.target.key:
            cell = key.get(name)
            term = None if cell is None else match_condition(
                self.dialect, self.columns[name], cell, self.styles[name], key=True)
            if term is None:
                why = unmatched_reason(self.dialect, cell, self.styles[name]) if name in key else "preview_only"
                self._problem(change_id, name, "key_incomplete", _KEY_MESSAGES[why].format(name=name))
                return None
            terms.append(term)
        return terms

    def _row(self, key: dict) -> str:
        return ", ".join(f"{name} = {cell_text(key.get(name), self.columns[name].kind)}" for name in self.target.key)

    def _literal(self, change_id: str, column: EditColumn, cell: CellInput) -> str | None:
        """A new value's literal, or None after recording why it has none."""
        if not column.writable:
            self._problem(change_id, column.name, "read_only_column",
                          _READ_ONLY_COLUMN.format(name=column.name, reason=column.read_only_reason or _READ_ONLY_HERE))
            return None
        if cell.kind == "null" and not column.nullable:
            self._problem(change_id, column.name, "not_nullable", _NOT_NULLABLE.format(name=column.name))
            return None
        try:
            return input_literal(self.dialect, column, cell, self.styles[column.name])
        except ValueProblem as problem:
            self._problem(change_id, column.name, problem.code, problem.message)
            return None

    def delete(self, change: RowDelete) -> PlannedStatement | None:
        terms = self._key_terms(change.id, change.key)
        if terms is None:
            return None
        sql = f"DELETE FROM {self.table} WHERE {' AND '.join(terms)}"
        return PlannedStatement(change_id=change.id, kind="delete", sql=sql, row=self._row(change.key))

    def update(self, change: RowUpdate) -> PlannedStatement | None:
        terms = self._key_terms(change.id, change.key)
        complete = terms is not None
        sets: list[str] = []
        checks: list[str] = []
        unchecked: list[UncheckedColumn] = []
        new_key: dict[str, str] = {}  # each key column set: its SET term, which is also the term that pins it after
        for name, cell in change.set.items():
            column = self._column(change.id, name)
            if column is None:
                complete = False
                continue
            if cell.kind == "default":
                self._problem(change.id, name, "default_not_allowed", _DEFAULT_ONLY_NEW)
                complete = False
                continue
            literal = self._literal(change.id, column, cell)
            if literal is None:
                complete = False
                continue
            sets.append(f"{quote_ident(self.dialect, name)} = {literal}")
            if name in self.target.key:
                new_key[name] = sets[-1]
                continue  # its key term pins it (CR-4)
            original = change.original[name]
            term = match_condition(self.dialect, column, original, self.styles[name], key=False)
            if term is None:
                reason = (_NOT_EXACT.format(type=column.type_name) if column.compare == "none"
                          else _UNCHECKED_REASONS[unmatched_reason(self.dialect, original, self.styles[name])])
                unchecked.append(UncheckedColumn(column=name, reason=reason))
            else:
                checks.append(term)
        if terms is not None and new_key.keys() == {name for name in change.set if name in self.target.key}:
            new = tuple(new_key.get(name, term) for name, term in zip(self.target.key, terms, strict=True))
            self.keys.append(_KeyChange(change.id, tuple(terms), new))  # also with other problems: all are reported
        if not complete or terms is None:
            return None
        sql = f"UPDATE {self.table} SET {', '.join(sets)} WHERE {' AND '.join([*terms, *checks])}"
        return PlannedStatement(change_id=change.id, kind="update", sql=sql, unchecked=unchecked,
                                row=self._row(change.key))

    def refuse_key_collisions(self) -> None:
        """After every UPDATE: a problem on each one that `_colliding` refuses (ruling G7)."""
        for change_id in _colliding(self.keys):
            self._problem(change_id, None, "key_collision", _KEY_COLLISION)

    def insert(self, change: RowInsert) -> tuple[PlannedStatement | None, list[str]]:
        """The statement (None with problems) and the required columns the row gives no value."""
        names: list[str] = []
        literals: list[str] = []
        complete = True
        for name, cell in change.values.items():
            column = self._column(change.id, name)
            if column is None:
                complete = False
                continue
            if cell.kind == "default":
                continue
            literal = self._literal(change.id, column, cell)
            if literal is None:
                complete = False
                continue
            names.append(quote_ident(self.dialect, name))
            literals.append(literal)
        given = {name for name, cell in change.values.items() if cell.kind != "default"}
        missing = [c.name for c in self.target.columns
                   if c.writable and not c.nullable and not c.has_default and c.name not in given]
        if not complete:
            return None, missing
        if names:
            sql = f"INSERT INTO {self.table} ({', '.join(names)}) VALUES ({', '.join(literals)})"
        else:
            sql = insert_defaults(self.dialect, self.table)
        return PlannedStatement(change_id=change.id, kind="insert", sql=sql), missing


def _unchecked_warnings(statements: list[PlannedStatement]) -> list[EditWarning]:
    """One warning per statement that leaves a set column unchecked (spec §3.3.3 逐句, pre-flight L4); the reasons are
    in the statement's own `unchecked`."""
    warnings: list[EditWarning] = []
    for statement in statements:
        if columns := [u.column for u in statement.unchecked]:
            warnings.append(EditWarning(code="unchecked_columns", change_id=statement.change_id, columns=columns,
                                        message=_UNCHECKED.format(columns=", ".join(columns))))
    return warnings


def build_plan(dialect: Dialect, target: EditTarget, changes: EditChanges) -> DmlPlan:
    builder = _Builder(dialect, target)
    frame = transaction_frame(dialect)
    statements: list[PlannedStatement] = []
    missing_warnings: list[EditWarning] = []
    for delete in changes.deletes:
        if (statement := builder.delete(delete)) is not None:
            statements.append(statement)
    for update in changes.updates:
        if (statement := builder.update(update)) is not None:
            statements.append(statement)
    builder.refuse_key_collisions()
    for insert in changes.inserts:
        statement, missing = builder.insert(insert)
        if statement is not None:
            statements.append(statement)
        if missing:
            missing_warnings.append(EditWarning(code="missing_required", change_id=insert.id, columns=missing,
                                                message=_MISSING_REQUIRED.format(columns=", ".join(missing))))
    warnings: list[EditWarning] = []
    if not target.transactional:
        warnings.append(EditWarning(code="non_transactional", message=_NON_TRANSACTIONAL))
    warnings += _unchecked_warnings(statements)
    warnings += missing_warnings
    counts = EditCounts(updates=len(changes.updates), inserts=len(changes.inserts), deletes=len(changes.deletes))
    if builder.problems:
        return DmlPlan(builder.table, frame.begin, [], frame.commit, builder.problems, warnings, counts, "")
    script = script_text(dialect, [frame.begin, *(s.sql for s in statements), frame.commit])
    return DmlPlan(builder.table, frame.begin, statements, frame.commit, [], warnings, counts, script)
