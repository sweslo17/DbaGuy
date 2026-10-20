"""The header of a DbaGuy dump (spec ⑥ §3.3.2): written by the dump, read back by Run SQL file's inspection (one
format, defined here once). Pure.

    -- DbaGuy SQL dump · format 1
    -- Engine: postgres · PostgreSQL 17.11 · database dbaguy · schema public
    -- Created: 2026-10-20T12:34:56+08:00
    -- Content: schema and data · 12 tables, 2 views
    -- Snapshot: one consistent snapshot (REPEATABLE READ)
    -- Not included: owners and privileges · 1 extension's objects
    -- Options: drop objects first: no

and its last line, `-- Dump complete: 1,234 rows in 12 tables`: a dump that lacks it was cut short (pre-flight M15).
"""

import re

from ..domain.data_jobs import DumpHeader
from ..domain.profiles import Engine

FORMAT = 1
_FIRST = re.compile(r"-- DbaGuy SQL dump · format (\d+)")
_FIELD = re.compile(r"-- ([A-Z][A-Za-z ]*): (.*)")
_SEPARATOR = " · "
_DROP_FIRST = "drop objects first: "
# Every character Python's splitlines() breaks a line at (the header is read back with it), and NUL, which no statement
# may hold: in a comment, each is written as its escape (pre-flight S15)
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\x00"
_ESCAPED = str.maketrans({ch: ascii(ch)[1:-1] for ch in _LINE_BREAKS})  # "\n" → "\\n", "\u2028" → "\\u2028"


def comment_safe(text: str) -> str:
    """`text` as one line of a `--` comment: a name holding a line break would otherwise end the comment, and the rest
    of the name would run as SQL (pre-flight S15). `\\r`, `\\n` and the other line breaks are written as text."""
    return text.translate(_ESCAPED)


def header_lines(
    *, engine: Engine, server: str, scope: str, created: str, content: str, snapshot: str, not_included: list[str],
    drop_first: bool,
) -> list[str]:
    """The header's comment lines, without line breaks: each through comment_safe."""
    lines = [
        f"-- DbaGuy SQL dump · format {FORMAT}",
        f"-- Engine: {engine.value}{_SEPARATOR}{server}{_SEPARATOR}{scope}",
        f"-- Created: {created}",
        f"-- Content: {content}",
        f"-- Snapshot: {snapshot}",
        f"-- Not included: {_SEPARATOR.join(not_included)}",
        f"-- Options: {_DROP_FIRST}{'yes' if drop_first else 'no'}",
    ]
    return [comment_safe(line) for line in lines]


def parse_header(text: str) -> DumpHeader | None:
    """The header at the start of `text` (a file's first lines), or None when the file does not start with one."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or (first := _FIRST.fullmatch(lines[0].rstrip())) is None:
        return None
    fields: dict[str, str] = {}
    kept = [lines[0].rstrip()]
    for line in lines[1:]:
        found = _FIELD.fullmatch(line.rstrip())
        if found is None:
            break
        kept.append(line.rstrip())
        fields[found.group(1)] = found.group(2)
    engine, _, rest = fields.get("Engine", "").partition(_SEPARATOR)
    if engine not in {member.value for member in Engine}:
        return None
    server, _, scope = rest.partition(_SEPARATOR)
    return DumpHeader(
        format=int(first.group(1)), engine=Engine(engine), server=server or None, scope=scope or None,
        created=fields.get("Created"), drop_first=fields.get("Options", "").endswith(f"{_DROP_FIRST}yes"), lines=kept,
    )


# ---- the last line (pre-flight M15) ----

_COMPLETE = "-- Dump complete: "
END_BYTES = 4096  # how much of a file's end is read for its last line
CUT_SHORT = "The file does not end with its 'Dump complete' line: it was cut short"
_HEAD_LINES = 10  # the tools name themselves within a file's first lines
# A dump whose first lines name the tool that wrote it, and the start of the comment line that tool ends it with:
# DbaGuy's, pg_dump's (plain format) and pg_dumpall's, mysqldump's and MariaDB's (`-- Dump completed` with
# --skip-dump-date). A copy or a download that stopped at a statement boundary still runs; only this line tells.
_TRAILERS = (
    (re.compile(r"-- DbaGuy SQL dump · format \d+"), _COMPLETE),
    (re.compile(r"-- PostgreSQL database dump"), "-- PostgreSQL database dump complete"),
    (re.compile(r"-- PostgreSQL database cluster dump"), "-- PostgreSQL database cluster dump complete"),
    (re.compile(r"-- (?:MySQL|MariaDB) dump \d.*"), "-- Dump completed"),
)


def complete_line(rows: int, tables: int) -> str:
    """The last line of a DbaGuy dump (no line break)."""
    return f"{_COMPLETE}{rows:,} rows in {tables:,} tables"


def cut_short(head: str, end: str) -> bool:
    """Whether a dump whose first lines (`head`) name the tool that wrote it lacks the line that tool ends a dump with
    in `end` (its last END_BYTES, as text). A file no tool's lines name is never cut short."""
    first = [line.rstrip() for line in head.lstrip("﻿").splitlines()[:_HEAD_LINES]]
    for opening, closing in _TRAILERS:
        if any(opening.fullmatch(line) for line in first):
            return not any(line.startswith(closing) for line in end.splitlines())
    return False
