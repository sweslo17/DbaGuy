# Sub-project ②a Query Workflow — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the backend half of the query workflow: dialect-aware SQL intelligence (splitting, classification, completion, describe, copy/export formatting, table-browse SELECT), schema snapshots with a cache, a safety policy, transaction control, execution-protocol extensions (confirmation, timeout, history, session state, export), and local stores for history, workspace, settings and scripts — all exposed over HTTP/WS for the frontend plans ②b and ②c.

**Architecture:** A new pure `sql/` package (no DB access) holds all SQL text logic; adapters gain `dialect`, `schema_snapshot()` and transaction methods; `SessionWorker` gains a pluggable `SafetyPolicy`, per-request timeout, transaction commands, history/DDL callbacks and an export path; new services persist history (SQLite FTS5), workspace/settings (JSON) and scripts (files under a root). All HTTP text offsets are UTF-16 code units.

**Tech Stack:** Python 3.13, FastAPI, pydantic v2, loguru, psycopg 3, sqlite3 (FTS5), sqlglot ≥ 30.23.

**Spec:** `docs/superpowers/specs/2026-10-16-02-query-workflow-design.md` (§3, §4). Foundation context: `CLAUDE.md`, `docs/superpowers/specs/2026-10-15-01-foundation-design.md`.

## Global Constraints

- Everything from sub-project ①'s constraints still holds (see `CLAUDE.md`): Python `>=3.13,<3.14`, poetry in-project venv, backend on 127.0.0.1 with bearer token, stdout reserved for the ready line, passwords only in Keychain, user SQL executed without parameters, layered loguru logging, atomic file writes, tests with pytest only.
- New dependency: `sqlglot>=30.23,<31` (main group).
- **All text offsets in HTTP/WS APIs are UTF-16 code units.** Convert only in `sql/offsets.py`; internal code uses Python code-point offsets.
- `sql/` modules are pure: no DB access, no I/O, no logging of SQL text above DEBUG.
- Dialects in this sub-project: `postgres`, `sqlite`. Every dialect table is keyed by `Dialect` so ③④ can add `mysql`, `tsql`, `sqlpp` without touching callers.
- New files under the data dir: `history.db`, `workspace.json`, `settings.json`, `scripts/` (default scripts root). Writes are atomic (temp file + `os.replace`); SQLite uses WAL.
- Domain models that are persisted tolerate unknown fields on read and preserve them on write (same rule as ① I-1); API input models keep `extra="forbid"`.
- After any domain-model change run `npm run gen:api` and commit the regenerated `src/lib/api/openapi.json` / `schema.d.ts`; `npm run typecheck` must pass (frontend code is not changed in this plan except generated files).
- Commit after every task; message ends with a blank line and `Co-Authored-By: <your model> <noreply@anthropic.com>`. Branch `feat/02-query-workflow`.
- Bastion tests can fail when the suite is re-run within ~1 minute (sshd PerSourcePenalties); wait or `docker compose -f dev/docker-compose.yml restart bastion`.

## Review Focus

1. **Astral characters (emoji) before the cursor or an error** must not shift offsets → tests in Task 1 (offsets) and Task 14 (API round trip, error columns).
2. **Semicolons inside PG function bodies** (`$$`, `$fn$`, `BEGIN ATOMIC … END` with `CASE … END` inside) and SQLite trigger bodies must not split → tests in Task 2.
3. **Half-typed SQL** (`SELECT u.| FROM users u`, unterminated string, trailing `JOIN`) must still complete sensibly and never raise → tests in Task 6.
4. **A confirmed-then-replayed dangerous statement** must not bypass the policy: the backend re-checks every execute → test in Task 10.
5. **Scripts path traversal** (`../`, absolute paths, symlink pointing outside the root) is rejected → tests in Task 12.

## File Structure

```
backend/dbaguy_backend/
  sql/
    __init__.py
    dialects.py        Dialect, LexRules, RULES, quote_ident, qualified, quote_literal, needs_quote, SQLGLOT_NAME
    offsets.py         Utf16Index (code point <-> UTF-16)
    lexer.py           TokenKind, Token, tokenize()
    splitter.py        StatementSpan, split()
    classifier.py      StatementKind, Classification, classify()
    completion.py      CompletionItem, CompletionResult, complete(), describe_at(), SnapshotIndex
    formatting.py      FormatKind, RowFormatOptions, CellValue normalisation, format_rows(), open_writer()
    select_builder.py  build_browse_select(), relation_from_path()
  domain/
    schema.py          SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema, SchemaSnapshot, ObjectSearchHit
    formats.py         FormatKind, FormatColumn, RowFormatOptions (shared by /sql/format-rows and the WS export)
    settings.py        AppSettings (editor/run/safety/scripts/notifications)
    workspace.py       WorkspaceState, WorkspaceTab, TextSelection
    scripts.py         ScriptNode, ScriptTree, ScriptFile, ScriptWrite, ScriptRename, ScriptFolder
    history.py         HistoryRecord, HistoryEntry, HistoryPage
    execution.py       (extended) new commands/events
    profiles.py        (extended) `safety` field; EngineInfo.dialect
  adapters/
    base.py            (extended) dialect, schema_snapshot, autocommit/set_autocommit/commit/rollback, relation_path
    postgres/schema.py PG snapshot queries
    sqlite/schema.py   SQLite snapshot queries
    sql_text.py        (kept as thin re-exports of sql/dialects.py)
  services/
    schema_cache.py    SchemaCache
    safety.py          SafetyPolicy, GuardDecision
    history_service.py HistoryService
    workspace_service.py, settings_service.py, scripts_service.py
  storage/
    json_files.py      overlay, move_aside, atomic writes, JsonDocument (shared with ProfileStore)
    paths.py           (extended) settings/workspace/history files, default scripts folder
  sessions/worker.py   (extended: WorkerHooks, ExecutionRecord, policy, timeout, transactions, state, export)
  sessions/hooks.py    SessionHooksFactory (policy + history + schema cache per session)
  sessions/manager.py  (extended: hooks, state replay on attach, idle-transaction checks)
  api/
    sql.py, history.py, workspace.py, settings.py, scripts.py
    metadata.py        (extended: /metadata/search, /metadata/schema/refresh)
```

---

### Task 1: Dialects, UTF-16 offsets and the lexer

**Files:**
- Create: `backend/dbaguy_backend/sql/__init__.py`, `sql/dialects.py`, `sql/offsets.py`, `sql/lexer.py`
- Modify: `backend/dbaguy_backend/adapters/sql_text.py` (delegate to `sql/dialects.py`; keep its public names so existing imports work)
- Test: `backend/tests/sql/__init__.py`, `backend/tests/sql/test_dialects_offsets.py`, `backend/tests/sql/test_lexer.py`

**Interfaces:**
- Produces: `Dialect` (StrEnum `postgres`, `sqlite`); `LexRules(nested_block_comments, dollar_quotes, escape_string_prefixes, identifier_quotes, block_bodies)`; `RULES: dict[Dialect, LexRules]`; `SQLGLOT_NAME: dict[Dialect, str]`; `needs_quote(dialect, name) -> bool`; `quote_ident(dialect, name) -> str`; `qualified(dialect, schema | None, name) -> str`; `quote_literal(value) -> str`; `Utf16Index(text)` with `to_utf16(cp) -> int`, `from_utf16(u) -> int`; `TokenKind`; `Token(kind, start, end, text)` with `.upper`; `tokenize(text, dialect) -> list[Token]` (never raises; unterminated constructs extend to end of text); `first_keyword(text, dialect) -> str` (upper-cased first WORD token or "").

- [ ] **Step 1: Write the failing tests**

`backend/tests/sql/__init__.py`: empty.

`backend/tests/sql/test_dialects_offsets.py`:
```python
import pytest

from dbaguy_backend.sql.dialects import Dialect, needs_quote, qualified, quote_ident, quote_literal
from dbaguy_backend.sql.offsets import Utf16Index

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


@pytest.mark.parametrize(
    ("dialect", "name", "expected"),
    [
        (PG, "users", "users"),
        (PG, "Users", '"Users"'),
        (PG, "order", '"order"'),
        (PG, "Order Items", '"Order Items"'),
        (PG, 'a"b', '"a""b"'),
        (LITE, "Users", "Users"),  # SQLite identifiers are case-insensitive: no quotes needed
        (LITE, "order", '"order"'),
        (LITE, "my table", '"my table"'),
    ],
)
def test_quote_ident(dialect, name, expected):
    assert quote_ident(dialect, name) == expected
    assert needs_quote(dialect, name) == (expected != name)


def test_qualified_and_literal():
    assert qualified(PG, "sales", "orders") == "sales.orders"
    assert qualified(PG, None, "orders") == "orders"
    assert qualified(LITE, "main", "my table") == 'main."my table"'
    assert quote_literal("it's") == "'it''s'"


def test_utf16_round_trip_with_astral_characters():
    text = "a😀b中😀c"  # 😀 is outside the BMP (2 UTF-16 units)
    index = Utf16Index(text)
    assert [index.to_utf16(i) for i in range(len(text) + 1)] == [0, 1, 3, 4, 5, 7, 8]
    for cp in range(len(text) + 1):
        assert index.from_utf16(index.to_utf16(cp)) == cp
    assert index.from_utf16(2) == 1  # middle of a surrogate pair clamps to the character start
    assert index.from_utf16(99) == len(text)
```

`backend/tests/sql/test_lexer.py`:
```python
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.lexer import TokenKind as K
from dbaguy_backend.sql.lexer import first_keyword, tokenize

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


def kinds(text, dialect=PG):
    return [(t.kind, t.text) for t in tokenize(text, dialect) if t.kind != K.SPACE]


def test_basic_tokens():
    assert kinds("SELECT a.b, 1.5 FROM t;") == [
        (K.WORD, "SELECT"), (K.WORD, "a"), (K.DOT, "."), (K.WORD, "b"), (K.COMMA, ","),
        (K.NUMBER, "1.5"), (K.WORD, "FROM"), (K.WORD, "t"), (K.SEMI, ";"),
    ]


def test_strings_and_quoted_identifiers():
    assert kinds("'it''s; ok' \"we\"\"ird;\"") == [(K.STRING, "'it''s; ok'"), (K.QUOTED, '"we""ird;"')]
    assert kinds(r"E'a\'b;c'") == [(K.STRING, r"E'a\'b;c'")]
    assert kinds("[my;col] `x;y`", LITE) == [(K.QUOTED, "[my;col]"), (K.QUOTED, "`x;y`")]


def test_comments_nested_only_in_postgres():
    assert kinds("/* a /* b */ c */ x") == [(K.COMMENT, "/* a /* b */ c */"), (K.WORD, "x")]
    lite = kinds("/* a /* b */ c */ x", LITE)
    assert lite[0] == (K.COMMENT, "/* a /* b */")
    assert kinds("-- note; here\nx") == [(K.COMMENT, "-- note; here"), (K.WORD, "x")]


def test_dollar_quotes_and_params():
    body = "$fn$ BEGIN RETURN 1; END $fn$"
    assert kinds(f"AS {body};") == [(K.WORD, "AS"), (K.STRING, body), (K.SEMI, ";")]
    assert kinds("$$a;b$$") == [(K.STRING, "$$a;b$$")]
    assert kinds("WHERE id = $1") == [(K.WORD, "WHERE"), (K.WORD, "id"), (K.OTHER, "="), (K.PARAM, "$1")]
    assert kinds("foo$bar") == [(K.WORD, "foo$bar")]


def test_unterminated_constructs_do_not_raise():
    assert kinds("SELECT 'abc")[-1] == (K.STRING, "'abc")
    assert kinds("SELECT /* never closed")[-1][0] == K.COMMENT
    assert kinds("$$ open")[-1] == (K.STRING, "$$ open")


def test_offsets_cover_the_text():
    text = "SELECT '中文' -- 😀\nFROM t"
    tokens = tokenize(text, PG)
    assert "".join(t.text for t in tokens) == text
    assert all(text[t.start:t.end] == t.text for t in tokens)


def test_first_keyword():
    assert first_keyword("  -- c\n/* x */ with x as (select 1) select * from x", PG) == "WITH"
    assert first_keyword("", PG) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/sql -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'dbaguy_backend.sql'`.

- [ ] **Step 3: Implement**

`backend/dbaguy_backend/sql/__init__.py`: empty.

`backend/dbaguy_backend/sql/dialects.py`:
```python
import re
from dataclasses import dataclass
from enum import StrEnum


class Dialect(StrEnum):
    POSTGRES = "postgres"
    SQLITE = "sqlite"


@dataclass(frozen=True)
class LexRules:
    nested_block_comments: bool
    dollar_quotes: bool
    escape_string_prefixes: frozenset[str]  # e.g. E'...' in PostgreSQL
    identifier_quotes: tuple[tuple[str, str], ...]  # (open, close)
    block_bodies: frozenset[str]  # "begin_atomic" (PG SQL-standard bodies), "trigger" (SQLite trigger bodies)


RULES: dict[Dialect, LexRules] = {
    Dialect.POSTGRES: LexRules(
        nested_block_comments=True, dollar_quotes=True, escape_string_prefixes=frozenset({"e", "E"}),
        identifier_quotes=(('"', '"'),), block_bodies=frozenset({"begin_atomic"}),
    ),
    Dialect.SQLITE: LexRules(
        nested_block_comments=False, dollar_quotes=False, escape_string_prefixes=frozenset(),
        identifier_quotes=(('"', '"'), ("[", "]"), ("`", "`")), block_bodies=frozenset({"trigger"}),
    ),
}

SQLGLOT_NAME: dict[Dialect, str] = {Dialect.POSTGRES: "postgres", Dialect.SQLITE: "sqlite"}

PG_RESERVED = frozenset(
    """all analyse analyze and any array as asc asymmetric authorization binary both case cast check
    collate collation column concurrently constraint create cross current_catalog current_date current_role
    current_schema current_time current_timestamp current_user default deferrable desc distinct do else end
    except false fetch for foreign freeze from full grant group having ilike in initially inner intersect into
    is isnull join lateral leading left like limit localtime localtimestamp natural not notnull null offset on
    only or order outer overlaps placing primary references returning right select session_user similar some
    symmetric system_user table tablesample then to trailing true union unique user using variadic verbose
    when where window with""".split()
)

SQLITE_RESERVED = frozenset(
    """abort action add after all alter always analyze and as asc attach autoincrement before begin between by
    cascade case cast check collate column commit conflict constraint create cross current current_date
    current_time current_timestamp database default deferrable deferred delete desc detach distinct do drop each
    else end escape except exclude exclusive exists explain fail filter first following for foreign from full
    generated glob group groups having if ignore immediate in index indexed initially inner insert instead
    intersect into is isnull join key last left like limit match materialized natural no not nothing notnull
    null nulls of offset on or order others outer over partition plan pragma preceding primary query raise range
    recursive references regexp reindex release rename replace restrict returning right rollback row rows
    savepoint select set table temp temporary then ties to transaction trigger unbounded union unique update
    using vacuum values view virtual when where window with without""".split()
)

_SIMPLE_IDENT = {
    Dialect.POSTGRES: re.compile(r"[a-z_][a-z0-9_$]*"),  # unquoted PG identifiers fold to lower case
    Dialect.SQLITE: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),  # SQLite identifiers are case-insensitive
}
_RESERVED = {Dialect.POSTGRES: PG_RESERVED, Dialect.SQLITE: SQLITE_RESERVED}


def needs_quote(dialect: Dialect, name: str) -> bool:
    return not _SIMPLE_IDENT[dialect].fullmatch(name) or name.lower() in _RESERVED[dialect]


def quote_ident(dialect: Dialect, name: str) -> str:
    if not needs_quote(dialect, name):
        return name
    return '"' + name.replace('"', '""') + '"'


def qualified(dialect: Dialect, schema: str | None, name: str) -> str:
    return f"{quote_ident(dialect, schema)}.{quote_ident(dialect, name)}" if schema else quote_ident(dialect, name)


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
```

Replace the bodies in `backend/dbaguy_backend/adapters/sql_text.py` so there is one implementation (keep `strip_leading_comments` and `first_keyword` there as they are used by adapters):
```python
from ..sql.dialects import PG_RESERVED, Dialect, qualified, quote_ident, quote_literal  # noqa: F401  (re-exported)


def quote_ident_pg(name: str) -> str:
    return quote_ident(Dialect.POSTGRES, name)


def qualified_pg(schema: str, name: str) -> str:
    return qualified(Dialect.POSTGRES, schema, name)


def quote_ident_sqlite(name: str) -> str:
    """Always quote: introspection SQL interpolates arbitrary names."""
    return '"' + name.replace('"', '""') + '"'
```
(Keep the existing `_LEADING_JUNK`, `strip_leading_comments` and `first_keyword` code in that module unchanged.) Note `quote_ident_sqlite` keeps always-quoting semantics because the SQLite introspection SQL relies on it.

`backend/dbaguy_backend/sql/offsets.py`:
```python
from bisect import bisect_left


class Utf16Index:
    """Maps code-point offsets of a string to UTF-16 code-unit offsets (JavaScript) and back."""

    def __init__(self, text: str) -> None:
        self._length = len(text)
        self._astral = [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF]

    def to_utf16(self, cp: int) -> int:
        cp = max(0, min(cp, self._length))
        return cp + bisect_left(self._astral, cp)

    def from_utf16(self, unit: int) -> int:
        """Largest code-point offset whose UTF-16 offset is <= unit (clamps inside surrogate pairs)."""
        lo, hi = 0, self._length
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.to_utf16(mid) <= unit:
                lo = mid
            else:
                hi = mid - 1
        return lo
```

`backend/dbaguy_backend/sql/lexer.py`:
```python
import re
from dataclasses import dataclass
from enum import StrEnum

from .dialects import RULES, Dialect


class TokenKind(StrEnum):
    WORD = "word"
    QUOTED = "quoted"
    STRING = "string"
    NUMBER = "number"
    COMMENT = "comment"
    SPACE = "space"
    SEMI = "semi"
    DOT = "dot"
    COMMA = "comma"
    LPAREN = "lparen"
    RPAREN = "rparen"
    PARAM = "param"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    start: int
    end: int
    text: str

    @property
    def upper(self) -> str:
        return self.text.upper()


_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
_DOLLAR_PARAM = re.compile(r"\$\d+")
_NUMBER = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_PUNCT = {";": TokenKind.SEMI, ".": TokenKind.DOT, ",": TokenKind.COMMA, "(": TokenKind.LPAREN, ")": TokenKind.RPAREN}


def _ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_" or ord(ch) > 127


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


def _scan_string(text: str, i: int, backslash: bool) -> int:
    """i points at the opening quote; returns the index after the closing quote (or len(text))."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def tokenize(text: str, dialect: Dialect) -> list[Token]:
    """Lossless tokenizer: the concatenated token texts equal the input. Never raises."""
    rules = RULES[dialect]
    quotes = dict(rules.identifier_quotes)
    tokens: list[Token] = []
    n = len(text)
    i = 0

    def add(kind: TokenKind, start: int, end: int) -> None:
        tokens.append(Token(kind, start, end, text[start:end]))

    while i < n:
        ch = text[i]
        start = i
        if ch.isspace():
            while i < n and text[i].isspace():
                i += 1
            add(TokenKind.SPACE, start, i)
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            add(TokenKind.COMMENT, start, i)
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                elif rules.nested_block_comments and text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                else:
                    i += 1
            add(TokenKind.COMMENT, start, i)
        elif ch == "'":
            i = _scan_string(text, i, backslash=False)
            add(TokenKind.STRING, start, i)
        elif ch in quotes:
            close = quotes[ch]
            i += 1
            while i < n:
                if text[i] == close:
                    if close != "]" and i + 1 < n and text[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            add(TokenKind.QUOTED, start, i)
        elif ch == "$" and rules.dollar_quotes and (param := _DOLLAR_PARAM.match(text, i)):
            i = param.end()
            add(TokenKind.PARAM, start, i)
        elif ch == "$" and rules.dollar_quotes and (tag := _DOLLAR_TAG.match(text, i)) and not (
            i > 0 and _ident_char(text[i - 1])
        ):
            close_at = text.find(tag.group(0), tag.end())
            i = n if close_at < 0 else close_at + len(tag.group(0))
            add(TokenKind.STRING, start, i)
        elif _ident_start(ch):
            while i < n and _ident_char(text[i]):
                i += 1
            if text[start:i] in rules.escape_string_prefixes and i < n and text[i] == "'":
                i = _scan_string(text, i, backslash=True)
                add(TokenKind.STRING, start, i)
            else:
                add(TokenKind.WORD, start, i)
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            match = _NUMBER.match(text, i)
            i = match.end() if match else i + 1
            add(TokenKind.NUMBER, start, i)
        else:
            i += 1
            add(_PUNCT.get(ch, TokenKind.OTHER), start, i)
    return tokens


def first_keyword(text: str, dialect: Dialect) -> str:
    for token in tokenize(text, dialect):
        if token.kind == TokenKind.WORD:
            return token.upper
        if token.kind not in (TokenKind.SPACE, TokenKind.COMMENT):
            return ""
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q && .venv/bin/pytest -q`
Expected: new tests pass; the existing suite still passes (sql_text delegation must not change PG/SQLite adapter behaviour).

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(sql): add dialect rules, UTF-16 offsets and a lossless SQL lexer

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 2: Statement splitter

**Files:**
- Create: `backend/dbaguy_backend/sql/splitter.py`
- Test: `backend/tests/sql/test_splitter.py`

**Interfaces:**
- Consumes: `tokenize`, `TokenKind`, `RULES` (Task 1).
- Produces: `StatementSpan(start, end, terminator_end, text)` (code-point offsets; `start` = first significant token, `end` = end of last significant token, `terminator_end` = end of the `;` or `end` when unterminated); `split(text, dialect) -> list[StatementSpan]` (drops statements that contain only whitespace/comments).

Rules (spec §3.4): `;` ends a statement except inside strings, quoted identifiers, comments, dollar quotes, a PG `BEGIN ATOMIC … END` body, or a SQLite `CREATE [TEMP] TRIGGER … BEGIN … END` body; inside a body, `CASE … END` pairs are tracked so the body's own `END` is found. `BEGIN` / `BEGIN TRANSACTION` / `END` as statements do not open bodies.

- [ ] **Step 1: Write the failing tests**

`backend/tests/sql/test_splitter.py`:
```python
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.splitter import split

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


def texts(sql, dialect=PG):
    return [s.text for s in split(sql, dialect)]


def test_simple_and_leading_comments():
    sql = "-- first\nSELECT 1;\n\n/* two */ SELECT 2 ;  SELECT 3"
    spans = split(sql, PG)
    assert [s.text for s in spans] == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert sql[spans[0].start:spans[0].terminator_end] == "SELECT 1;"
    assert spans[2].terminator_end == spans[2].end == len(sql)


def test_empty_statements_and_comment_only_tail_are_dropped():
    assert texts(";;  ; -- only a comment\n") == []
    assert texts("SELECT 1; -- trailing note") == ["SELECT 1"]


def test_semicolons_inside_literals_identifiers_comments():
    sql = "SELECT 'a;b', \"c;d\" /* e;f */ FROM t; SELECT E'g\\';h'"
    assert texts(sql) == ["SELECT 'a;b', \"c;d\" /* e;f */ FROM t", "SELECT E'g\\';h'"]


def test_postgres_dollar_quoted_function_bodies():
    fn = "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$ BEGIN RETURN 1; END; $fn$"
    assert texts(f"{fn}; SELECT f();") == [fn, "SELECT f()"]
    assert texts("DO $$ BEGIN RAISE NOTICE 'x;y'; END $$; SELECT 1") == [
        "DO $$ BEGIN RAISE NOTICE 'x;y'; END $$", "SELECT 1",
    ]


def test_postgres_begin_atomic_with_case_inside():
    fn = (
        "CREATE FUNCTION g(x int) RETURNS int LANGUAGE sql BEGIN ATOMIC "
        "SELECT CASE WHEN x > 0 THEN 1 ELSE 0 END; SELECT 2; END"
    )
    assert texts(f"{fn}; SELECT g(1)") == [fn, "SELECT g(1)"]


def test_transaction_begin_and_end_do_not_open_bodies():
    assert texts("BEGIN; UPDATE t SET a = 1; END; BEGIN TRANSACTION; COMMIT") == [
        "BEGIN", "UPDATE t SET a = 1", "END", "BEGIN TRANSACTION", "COMMIT",
    ]


def test_sqlite_trigger_body():
    trg = "CREATE TEMP TRIGGER trg AFTER INSERT ON t BEGIN UPDATE c SET n = n + 1; SELECT CASE WHEN 1 THEN 2 END; END"
    assert texts(f"{trg}; SELECT 1;", LITE) == [trg, "SELECT 1"]
    assert texts("BEGIN; SELECT 1; COMMIT", LITE) == ["BEGIN", "SELECT 1", "COMMIT"]


def test_nested_comments_only_in_postgres():
    assert texts("/* a /* ; */ still comment */ SELECT 1") == ["SELECT 1"]
    # In SQLite the first */ closes the comment, so "still comment */ SELECT 1" is one (bogus) statement.
    assert texts("/* a /* ; */ x */ SELECT 1", LITE) == ["x */ SELECT 1"]


def test_offsets_with_cjk_and_emoji():
    sql = "SELECT '中😀';\nSELECT 2"
    spans = split(sql, PG)
    assert [sql[s.start:s.end] for s in spans] == ["SELECT '中😀'", "SELECT 2"]


def test_unterminated_string_swallows_rest():
    assert texts("SELECT 'abc; SELECT 2") == ["SELECT 'abc; SELECT 2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_splitter.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.sql.splitter'`).

- [ ] **Step 3: Implement**

`backend/dbaguy_backend/sql/splitter.py`:
```python
from dataclasses import dataclass

from .dialects import RULES, Dialect
from .lexer import Token, TokenKind, tokenize

_INSIGNIFICANT = (TokenKind.SPACE, TokenKind.COMMENT)


@dataclass(frozen=True, slots=True)
class StatementSpan:
    start: int
    end: int
    terminator_end: int
    text: str


class _BodyTracker:
    """Tracks routine/trigger bodies whose inner semicolons must not split the statement."""

    def __init__(self, kinds: frozenset[str]) -> None:
        self._kinds = kinds
        self.reset()

    def reset(self) -> None:
        self.depth = 0
        self._case_depth = 0
        self._first_word = ""
        self._prev_word = ""
        self._is_trigger = False

    def feed(self, token: Token) -> None:
        if token.kind != TokenKind.WORD:
            return
        word = token.upper
        if self.depth == 0:
            if not self._first_word:
                self._first_word = word
            if word == "TRIGGER" and self._first_word == "CREATE":
                self._is_trigger = True
            if word == "BEGIN" and "trigger" in self._kinds and self._is_trigger:
                self.depth = 1
            elif word == "ATOMIC" and self._prev_word == "BEGIN" and "begin_atomic" in self._kinds:
                self.depth = 1
        elif word == "CASE":
            self._case_depth += 1
        elif word == "END":
            if self._case_depth:
                self._case_depth -= 1
            else:
                self.depth -= 1
        self._prev_word = word


def _emit(spans: list[StatementSpan], text: str, tokens: list[Token], terminator_end: int | None) -> None:
    significant = [t for t in tokens if t.kind not in _INSIGNIFICANT]
    if not significant:
        return
    start, end = significant[0].start, significant[-1].end
    spans.append(StatementSpan(start, end, terminator_end if terminator_end is not None else end, text[start:end]))


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    tracker = _BodyTracker(RULES[dialect].block_bodies)
    spans: list[StatementSpan] = []
    current: list[Token] = []
    for token in tokenize(text, dialect):
        if token.kind == TokenKind.SEMI and tracker.depth == 0:
            _emit(spans, text, current, token.end)
            current = []
            tracker.reset()
            continue
        current.append(token)
        tracker.feed(token)
    _emit(spans, text, current, None)
    return spans
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(sql): split scripts into statements (dollar quotes, BEGIN ATOMIC, trigger bodies)

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 3: Statement classifier

**Files:**
- Modify: `backend/pyproject.toml` (add `sqlglot>=30.23,<31`), `backend/poetry.lock`
- Create: `backend/dbaguy_backend/sql/classifier.py`
- Test: `backend/tests/sql/test_classifier.py`

**Interfaces:**
- Consumes: `Dialect`, `SQLGLOT_NAME`, `tokenize`, `first_keyword` (Task 1).
- Produces: `StatementKind` (StrEnum `read, write, ddl, transaction, session, unknown`); `Classification(kind, dangerous_reasons: list[str])` (pydantic model, reasons from `update_without_where, delete_without_where, drop, truncate, alter_drop`); `classify(sql, dialect) -> Classification` (one statement; never raises).

Rules: parse with sqlglot (`read=SQLGLOT_NAME[dialect]`). `exp.Command` results or parse errors fall back to a first-keyword table. `EXPLAIN ANALYZE <stmt>` (also `EXPLAIN (ANALYZE …)`) takes the inner statement's kind if it is not read; plain EXPLAIN is read. A `SELECT` containing a data-modifying CTE or a locking clause (`FOR UPDATE/SHARE`) is `write`. SQLite `PRAGMA x = y` is `session`, `PRAGMA x` is `read`.

- [ ] **Step 1: Add the dependency**

Run: `cd backend && poetry add "sqlglot>=30.23,<31"`
Expected: installs sqlglot 30.x; `poetry.lock` updated.

- [ ] **Step 2: Write the failing tests**

`backend/tests/sql/test_classifier.py`:
```python
import pytest

from dbaguy_backend.sql.classifier import StatementKind as K
from dbaguy_backend.sql.classifier import classify
from dbaguy_backend.sql.dialects import Dialect

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


@pytest.mark.parametrize(
    ("sql", "kind", "reasons"),
    [
        ("SELECT * FROM t WHERE a = 1", K.READ, []),
        ("WITH x AS (SELECT 1) SELECT * FROM x", K.READ, []),
        ("VALUES (1), (2)", K.READ, []),
        ("SHOW search_path", K.READ, []),
        ("EXPLAIN SELECT 1", K.READ, []),
        ("EXPLAIN ANALYZE DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("SELECT * FROM t FOR UPDATE", K.WRITE, []),
        ("WITH d AS (DELETE FROM t WHERE id = 1 RETURNING *) SELECT * FROM d", K.WRITE, []),
        ("INSERT INTO t VALUES (1)", K.WRITE, []),
        ("UPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
        ("UPDATE t SET a = 1 WHERE id = 2", K.WRITE, []),
        ("DELETE FROM t", K.WRITE, ["delete_without_where"]),
        ("DELETE FROM t WHERE id = 1", K.WRITE, []),
        ("DROP TABLE t", K.DDL, ["drop"]),
        ("TRUNCATE t", K.DDL, ["truncate"]),
        ("ALTER TABLE t DROP COLUMN a", K.DDL, ["alter_drop"]),
        ("ALTER TABLE t ADD COLUMN a int", K.DDL, []),
        ("CREATE INDEX i ON t (a)", K.DDL, []),
        ("CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql", K.DDL, []),
        ("VACUUM t", K.DDL, []),
        ("BEGIN", K.TRANSACTION, []),
        ("COMMIT", K.TRANSACTION, []),
        ("ROLLBACK", K.TRANSACTION, []),
        ("SET search_path TO public", K.SESSION, []),
        ("DO $$ BEGIN PERFORM 1; END $$", K.WRITE, []),
        ("COPY t FROM STDIN", K.WRITE, []),
        ("frobnicate the widgets", K.UNKNOWN, []),
    ],
)
def test_postgres(sql, kind, reasons):
    result = classify(sql, PG)
    assert result.kind == kind
    assert result.dangerous_reasons == reasons


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("PRAGMA table_info(t)", K.READ),
        ("PRAGMA journal_mode = WAL", K.SESSION),
        ("ATTACH DATABASE 'x.db' AS x", K.SESSION),
        ("REPLACE INTO t VALUES (1)", K.WRITE),
        ("SELECT 1", K.READ),
    ],
)
def test_sqlite(sql, kind):
    assert classify(sql, LITE).kind == kind


def test_never_raises_on_garbage():
    for sql in ["", "   ", "SELECT (", "'", "-- only comment", "DELETE FROM"]:
        classify(sql, PG)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_classifier.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.sql.classifier'`).

- [ ] **Step 4: Implement**

`backend/dbaguy_backend/sql/classifier.py`:
```python
from enum import StrEnum

import sqlglot
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .dialects import SQLGLOT_NAME, Dialect
from .lexer import TokenKind, tokenize


class StatementKind(StrEnum):
    READ = "read"
    WRITE = "write"
    DDL = "ddl"
    TRANSACTION = "transaction"
    SESSION = "session"
    UNKNOWN = "unknown"


class Classification(BaseModel):
    kind: StatementKind
    dangerous_reasons: list[str] = []


_KEYWORD_KIND: dict[str, StatementKind] = {
    **dict.fromkeys(("SELECT", "WITH", "VALUES", "TABLE", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "FETCH"), StatementKind.READ),
    **dict.fromkeys(
        ("INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "REPLACE", "UPSERT", "CALL", "DO", "LOCK"), StatementKind.WRITE
    ),
    **dict.fromkeys(
        ("CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "GRANT", "REVOKE", "REINDEX", "VACUUM", "ANALYZE",
         "CLUSTER", "REFRESH", "SECURITY", "RENAME", "IMPORT"),
        StatementKind.DDL,
    ),
    **dict.fromkeys(
        ("BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "END", "ABORT"), StatementKind.TRANSACTION
    ),
    **dict.fromkeys(
        ("SET", "RESET", "USE", "ATTACH", "DETACH", "LISTEN", "UNLISTEN", "NOTIFY", "DISCARD", "DEALLOCATE"),
        StatementKind.SESSION,
    ),
}
_DML = (exp.Insert, exp.Update, exp.Delete, exp.Merge)


def _words(sql: str, dialect: Dialect) -> list[str]:
    return [t.upper for t in tokenize(sql, dialect) if t.kind == TokenKind.WORD]


def _lexical(sql: str, dialect: Dialect) -> Classification:
    words = _words(sql, dialect)
    if not words:
        return Classification(kind=StatementKind.UNKNOWN)
    first = words[0]
    if first == "PRAGMA":
        kind = StatementKind.SESSION if "=" in sql else StatementKind.READ
        return Classification(kind=kind)
    kind = _KEYWORD_KIND.get(first, StatementKind.UNKNOWN)
    if kind == StatementKind.READ and first in ("SELECT", "WITH"):
        # Data-modifying CTEs and locking reads are writes even when sqlglot could not parse them.
        has_dml = any(w in ("INSERT", "UPDATE", "DELETE", "MERGE") for w in words[1:])
        locks = any(a == "FOR" and b in ("UPDATE", "SHARE", "NO", "KEY") for a, b in zip(words, words[1:]))
        if has_dml or locks:
            kind = StatementKind.WRITE
    reasons: list[str] = []
    if first == "DROP":
        reasons.append("drop")
    elif first == "TRUNCATE":
        reasons.append("truncate")
    elif first in ("UPDATE", "DELETE") and "WHERE" not in words:
        reasons.append("update_without_where" if first == "UPDATE" else "delete_without_where")
    return Classification(kind=kind, dangerous_reasons=reasons)


def _explain_inner(sql: str, dialect: Dialect) -> str | None:
    """For EXPLAIN ANALYZE ..., return the analysed statement text; None for plain EXPLAIN."""
    tokens = [t for t in tokenize(sql, dialect) if t.kind not in (TokenKind.SPACE, TokenKind.COMMENT)]
    if not tokens or tokens[0].upper != "EXPLAIN":
        return None
    i, analyze = 1, False
    if i < len(tokens) and tokens[i].kind == TokenKind.LPAREN:  # EXPLAIN (ANALYZE, BUFFERS) ...
        depth = 0
        while i < len(tokens):
            if tokens[i].kind == TokenKind.LPAREN:
                depth += 1
            elif tokens[i].kind == TokenKind.RPAREN:
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            elif tokens[i].upper == "ANALYZE":
                analyze = True
            i += 1
    while i < len(tokens) and tokens[i].upper in ("ANALYZE", "VERBOSE", "QUERY", "PLAN"):
        analyze = analyze or tokens[i].upper == "ANALYZE"
        i += 1
    if not analyze or i >= len(tokens):
        return None
    return sql[tokens[i].start:]


def _from_expression(node: exp.Expression) -> Classification | None:
    if isinstance(node, exp.Drop):
        return Classification(kind=StatementKind.DDL, dangerous_reasons=["drop"])
    if isinstance(node, exp.TruncateTable):
        return Classification(kind=StatementKind.DDL, dangerous_reasons=["truncate"])
    if isinstance(node, exp.Alter):
        drops = any(isinstance(child, exp.Drop) for child in node.walk() if child is not node)
        return Classification(kind=StatementKind.DDL, dangerous_reasons=["alter_drop"] if drops else [])
    if isinstance(node, exp.Create):
        return Classification(kind=StatementKind.DDL)
    if isinstance(node, exp.Update):
        return Classification(
            kind=StatementKind.WRITE, dangerous_reasons=[] if node.args.get("where") else ["update_without_where"]
        )
    if isinstance(node, exp.Delete):
        return Classification(
            kind=StatementKind.WRITE, dangerous_reasons=[] if node.args.get("where") else ["delete_without_where"]
        )
    if isinstance(node, (exp.Insert, exp.Merge)):
        return Classification(kind=StatementKind.WRITE)
    if isinstance(node, (exp.Transaction, exp.Commit, exp.Rollback)):
        return Classification(kind=StatementKind.TRANSACTION)
    if isinstance(node, (exp.Set, exp.Use)):
        return Classification(kind=StatementKind.SESSION)
    if isinstance(node, exp.Query):
        writes = any(isinstance(child, _DML) for child in node.walk())
        locks = bool(node.args.get("locks"))
        return Classification(kind=StatementKind.WRITE if writes or locks else StatementKind.READ)
    return None


def classify(sql: str, dialect: Dialect) -> Classification:
    inner = _explain_inner(sql, dialect)
    if inner is not None:
        result = classify(inner, dialect)
        return result if result.kind != StatementKind.READ else Classification(kind=StatementKind.READ)
    try:
        parsed = [e for e in sqlglot.parse(sql, read=SQLGLOT_NAME[dialect]) if e is not None]
    except SqlglotError:
        return _lexical(sql, dialect)
    if len(parsed) != 1 or isinstance(parsed[0], exp.Command):
        return _lexical(sql, dialect)
    return _from_expression(parsed[0]) or _lexical(sql, dialect)
```
If an sqlglot class name differs in the installed version (e.g. `exp.Alter` vs `exp.AlterTable`), use the installed name and keep the test expectations — the tests define the behaviour.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q`
Expected: all pass. If a sqlglot parse differs from the expectation for one statement, prefer adjusting `_from_expression` / the fallback, not the test.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat(sql): classify statements (read/write/ddl/...) and flag dangerous ones with sqlglot

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 4: Schema snapshots in the adapters

**Files:**
- Create: `backend/dbaguy_backend/domain/schema.py`, `backend/dbaguy_backend/adapters/postgres/schema.py`, `backend/dbaguy_backend/adapters/sqlite/schema.py`
- Modify: `backend/dbaguy_backend/adapters/base.py` (contract), `adapters/postgres/adapter.py`, `adapters/sqlite/adapter.py`, `domain/profiles.py` (`EngineInfo.dialect`)
- Test: `backend/tests/test_schema_snapshot.py`

**Interfaces:**
- Produces (domain/schema.py, pydantic):
  - `SnapshotColumn(name, type_name)`
  - `SnapshotForeignKey(name, columns: list[str], ref_schema, ref_table, ref_columns: list[str])`
  - `RelationKind = Literal["table", "view", "materialized_view"]`
  - `SnapshotRelation(name, kind, columns, primary_key: list[str] = [], foreign_keys = [])`
  - `SnapshotSchema(name, system: bool = False, relations = [], functions: list[str] = [])`
  - `SchemaSnapshot(database: str | None, default_schema: str | None, search_path: list[str], schemas, built_at: datetime)`
  - `ObjectSearchHit(database: str | None, schema_name, name, kind: RelationKind, path: list[PathSegment])`
- Adapter contract additions:
  - `DatabaseAdapter.dialect: ClassVar[Dialect]`
  - `DatabaseAdapter.relation_path(database, schema, name, kind) -> list[PathSegment]` — the explorer path for opening the object (PG `[database, schema, folder, <kind>:name]`, SQLite `[schema, folder, <kind>:name]`; folders `tables`/`views`/`materialized_views`).
  - `DatabaseConnection.schema_snapshot() -> SchemaSnapshot` (abstract).
  - `EngineInfo.dialect: Dialect` (filled by `DatabaseAdapter.info()`).
- PG: relations + columns in **one** query over `pg_class`/`pg_attribute` (relkind `r,p,f` → table, `v` → view, `m` → materialized_view; partitions excluded); PKs and FKs in one query each (`unnest(conkey, confkey) WITH ORDINALITY`); functions `SELECT DISTINCT nspname, proname … WHERE prokind IN ('f','a','w')`; `search_path` from `current_schemas(false)`; system schemas (`pg_catalog`, `information_schema`, `pg_*`) are listed with `system=True` but **without relations** (spec §3.2); only `pg_catalog` contributes function names. Run on the connection's native-loader cursor (not the text-first user cursor).
- SQLite: per schema from `PRAGMA database_list` (excluding `temp`): relations from `sqlite_master` (excluding `sqlite_%`), columns via `pragma_table_xinfo(name, schema)`, FKs via `pragma_foreign_key_list(name, schema)` (same schema), PK from `pk > 0` ordered by `pk`; `search_path=["main"]`, `default_schema="main"`; no functions.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_schema_snapshot.py`:
```python
import sqlite3
from uuid import uuid4

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.sql.dialects import Dialect
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg


def rel(snapshot, schema, name):
    s = next(s for s in snapshot.schemas if s.name == schema)
    return next(r for r in s.relations if r.name == name)


@requires_pg
def test_postgres_snapshot():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    assert snap.database == "dbaguy"
    assert snap.default_schema == "public" and snap.search_path[0] == "public"
    customers = rel(snap, "public", "customers")
    assert customers.kind == "table" and customers.primary_key == ["id"]
    assert ("email", "email") in [(c.name, c.type_name) for c in customers.columns]
    orders = rel(snap, "sales", "orders")
    [fk] = orders.foreign_keys
    assert (fk.columns, fk.ref_schema, fk.ref_table, fk.ref_columns) == (["customer_id"], "public", "customers", ["id"])
    assert rel(snap, "sales", "order_summary").kind == "view"
    assert rel(snap, "sales", "daily_totals").kind == "materialized_view"
    assert rel(snap, "public", "Order Items").columns[1].name == "Select"
    assert "events_2026" not in [r.name for r in next(s for s in snap.schemas if s.name == "sales").relations]
    pg_catalog = next(s for s in snap.schemas if s.name == "pg_catalog")
    assert pg_catalog.system and pg_catalog.relations == [] and "count" in pg_catalog.functions  # spec §3.2
    assert "add_numbers" in next(s for s in snap.schemas if s.name == "public").functions


@requires_pg
def test_postgres_snapshot_of_another_database():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, "analytics")
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    assert snap.database == "analytics"
    assert [r.name for r in next(s for s in snap.schemas if s.name == "public").relations] == ["page_views"]


def test_sqlite_snapshot(tmp_path):
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE books (id INTEGER, author_id INTEGER REFERENCES authors(id), title TEXT, PRIMARY KEY (id));"
        "CREATE VIEW titles AS SELECT title FROM books;"
        "CREATE TABLE seq_user (id INTEGER PRIMARY KEY AUTOINCREMENT);"  # creates sqlite_sequence
    )
    raw.close()
    profile = SqliteProfile(id=uuid4(), name="s", engine="sqlite", path=str(db))
    conn = SqliteAdapter().open(profile, None, None, None)
    try:
        snap = conn.schema_snapshot()
    finally:
        conn.close()
    assert snap.search_path == ["main"] and snap.default_schema == "main"
    main = snap.schemas[0]
    assert [r.name for r in main.relations] == ["authors", "books", "seq_user", "titles"]  # no sqlite_sequence
    books = rel(snap, "main", "books")
    assert books.primary_key == ["id"]
    assert [(f.columns, f.ref_schema, f.ref_table, f.ref_columns) for f in books.foreign_keys] == [
        (["author_id"], "main", "authors", ["id"])
    ]
    assert rel(snap, "main", "titles").kind == "view"


def test_relation_paths_and_dialects():
    pg, lite = PostgresAdapter(), SqliteAdapter()
    assert [(s.kind, s.name) for s in pg.relation_path("dbaguy", "sales", "orders", "table")] == [
        ("database", "dbaguy"), ("schema", "sales"), ("folder", "tables"), ("table", "orders"),
    ]
    assert [(s.kind, s.name) for s in lite.relation_path(None, "main", "v", "view")] == [
        ("schema", "main"), ("folder", "views"), ("view", "v"),
    ]
    assert pg.info().dialect == Dialect.POSTGRES and lite.info().dialect == Dialect.SQLITE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_schema_snapshot.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.domain.schema'`).

- [ ] **Step 3: Implement**

`backend/dbaguy_backend/domain/schema.py`:
```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .metadata import PathSegment

RelationKind = Literal["table", "view", "materialized_view"]


class SnapshotColumn(BaseModel):
    name: str
    type_name: str


class SnapshotForeignKey(BaseModel):
    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]


class SnapshotRelation(BaseModel):
    name: str
    kind: RelationKind
    columns: list[SnapshotColumn]
    primary_key: list[str] = []
    foreign_keys: list[SnapshotForeignKey] = []


class SnapshotSchema(BaseModel):
    name: str
    system: bool = False
    relations: list[SnapshotRelation] = []
    functions: list[str] = []


class SchemaSnapshot(BaseModel):
    database: str | None
    default_schema: str | None
    search_path: list[str]
    schemas: list[SnapshotSchema]
    built_at: datetime


class ObjectSearchHit(BaseModel):
    database: str | None
    schema_name: str
    name: str
    kind: RelationKind
    path: list[PathSegment]
```

In `domain/profiles.py` add `dialect: Dialect` to `EngineInfo` (import `Dialect` from `..sql.dialects`). In `adapters/base.py`:
- import `Dialect`, `SchemaSnapshot`, `RelationKind`, `NodeKind`, `Folder`, `make_node`/`child_path`;
- add `dialect: ClassVar[Dialect]` to `DatabaseAdapter` and make `info()` pass `dialect=self.dialect`;
- add the abstract `schema_snapshot()` to `DatabaseConnection` with docstring "Schemas, relations, columns, PK/FK of the connection's current database, built with a small fixed number of queries.";
- add to `DatabaseAdapter` a non-abstract default used by both engines:
```python
    _FOLDER_FOR_KIND: ClassVar[dict[str, str]] = {
        "table": Folder.TABLES, "view": Folder.VIEWS, "materialized_view": Folder.MATERIALIZED_VIEWS,
    }

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
        path: list[PathSegment] = []
        if self.capabilities.per_database_connection and database:
            path = child_path(path, NodeKind.DATABASE, database)
        path = child_path(path, NodeKind.SCHEMA, schema)
        path = child_path(path, NodeKind.FOLDER, self._FOLDER_FOR_KIND[kind])
        return child_path(path, kind, name)
```
Set `dialect = Dialect.POSTGRES` on `PostgresAdapter` and `dialect = Dialect.SQLITE` on `SqliteAdapter`.

`backend/dbaguy_backend/adapters/postgres/schema.py`:
```python
from datetime import UTC, datetime

import psycopg

from ...domain.schema import SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema

_SCHEMAS_SQL = r"""
SELECT nspname, (nspname IN ('pg_catalog', 'information_schema') OR nspname LIKE 'pg\_%') AS is_system
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_toast%' AND nspname NOT LIKE 'pg\_temp\_%'
ORDER BY is_system, nspname
"""
_COLUMNS_SQL = r"""
SELECT n.nspname, c.relname, c.relkind::text, a.attname, format_type(a.atttypid, a.atttypmod)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
ORDER BY n.nspname, c.relname, a.attnum
"""
_PK_SQL = """
SELECT n.nspname, c.relname, array_agg(a.attname::text ORDER BY k.ord)
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(co.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.attnum
WHERE co.contype = 'p'
GROUP BY n.nspname, c.relname
"""
_FK_SQL = """
SELECT n.nspname, c.relname, co.conname, array_agg(a.attname::text ORDER BY k.ord),
       rn.nspname, rc.relname, array_agg(ra.attname::text ORDER BY k.ord)
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class rc ON rc.oid = co.confrelid
JOIN pg_namespace rn ON rn.oid = rc.relnamespace
CROSS JOIN LATERAL unnest(co.conkey, co.confkey) WITH ORDINALITY AS k(attnum, refnum, ord)
JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.attnum
JOIN pg_attribute ra ON ra.attrelid = co.confrelid AND ra.attnum = k.refnum
WHERE co.contype = 'f'
GROUP BY n.nspname, c.relname, co.conname, rn.nspname, rc.relname
ORDER BY n.nspname, c.relname, co.conname
"""
_FUNCTIONS_SQL = r"""
SELECT DISTINCT n.nspname, p.proname
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'a', 'w') AND (n.nspname = 'pg_catalog' OR n.nspname NOT IN ('information_schema') AND n.nspname NOT LIKE 'pg\_%')
"""
_KIND = {"r": "table", "p": "table", "f": "table", "v": "view", "m": "materialized_view"}


def build_snapshot(conn: psycopg.Connection) -> SchemaSnapshot:
    schemas = {name: SnapshotSchema(name=name, system=is_system) for name, is_system in conn.execute(_SCHEMAS_SQL)}
    relations: dict[tuple[str, str], SnapshotRelation] = {}
    for schema, relname, relkind, column, type_name in conn.execute(_COLUMNS_SQL):
        key = (schema, relname)
        if key not in relations:
            relations[key] = SnapshotRelation(name=relname, kind=_KIND[relkind], columns=[])
            schemas[schema].relations.append(relations[key])
        relations[key].columns.append(SnapshotColumn(name=column, type_name=type_name))
    for schema, relname, columns in conn.execute(_PK_SQL):
        if (schema, relname) in relations:
            relations[(schema, relname)].primary_key = list(columns)
    for schema, relname, name, columns, ref_schema, ref_table, ref_columns in conn.execute(_FK_SQL):
        if (schema, relname) in relations:
            relations[(schema, relname)].foreign_keys.append(SnapshotForeignKey(
                name=name, columns=list(columns), ref_schema=ref_schema, ref_table=ref_table,
                ref_columns=list(ref_columns),
            ))
    for schema, proname in conn.execute(_FUNCTIONS_SQL):
        if schema in schemas:
            schemas[schema].functions.append(proname)
    for s in schemas.values():
        s.functions.sort()
    database, search_path = conn.execute("SELECT current_database(), current_schemas(false)").fetchone()
    return SchemaSnapshot(
        database=database, default_schema=search_path[0] if search_path else None,
        search_path=list(search_path), schemas=list(schemas.values()), built_at=datetime.now(UTC),
    )
```
`PostgresConnection.schema_snapshot()` → `return schema.build_snapshot(self._conn)` (import the module as `from . import schema as pg_schema`).

`backend/dbaguy_backend/adapters/sqlite/schema.py`:
```python
import sqlite3
from datetime import UTC, datetime

from ...domain.schema import SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema
from ..sql_text import quote_ident_sqlite as q


def build_snapshot(conn: sqlite3.Connection) -> SchemaSnapshot:
    schemas: list[SnapshotSchema] = []
    for _, schema_name, _ in conn.execute("PRAGMA database_list").fetchall():
        if schema_name == "temp":
            continue
        master = f"{q(schema_name)}.sqlite_master"
        relations: dict[str, SnapshotRelation] = {}
        for name, type_ in conn.execute(
            f"SELECT name, type FROM {master} WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            f"ORDER BY name"
        ):
            relations[name] = SnapshotRelation(name=name, kind="table" if type_ == "table" else "view", columns=[])
        pks: dict[str, list[tuple[int, str]]] = {}
        for rel_name, col, type_name, pk in conn.execute(
            f"SELECT m.name, p.name, p.type, p.pk FROM {master} m JOIN pragma_table_xinfo(m.name, ?) p "
            f"WHERE m.type IN ('table', 'view') ORDER BY m.name, p.cid",
            (schema_name,),
        ):
            if rel_name in relations:
                relations[rel_name].columns.append(SnapshotColumn(name=col, type_name=type_name or ""))
                if pk:
                    pks.setdefault(rel_name, []).append((pk, col))
        for rel_name, cols in pks.items():
            relations[rel_name].primary_key = [c for _, c in sorted(cols)]
        fks: dict[tuple[str, int], SnapshotForeignKey] = {}
        for rel_name, fk_id, _seq, ref_table, col, ref_col in conn.execute(
            f'SELECT m.name, f.id, f.seq, f."table", f."from", f."to" FROM {master} m '
            f"JOIN pragma_foreign_key_list(m.name, ?) f WHERE m.type = 'table' ORDER BY m.name, f.id, f.seq",
            (schema_name,),
        ):
            if rel_name not in relations:
                continue
            key = (rel_name, fk_id)
            if key not in fks:
                fks[key] = SnapshotForeignKey(
                    name=f"fk_{rel_name}_{fk_id}", columns=[], ref_schema=schema_name, ref_table=ref_table,
                    ref_columns=[],
                )
                relations[rel_name].foreign_keys.append(fks[key])
            fks[key].columns.append(col)
            fks[key].ref_columns.append(ref_col or "")
        schemas.append(SnapshotSchema(name=schema_name, relations=list(relations.values())))
    return SchemaSnapshot(
        database=None, default_schema="main", search_path=["main"], schemas=schemas, built_at=datetime.now(UTC),
    )
```
(`ref_col` is NULL when the FK references the parent's implicit primary key; resolve it to the parent's PK in a follow-up pass if `ref_col` is empty — the test above uses explicit columns, so a pass that fills `""` from the referenced relation's `primary_key` is enough.) `SqliteConnection.schema_snapshot()` → wrap `build_snapshot(self._conn)` translating `sqlite3.Error` with the module's `_translate`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_schema_snapshot.py -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck` (EngineInfo gained `dialect`).
```bash
git add backend src/lib/api
git commit -m "feat(backend): build schema snapshots (relations, columns, PK/FK, functions) for PG and SQLite

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 5: Schema cache, object search and refresh

**Files:**
- Create: `backend/dbaguy_backend/services/schema_cache.py`
- Modify: `backend/dbaguy_backend/services/metadata_service.py` (public `with_connection`), `backend/dbaguy_backend/container.py` (build `SchemaCache`), `backend/dbaguy_backend/api/metadata.py` (endpoints), `backend/dbaguy_backend/api/profiles.py` (invalidate on update/delete)
- Test: `backend/tests/test_schema_cache.py`

**Interfaces:**
- Consumes: `MetadataService` (①), `AdapterRegistry`, `ProfileService`, `SchemaSnapshot`, `ObjectSearchHit` (Task 4), `SnapshotIndex` (Task 6 — import lazily; for this task store the snapshot only).
- Produces:
  - `MetadataService.with_connection(profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T` — same worker/retry semantics as `children()`; `database` only selects a connection for adapters with `per_database_connection`.
  - `SchemaCache(metadata, registry, profiles, ttl_s=600.0, failure_ttl_s=30.0, clock=time.monotonic)` with `get(profile_id, database=None) -> SchemaSnapshot` (builds on miss or expiry; one build per key at a time; other keys not blocked; a failed build is remembered for `failure_ttl_s` and re-raised without reconnecting — completion calls it on every keystroke, and an unreachable server must not cost a connect timeout each time; `invalidate` forgets failures too), `peek(profile_id, database=None) -> SchemaSnapshot | None`, `invalidate(profile_id, database: str | None = None)` (all databases of the profile when `database` is None), `search(profile_id, q, database=None, limit=50) -> list[ObjectSearchHit]`.
  - Search ranking: exact name (case-insensitive) > prefix > substring; ties: non-system schemas first, then search_path order, then name; `q` may be `schema.name`; empty `q` returns []; hits carry `path = adapter.relation_path(snapshot.database, schema, name, kind)`.
  - HTTP: `GET /metadata/search?profile_id=&q=&database=&limit=` → `list[ObjectSearchHit]`; `POST /metadata/schema/refresh {profile_id, database?}` → 204 (invalidate only; the next use rebuilds).
  - `PUT /profiles/{id}` and `DELETE /profiles/{id}` also call `schema_cache.invalidate(id)`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_schema_cache.py` (SQLite-backed so it needs no Docker):
```python
import sqlite3

import pytest

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.metadata_service import MetadataService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.schema_cache import SchemaCache
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "c.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);"
        "CREATE TABLE user_roles (user_id INTEGER, role TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id));"
    )
    raw.close()
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    pid = profiles.create(ProfileUpsertRequest.model_validate(
        {"profile": {"engine": "sqlite", "name": "c", "path": str(db)}})).profile.id
    registry = AdapterRegistry([SqliteAdapter()])
    connections = ConnectionService(registry, profiles, SshTunnelManager(tmp_path / "askpass.sh"))
    metadata = MetadataService(registry, connections, profiles)
    clock = Clock()
    cache = SchemaCache(metadata, registry, profiles, ttl_s=60, clock=clock)
    yield cache, pid, db, clock
    metadata.close_all()


def test_get_caches_until_ttl_or_invalidate(env, monkeypatch):
    cache, pid, db, clock = env
    first = cache.get(pid)
    assert cache.get(pid) is first  # cached
    sqlite3.connect(db).executescript("CREATE TABLE later (id INTEGER);")
    assert "later" not in [r.name for r in cache.get(pid).schemas[0].relations]
    clock.now += 61
    assert "later" in [r.name for r in cache.get(pid).schemas[0].relations]
    sqlite3.connect(db).executescript("CREATE TABLE later2 (id INTEGER);")
    cache.invalidate(pid)
    assert "later2" in [r.name for r in cache.get(pid).schemas[0].relations]


def test_peek_does_not_build(env):
    cache, pid, _, _ = env
    assert cache.peek(pid) is None
    cache.get(pid)
    assert cache.peek(pid) is not None


def test_search_ranking_and_paths(env):
    cache, pid, _, _ = env
    hits = cache.search(pid, "user")
    assert [h.name for h in hits] == ["users", "user_roles"]  # prefix matches, shorter first via name order
    assert [(s.kind, s.name) for s in hits[0].path] == [("schema", "main"), ("folder", "tables"), ("table", "users")]
    assert [h.name for h in cache.search(pid, "ORDERS")] == ["orders"]
    assert [h.name for h in cache.search(pid, "main.ord")] == ["orders"]
    assert cache.search(pid, "") == []
    assert [h.name for h in cache.search(pid, "role")] == ["user_roles"]  # substring
```
(`test_search_ranking_and_paths` expects ties within the same rank ordered by name; `users` and `user_roles` are both prefix matches and `user_roles` < `users` alphabetically — so define the tie-break as **shorter name first, then alphabetical**, which yields `["users", "user_roles"]`.)

Also in `test_schema_cache.py`:
```python
def test_a_failed_build_is_not_retried_on_every_call(env, monkeypatch):
    cache, pid, _, clock = env
    calls = []

    def unreachable(*args):
        calls.append(1)
        raise ConnectionFailed("server down")

    monkeypatch.setattr(cache._metadata, "with_connection", unreachable)
    for _ in range(3):
        with pytest.raises(ConnectionFailed):
            cache.get(pid)
    assert len(calls) == 1
    clock.now += 31
    with pytest.raises(ConnectionFailed):
        cache.get(pid)
    assert len(calls) == 2
    cache.invalidate(pid)
    with pytest.raises(ConnectionFailed):
        cache.get(pid)
    assert len(calls) == 3
```
(import `ConnectionFailed` from `dbaguy_backend.domain.errors`.) In `get`, check `self._failures[key]` (time, exception) next to the entry check, record the failure when the build raises, and log the first failure per key at WARNING.

Add to `backend/tests/test_api.py`:
```python
def test_metadata_search_and_refresh(client, sqlite_profile_id):
    hits = client.get("/metadata/search", params={"profile_id": sqlite_profile_id, "q": "t"}, headers=AUTH).json()
    assert hits[0]["name"] == "t" and hits[0]["path"][-1] == {"kind": "table", "name": "t"}
    response = client.post("/metadata/schema/refresh", json={"profile_id": sqlite_profile_id}, headers=AUTH)
    assert response.status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_schema_cache.py tests/test_api.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.services.schema_cache'`).

- [ ] **Step 3: Implement**

`MetadataService.with_connection` — refactor `_call` to take `(profile_id, database_hint_path_or_database)`: extract the routing (`database` chosen from the request path today) into `_database_for(adapter, path)`, and implement:
```python
    def with_connection(self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        key_db = database if adapter.capabilities.per_database_connection else None
        return self._call_on(profile, key_db, fn)
```
where `_call_on(profile, database, fn)` is the existing attempt/retry loop body, now shared by `children`/`detail`/`with_connection` (no duplicated retry logic).

`backend/dbaguy_backend/services/schema_cache.py`:
```python
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from loguru import logger

from ..adapters.registry import AdapterRegistry
from ..domain.schema import ObjectSearchHit, SchemaSnapshot
from .metadata_service import MetadataService
from .profile_service import ProfileService

_Key = tuple[UUID, str]


@dataclass
class _Entry:
    snapshot: SchemaSnapshot
    built_at: float


class SchemaCache:
    """Schema snapshots per (profile, database), rebuilt after `ttl_s` or on invalidation."""

    def __init__(
        self, metadata: MetadataService, registry: AdapterRegistry, profiles: ProfileService,
        ttl_s: float = 600.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._metadata = metadata
        self._registry = registry
        self._profiles = profiles
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[_Key, _Entry] = {}
        self._key_locks: dict[_Key, threading.Lock] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(profile_id: UUID, database: str | None) -> _Key:
        return (profile_id, database or "")

    def peek(self, profile_id: UUID, database: str | None = None) -> SchemaSnapshot | None:
        with self._lock:
            entry = self._entries.get(self._key(profile_id, database))
        return entry.snapshot if entry else None

    def get(self, profile_id: UUID, database: str | None = None) -> SchemaSnapshot:
        key = self._key(profile_id, database)
        with self._lock:
            entry = self._entries.get(key)
            if entry and self._clock() - entry.built_at < self._ttl:
                return entry.snapshot
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                entry = self._entries.get(key)
                if entry and self._clock() - entry.built_at < self._ttl:
                    return entry.snapshot
            started = time.monotonic()
            snapshot = self._metadata.with_connection(profile_id, database, lambda c: c.schema_snapshot())
            relations = sum(len(s.relations) for s in snapshot.schemas)
            logger.info("Built schema snapshot for {} ({} relations, {:.0f} ms)", profile_id, relations,
                        (time.monotonic() - started) * 1000)
            with self._lock:
                self._entries[key] = _Entry(snapshot=snapshot, built_at=self._clock())
            return snapshot

    def invalidate(self, profile_id: UUID, database: str | None = None) -> None:
        with self._lock:
            if database is None:
                for key in [k for k in self._entries if k[0] == profile_id]:
                    del self._entries[key]
            else:
                self._entries.pop(self._key(profile_id, database), None)

    def search(self, profile_id: UUID, q: str, database: str | None = None, limit: int = 50) -> list[ObjectSearchHit]:
        q = q.strip()
        if not q:
            return []
        snapshot = self.get(profile_id, database)
        adapter = self._registry.get(self._profiles.get(profile_id).engine)
        schema_part, _, name_part = q.rpartition(".")
        needle, schema_needle = name_part.lower(), schema_part.lower()
        search_path = [s.lower() for s in snapshot.search_path]
        scored = []
        for schema in snapshot.schemas:
            if schema_needle and not schema.name.lower().startswith(schema_needle):
                continue
            for rel in schema.relations:
                name = rel.name.lower()
                rank = 0 if name == needle else 1 if name.startswith(needle) else 2 if needle in name else None
                if rank is None:
                    continue
                path_rank = search_path.index(schema.name.lower()) if schema.name.lower() in search_path else len(search_path)
                scored.append(((rank, schema.system, path_rank, len(rel.name), rel.name), schema, rel))
        scored.sort(key=lambda item: item[0])
        return [
            ObjectSearchHit(
                database=snapshot.database, schema_name=schema.name, name=rel.name, kind=rel.kind,
                path=adapter.relation_path(snapshot.database, schema.name, rel.name, rel.kind),
            )
            for _, schema, rel in scored[:limit]
        ]
```
(Task 6 adds the cached `SnapshotIndex`; leave room by keeping `_Entry` a dataclass.)

Container: `self.schema_cache = SchemaCache(self.metadata, self.registry, self.profiles)`. API (`api/metadata.py`):
```python
class SchemaRefreshRequest(BaseModel):
    profile_id: UUID
    database: str | None = None


@router.get("/search", response_model=list[ObjectSearchHit])
def search(profile_id: UUID, q: str = "", database: str | None = None, limit: int = Query(50, ge=1, le=500),
           container: AppContainer = Depends(get_container)) -> list[ObjectSearchHit]:
    return container.schema_cache.search(profile_id, q, database, limit)


@router.post("/schema/refresh", status_code=204)
def refresh_schema(body: SchemaRefreshRequest, container: AppContainer = Depends(get_container)) -> Response:
    container.schema_cache.invalidate(body.profile_id, body.database)
    return Response(status_code=204)
```
Profiles router: call `container.schema_cache.invalidate(profile_id)` after update and in delete.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_schema_cache.py tests/test_api.py -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck`
```bash
git add backend src/lib/api
git commit -m "feat(backend): cache schema snapshots per connection and search objects for quick open

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 6: Completion and describe

**Files:**
- Create: `backend/dbaguy_backend/sql/completion.py`
- Modify: `backend/dbaguy_backend/services/schema_cache.py` (cache a `SnapshotIndex` per entry: `index(profile_id, database) -> SnapshotIndex`)
- Test: `backend/tests/sql/test_completion.py`

**Interfaces:**
- Consumes: `split`, `tokenize`, `TokenKind`, `quote_ident`, `Dialect` (Tasks 1–2), `SchemaSnapshot` (Task 4).
- Produces:
  - `SnapshotIndex(snapshot, dialect)` with `schema(name)`, `resolve(schema | None, name) -> tuple[SnapshotSchema, SnapshotRelation] | None` (case-insensitive, exact case preferred, unqualified names resolved in search_path order), `in_search_path(schema)`, `functions: list[str]`.
  - pydantic models (they are returned by `/sql/*` in Task 14): `CompletionItem(label, kind, detail=None, apply=None, boost=0)` with `kind ∈ keyword|schema|table|view|materialized_view|column|alias|function|join`.
  - `CompletionResult(start, end, items)` — code-point offsets into the full text; `end` = cursor.
  - `DescribeColumn(name, type_name, is_primary_key, references: str | None)`, `DescribeResult(schema_name, name, kind, columns)`.
  - `complete(text, cursor, dialect, index: SnapshotIndex | None) -> CompletionResult` — never raises; `index=None` → keywords only.
  - `describe_at(text, cursor, dialect, index) -> DescribeResult | None`.
  - `SchemaCache.index(profile_id, database=None) -> SnapshotIndex` (built lazily once per snapshot).

Behaviour (spec §3.4):
- Work on the statement containing the cursor: a split span with `start <= cursor <= terminator_end`; otherwise the gap between statements (start context).
- No items when the cursor is inside a string or comment.
- Partial word = identifier characters immediately before the cursor; `start` = its beginning.
- **Qualified** (`x.` before the partial word): x is an alias or relation in scope → its columns; else a schema → its relations; else a relation resolvable by name → its columns.
- **Scope** = relations introduced by `FROM`/`JOIN`/`UPDATE`/`INTO`/`TABLE` anywhere in the statement (including after the cursor), with optional `[AS] alias`, comma lists after `FROM`, subqueries `( … ) alias` (alias only).
- **Context** = last clause keyword before the cursor at the cursor's parenthesis depth: `FROM/JOIN/UPDATE/INTO/TABLE` → relations (but if the previous token is already a relation name, i.e. not the keyword or a comma, → clause keywords); `ON` directly before the cursor → FK join conditions (boost 3) + columns; `SELECT/WHERE/HAVING/SET/BY/RETURNING/WHEN/THEN/ELSE/CASE/DISTINCT/USING` or inside parentheses → columns of scope + aliases + functions + keywords; `AND/OR/NOT` are skipped while searching backwards; no clause → statement-starting keywords.
- Relations outside the search path get a schema-qualified label; system-schema relations get boost −1.
- Filtering: case-insensitive prefix match (boost +2) or substring (+1); sort by `(-boost, kind order, len(label), label)`; at most 300 items.
- `apply` quotes identifiers that need quoting (`quote_ident`); join items apply the full condition text.

- [ ] **Step 1: Write the failing tests**

`backend/tests/sql/test_completion.py`:
```python
import time
from datetime import UTC, datetime

import pytest

from dbaguy_backend.domain.schema import (
    SchemaSnapshot, SnapshotColumn, SnapshotForeignKey, SnapshotRelation, SnapshotSchema,
)
from dbaguy_backend.sql.completion import SnapshotIndex, complete, describe_at
from dbaguy_backend.sql.dialects import Dialect

PG = Dialect.POSTGRES


def table(name, columns, pk=None, fks=None, kind="table"):
    return SnapshotRelation(
        name=name, kind=kind, columns=[SnapshotColumn(name=c, type_name=t) for c, t in columns],
        primary_key=pk or [], foreign_keys=fks or [],
    )


@pytest.fixture(scope="module")
def index():
    snapshot = SchemaSnapshot(
        database="app", default_schema="public", search_path=["public"], built_at=datetime.now(UTC),
        schemas=[
            SnapshotSchema(name="public", relations=[
                table("users", [("id", "integer"), ("email", "text"), ("name", "text")], pk=["id"]),
                table("orders", [("id", "integer"), ("user_id", "integer"), ("total", "numeric")], pk=["id"],
                      fks=[SnapshotForeignKey(name="fk", columns=["user_id"], ref_schema="public",
                                              ref_table="users", ref_columns=["id"])]),
                table("Order Items", [("id", "integer"), ("Select", "text")]),
            ], functions=["now_utc"]),
            SnapshotSchema(name="sales", relations=[table("invoices", [("id", "integer"), ("amount", "numeric")])]),
            SnapshotSchema(name="pg_catalog", system=True, relations=[table("pg_class", [("relname", "name")])],
                           functions=["count", "upper"]),
        ],
    )
    return SnapshotIndex(snapshot, PG)


def run(text, index):
    cursor = text.index("|")
    sql = text.replace("|", "")
    return complete(sql, cursor, PG, index), sql


def labels(result):
    return [i.label for i in result.items]


def test_relations_after_from(index):
    result, sql = run("SELECT * FROM u|", index)
    assert labels(result)[0] == "users"
    assert (result.start, result.end) == (sql.index("u"), len(sql))


def test_schema_qualified_relations(index):
    result, _ = run("SELECT * FROM sales.|", index)
    assert labels(result) == ["invoices"]
    result, _ = run("SELECT * FROM inv|", index)
    assert "sales.invoices" in labels(result)


def test_alias_columns_even_when_alias_is_defined_after_cursor(index):
    result, _ = run("SELECT u.| FROM users u", index)
    assert labels(result) == ["id", "email", "name"]


def test_columns_of_all_relations_in_scope(index):
    result, _ = run("SELECT | FROM users u JOIN orders o ON o.user_id = u.id", index)
    names = labels(result)
    assert {"email", "total", "user_id", "u", "o"} <= set(names)
    assert next(i for i in result.items if i.label == "total").detail.startswith("orders")


def test_fk_join_condition(index):
    result, _ = run("SELECT * FROM users u JOIN orders o ON |", index)
    assert result.items[0].kind == "join" and result.items[0].label == "o.user_id = u.id"


def test_quoted_identifiers(index):
    result, _ = run('SELECT * FROM "Order Items" oi WHERE oi.S|', index)
    [item] = result.items
    assert item.label == "Select" and item.apply == '"Select"'
    result, _ = run("SELECT * FROM Ord|", index)
    assert next(i for i in result.items if i.label == "Order Items").apply == '"Order Items"'


def test_no_items_inside_strings_or_comments(index):
    assert complete("SELECT * FROM users WHERE name = 'ab", 38, PG, index).items == []
    assert complete("SELECT 1 -- fro", 15, PG, index).items == []


def test_statement_keywords_after_semicolon(index):
    result, _ = run("SELECT 1; |", index)
    assert "SELECT" in labels(result) and all(i.kind == "keyword" for i in result.items)


def test_uses_the_statement_under_cursor(index):
    result, sql = run("SELECT * FROM users; SELECT * FROM ord|", index)
    assert labels(result)[0] == "orders" and result.start == sql.rindex("ord")


def test_after_relation_name_suggests_clause_keywords(index):
    result, _ = run("SELECT * FROM users W|", index)
    assert "WHERE" in labels(result) and "users" not in labels(result)


def test_without_snapshot_only_keywords():
    result = complete("SELECT * FR", 11, PG, None)
    assert "FROM" in [i.label for i in result.items]


@pytest.mark.parametrize("text", ["", "(((", "'", '"', "SELECT", "SELECT * FROM", "FROM . . .", "JOIN ON ON", ";;;"])
def test_never_raises(index, text):
    complete(text, len(text), PG, index)
    describe_at(text, len(text), PG, index)


def test_describe(index):
    sql = "SELECT * FROM orders o WHERE o.total > 1"
    found = describe_at(sql, sql.index("orders") + 2, PG, index)
    assert found.name == "orders" and found.columns[1].references == "public.users(id)"
    via_alias = describe_at(sql, sql.index("o.total") + 1, PG, index)
    assert via_alias.name == "orders"
    assert describe_at(sql, sql.index("SELECT") + 1, PG, index) is None


def test_performance_on_a_large_schema():
    relations = [table(f"table_{i:05d}", [(f"col_{j}", "text") for j in range(20)]) for i in range(5000)]
    snapshot = SchemaSnapshot(
        database="big", default_schema="public", search_path=["public"], built_at=datetime.now(UTC),
        schemas=[SnapshotSchema(name="public", relations=relations)],
    )
    big = SnapshotIndex(snapshot, PG)
    samples = []
    for text in ["SELECT * FROM table_01|", "SELECT t.| FROM table_04999 t", "SELECT col| FROM table_00001"] * 20:
        cursor = text.index("|")
        started = time.perf_counter()
        complete(text.replace("|", ""), cursor, PG, big)
        samples.append(time.perf_counter() - started)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    print(f"completion p95: {p95 * 1000:.1f} ms")
    assert p95 < 0.2  # generous gate; the spec target (50 ms) is checked by reading the printed number
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_completion.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.sql.completion'`).

- [ ] **Step 3: Implement**

Implement `backend/dbaguy_backend/sql/completion.py` to the behaviour above. Required structure (keep each function small):
- `SnapshotIndex` — dicts built once: `schemas_by_lower`, `relations_by_lower_name: dict[str, list[(schema, relation)]]`, `search_path_lower`, `functions` (sorted, deduplicated across schemas).
- `_statement_bounds(text, cursor, dialect) -> tuple[int, int]` using `split`.
- `_significant(tokens)` — drop SPACE/COMMENT.
- `_unquote(token) -> str` — strip `"…"` (unescape `""`), `[…]`, `` `…` ``.
- `_scope(tokens) -> list[ScopeRelation(schema | None, name, alias | None)]` and `_read_relations(tokens, i, out, allow_list)` as described (subquery skip by paren matching; `ONLY` skipped; alias stop-words: `ON WHERE JOIN LEFT RIGHT INNER OUTER FULL CROSS NATURAL GROUP ORDER LIMIT OFFSET SET USING UNION EXCEPT INTERSECT WINDOW HAVING VALUES RETURNING LATERAL SELECT FROM AS FOR DEFAULT WITH`).
- `_context(before_tokens) -> tuple[str, Token | None]` — backwards scan with paren depth (`RPAREN` +1, `LPAREN` −1; reaching an unmatched `LPAREN` → `"columns"`).
- `_relation_items`, `_column_items(scope)`, `_join_items(scope)`, `_keyword_items(kind)` — keyword lists per dialect in a module-level dict keyed by `Dialect` (statement starters: `SELECT INSERT UPDATE DELETE WITH CREATE ALTER DROP EXPLAIN BEGIN COMMIT ROLLBACK` + `PRAGMA` for SQLite; clause keywords: `FROM WHERE JOIN LEFT JOIN INNER JOIN GROUP BY ORDER BY HAVING LIMIT OFFSET AND OR NOT IN IS NULL AS ON DISTINCT CASE WHEN THEN ELSE END UNION ALL ASC DESC RETURNING`).
- `_filter(items, prefix)` — boosts and sort as specified.
- `describe_at`: identifier token at/just before the cursor; if preceded by `ident .` use it as schema (or alias qualifier when the left part is an alias — then describe the alias's relation only if the token itself is the alias); an alias in scope resolves to its relation; FK columns render `references="<ref_schema>.<ref_table>(<ref_col>)"`.

`SchemaCache`: store `index: SnapshotIndex | None` on `_Entry` and add
```python
    def index(self, profile_id: UUID, database: str | None = None) -> SnapshotIndex:
        snapshot = self.get(profile_id, database)
        key = self._key(profile_id, database)
        with self._lock:
            entry = self._entries[key]
            if entry.index is None or entry.index.snapshot is not snapshot:
                dialect = self._registry.get(self._profiles.get(profile_id).engine).dialect
                entry.index = SnapshotIndex(snapshot, dialect)
            return entry.index
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q -s -k completion`
Expected: all pass; note the printed p95 in your report (target < 50 ms).

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(sql): schema-aware completion with alias scope, FK join conditions and describe

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 7: Row formatting (copy/export) and the table-browse SELECT

**Files:**
- Create: `backend/dbaguy_backend/domain/formats.py` (`FormatKind`, `FormatColumn`, `RowFormatOptions` — in the domain because the WS protocol uses them in Task 11; `sql/formatting.py` imports and re-exports them), `backend/dbaguy_backend/sql/formatting.py`, `backend/dbaguy_backend/sql/select_builder.py`
- Test: `backend/tests/sql/test_formatting.py`, `backend/tests/sql/test_select_builder.py`

**Interfaces:**
- Consumes: `Dialect`, `quote_ident`, `qualified`, `quote_literal`, `tokenize` (Task 1); `ColumnCategory` (①); `PathSegment`, `NodeKind` (①); `ValidationFailed` (①).
- Produces (formatting; all models are pydantic, `RowFormatOptions` with `extra="forbid"`):
  - `FormatKind` (StrEnum `tsv, csv, json, jsonl, markdown, insert, in_list`).
  - `FormatColumn(name, category)`.
  - `RowFormatOptions(header=True, null_text="", table_schema: str | None = None, table_name: str | None = None)`.
  - `FormatResult(text, warnings: list[str])`.
  - `format_rows(kind, columns, rows, options, dialect, source: Literal["encoded", "raw"]) -> FormatResult` (encoded = cells as sent to the UI by `encode_row`; raw = driver values).
  - `open_writer(kind, fp: TextIO, columns, options, dialect) -> RowWriter` with `write(rows_raw)` and `close()` — streaming, raw values, used by export; `in_list` is not supported by writers (ValueError).
- Value rules:
  - `None` → `null_text` (text formats), JSON `null`, SQL `NULL`.
  - bool → `true`/`false` (text), JSON bool, SQL `TRUE`/`FALSE` (PG) or `1`/`0` (SQLite).
  - Encoded `{"$type":"bytes"}` → text `0x<preview>` (+`…` if size > preview bytes); SQL: NULL + warning `"N binary value(s) were truncated in the grid and written as NULL"`.
  - Encoded `{"$type":"text","truncated":true}` → text preview + `…`; SQL NULL + the same style warning.
  - Raw bytes → text `0x<full hex>`; SQL PG `'\x<hex>'::bytea`, SQLite `X'<hex>'`.
  - Raw Decimal/int beyond 2^53 → exact text; raw datetime/date/time → ISO; UUID → str; dict/list → JSON text.
  - Numbers in `number` category columns are emitted unquoted in SQL only when they match `^-?\d+(\.\d+)?([eE][+-]?\d+)?$`, else quoted.
  - TSV: a value containing tab, newline, CR or `"` is wrapped in `"…"` with `""` escaping.
  - CSV: `csv` module, `lineterminator="\n"`, `QUOTE_MINIMAL`.
  - Markdown: escape `|`, newlines → space.
  - INSERT: `INSERT INTO <qualified table or table_name> (<quoted cols>) VALUES (<literals>);` one per line.
  - IN list: exactly one column else `ValidationFailed`; distinct values in first-seen order → `(v1, v2)`.
- Produces (select_builder):
  - `relation_from_path(path) -> tuple[str | None, str]` (schema = last `schema` segment; name = last segment whose kind is table/view/materialized_view/partition; otherwise `ValidationFailed`).
  - `SortKey(column, descending=False)` (pydantic).
  - `build_browse_select(dialect, path, where: str | None, order_by: list[SortKey], count: bool = False) -> str` — `SELECT * FROM <qualified>` or `SELECT count(*) FROM …`; `\nWHERE <where>` (trimmed, trailing `;` removed; a `;` token outside literals → `ValidationFailed("A filter must be a single expression")`); `\nORDER BY "<col>" ASC|DESC, …` (not for count).

- [ ] **Step 1: Write the failing tests**

`backend/tests/sql/test_formatting.py`:
```python
import io
from datetime import date
from decimal import Decimal

import pytest

from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.execution import ColumnCategory as C
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.formatting import FormatColumn, FormatKind as F, RowFormatOptions, format_rows, open_writer

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE
COLS = [FormatColumn(name="id", category=C.NUMBER), FormatColumn(name="note", category=C.TEXT),
        FormatColumn(name="ok", category=C.BOOL)]
ROWS = [[1, "a\tb", True], ["9007199254740993", None, False]]


def fmt(kind, rows=ROWS, cols=COLS, dialect=PG, source="encoded", **options):
    return format_rows(kind, cols, rows, RowFormatOptions(**options), dialect, source)


def test_tsv_and_csv():
    assert fmt(F.TSV).text == 'id\tnote\tok\n1\t"a\tb"\ttrue\n9007199254740993\t\tfalse'
    assert fmt(F.TSV, header=False, null_text="NULL").text.splitlines()[1] == "9007199254740993\tNULL\tfalse"
    assert fmt(F.CSV).text == 'id,note,ok\n1,a\tb,true\n9007199254740993,,false\n'


def test_json_jsonl_markdown():
    assert '"note": null' in fmt(F.JSON).text
    assert fmt(F.JSONL).text.splitlines()[0] == '{"id": 1, "note": "a\\tb", "ok": true}'
    md = fmt(F.MARKDOWN, rows=[[1, "x|y\nz", True]]).text.splitlines()
    assert md[0] == "| id | note | ok |" and md[2] == "| 1 | x\\|y z | true |"


def test_insert_per_dialect_and_truncation_warning():
    rows = [[1, "it's", True], [2, {"$type": "text", "preview": "abc", "size": 70000, "truncated": True}, None]]
    result = fmt(F.INSERT, rows=rows, table_schema="sales", table_name="Order Items")
    assert result.text.splitlines() == [
        "INSERT INTO sales.\"Order Items\" (id, note, ok) VALUES (1, 'it''s', TRUE);",
        'INSERT INTO sales."Order Items" (id, note, ok) VALUES (2, NULL, NULL);',
    ]
    assert result.warnings and "truncated" in result.warnings[0]
    lite = fmt(F.INSERT, rows=[[1, "x", False]], dialect=LITE)
    assert lite.text == "INSERT INTO table_name (id, note, ok) VALUES (1, 'x', 0);"


def test_raw_values_for_export():
    cols = [FormatColumn(name="b", category=C.BINARY), FormatColumn(name="d", category=C.DATETIME),
            FormatColumn(name="n", category=C.NUMBER)]
    rows = [[b"\xde\xad", date(2026, 1, 2), Decimal("12345678901234567890.5")]]
    assert fmt(F.CSV, rows=rows, cols=cols, source="raw").text == "b,d,n\n0xdead,2026-01-02,12345678901234567890.5\n"
    pg_insert = fmt(F.INSERT, rows=rows, cols=cols, source="raw").text
    assert "'\\xdead'::bytea" in pg_insert and "'2026-01-02'" in pg_insert and "12345678901234567890.5)" in pg_insert
    assert "X'dead'" in fmt(F.INSERT, rows=rows, cols=cols, source="raw", dialect=LITE).text


def test_in_list():
    col = [FormatColumn(name="id", category=C.NUMBER)]
    assert fmt(F.IN_LIST, rows=[[1], [2], [1]], cols=col).text == "(1, 2)"
    assert fmt(F.IN_LIST, rows=[["a"], [None]], cols=[FormatColumn(name="s", category=C.TEXT)]).text == "('a', NULL)"
    with pytest.raises(ValidationFailed):
        fmt(F.IN_LIST)


def test_streaming_writers():
    buffer = io.StringIO()
    writer = open_writer(F.JSON, buffer, COLS, RowFormatOptions(), PG)
    writer.write([[1, "x", True]])
    writer.write([[2, "y", False]])
    writer.close()
    assert buffer.getvalue().startswith("[\n") and buffer.getvalue().rstrip().endswith("]")
    import json
    assert [r["id"] for r in json.loads(buffer.getvalue())] == [1, 2]
    with pytest.raises(ValueError):
        open_writer(F.IN_LIST, io.StringIO(), COLS, RowFormatOptions(), PG)
```

`backend/tests/sql/test_select_builder.py`:
```python
import pytest

from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.select_builder import SortKey, build_browse_select, relation_from_path

PG, LITE = Dialect.POSTGRES, Dialect.SQLITE


def path(*pairs):
    return [PathSegment(kind=k, name=n) for k, n in pairs]


PG_PATH = path(("database", "app"), ("schema", "sales"), ("folder", "tables"), ("table", "Order Items"))


def test_relation_from_path():
    assert relation_from_path(PG_PATH) == ("sales", "Order Items")
    assert relation_from_path(path(("schema", "main"), ("folder", "views"), ("view", "v"))) == ("main", "v")
    with pytest.raises(ValidationFailed):
        relation_from_path(path(("schema", "main"), ("folder", "tables")))


def test_build_browse_select():
    assert build_browse_select(PG, PG_PATH, None, []) == 'SELECT * FROM sales."Order Items"'
    sql = build_browse_select(PG, PG_PATH, " status = 'a;b'; ", [SortKey(column="Select", descending=True), SortKey(column="id")])
    assert sql == "SELECT * FROM sales.\"Order Items\"\nWHERE status = 'a;b'\nORDER BY \"Select\" DESC, id ASC"
    assert build_browse_select(LITE, path(("schema", "main"), ("folder", "tables"), ("table", "t")), "x > 1", [], count=True) == (
        "SELECT count(*) FROM main.t\nWHERE x > 1"
    )
    with pytest.raises(ValidationFailed):
        build_browse_select(PG, PG_PATH, "1=1; DROP TABLE t", [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_formatting.py tests/sql/test_select_builder.py -q`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement**

Implement both modules to the rules above. Structure `formatting.py` around one value-normalisation step and one renderer per format so copy (`format_rows`) and export (`open_writer`) share all rendering code:
- `_Binary(hex, truncated)`, `_Truncated(text)` small dataclasses; `_normalise(value, source)` → `None | bool | int | float | str | _Binary | _Truncated`.
- `_text_value(v, options) -> str` (TSV/CSV/Markdown), `_json_value(v)`, `_sql_literal(v, column, dialect, warnings) -> str`.
- Renderer classes (`_TsvWriter`, `_CsvWriter`, `_JsonWriter`, `_JsonlWriter`, `_MarkdownWriter`, `_InsertWriter`) with `write(rows)` / `close()`; `format_rows` writes into a `StringIO` with `source="encoded"|"raw"` normalisation and returns the text without a trailing newline for TSV/Markdown/INSERT/JSONL (CSV keeps the module's trailing newline, as the test shows); `open_writer` returns the same writer classes in raw mode. `in_list` is a separate function used only by `format_rows`.
- Warnings are collected per call (`list[str]`), worded `"<n> value(s) were truncated in the grid and written as NULL; export the full result for complete values"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(sql): format rows for copy/export and build table-browse SELECTs

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 8: Safety policy, profile safety level, read-only enforced in PostgreSQL

**Files:**
- Create: `backend/dbaguy_backend/services/safety.py`
- Modify: `backend/dbaguy_backend/domain/profiles.py` (`SafetyLevel`, `_ProfileCommon.safety`), `backend/dbaguy_backend/adapters/postgres/adapter.py` (`open`)
- Test: `backend/tests/test_safety.py`, `backend/tests/test_postgres_adapter.py` (one test added)

**Interfaces:**
- Consumes: `classify`, `Classification`, `StatementKind` (Task 3); `tokenize`, `TokenKind` (Task 1); `Dialect`.
- Produces:
  - `SafetyLevel = Literal["relaxed", "standard", "strict"]`; every profile gains `safety: SafetyLevel = "standard"`.
  - `GuardAction` (StrEnum `run`, `confirm`, `block`); `GuardDecision(BaseModel)`: `action`, `reasons: list[str] = []`, `classification: Classification`.
  - `SafetyPolicy().decide(sql, classification, dialect, level, read_only, confirmed=False) -> GuardDecision` and `SafetyPolicy().check(sql, dialect, level, read_only, confirmed=False) -> GuardDecision` (classifies, then decides).
  - Reason codes (stable, the frontend maps them to text): the classifier's dangerous reasons (`update_without_where`, `delete_without_where`, `drop`, `truncate`, `alter_drop`), `strict_<kind>` (e.g. `strict_write`, `strict_unknown`) and `read_only`.

Rules (spec §3.4):
1. **Read-only profile:** allowed are `read` statements and `transaction` statements that do not open a read-write transaction (no `WRITE` word: `BEGIN READ WRITE`, `START TRANSACTION READ WRITE`). Everything else (`write`, `ddl`, `session`, `unknown`) → `block` with `["read_only"]`, even when confirmed. `session` is blocked because `SET default_transaction_read_only = off`, `RESET ALL` or `DISCARD ALL` would undo the DB-level guard.
2. **Level:** `relaxed` → no confirmation; `standard` → confirm when the classification has dangerous reasons; `strict` → confirm every non-`read` statement (reasons = dangerous reasons, or `[f"strict_{kind}"]` when there are none).
3. `confirm` + `confirmed=True` → `run`. A `block` cannot be confirmed.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_safety.py`:
```python
import pytest

from dbaguy_backend.domain.profiles import SqliteProfileDraft
from dbaguy_backend.services.safety import GuardAction as A, SafetyPolicy
from dbaguy_backend.sql.dialects import Dialect

PG = Dialect.POSTGRES
policy = SafetyPolicy()


def decide(sql, level="standard", read_only=False, confirmed=False):
    decision = policy.check(sql, PG, level, read_only, confirmed)
    return decision.action, decision.reasons


@pytest.mark.parametrize(("sql", "level", "expected"), [
    ("SELECT 1", "standard", (A.RUN, [])),
    ("UPDATE t SET x = 1", "standard", (A.CONFIRM, ["update_without_where"])),
    ("UPDATE t SET x = 1 WHERE id = 1", "standard", (A.RUN, [])),
    ("DELETE FROM t", "standard", (A.CONFIRM, ["delete_without_where"])),
    ("DROP TABLE t", "standard", (A.CONFIRM, ["drop"])),
    ("DROP TABLE t", "relaxed", (A.RUN, [])),
    ("INSERT INTO t VALUES (1)", "standard", (A.RUN, [])),
    ("INSERT INTO t VALUES (1)", "strict", (A.CONFIRM, ["strict_write"])),
    ("DROP TABLE t", "strict", (A.CONFIRM, ["drop"])),
    ("frobnicate the database", "strict", (A.CONFIRM, ["strict_unknown"])),
    ("SELECT 1", "strict", (A.RUN, [])),
])
def test_levels(sql, level, expected):
    assert decide(sql, level) == expected


def test_confirmation_lets_it_run():
    assert decide("DROP TABLE t", confirmed=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "DROP TABLE t", "SET default_transaction_read_only = off", "RESET ALL",
    "BEGIN READ WRITE", "START TRANSACTION READ WRITE", "EXPLAIN ANALYZE DELETE FROM t",
    "SELECT * FROM t FOR UPDATE", "frobnicate",
])
def test_read_only_blocks_everything_that_can_write(sql):
    assert decide(sql, "relaxed", read_only=True, confirmed=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize("sql", ["SELECT 1", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN SELECT 1", "SHOW search_path"])
def test_read_only_allows_reads_and_plain_transactions(sql):
    assert decide(sql, "strict", read_only=True)[0] == A.RUN


def test_profiles_default_to_standard():
    assert SqliteProfileDraft(engine="sqlite", name="x", path="/tmp/x.db").safety == "standard"
```

Add to `backend/tests/test_postgres_adapter.py`:
```python
@requires_pg
def test_read_only_profile_is_read_only_in_the_database():
    conn = PostgresAdapter().open(make_pg_profile(read_only=True), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        result = conn.run("SHOW default_transaction_read_only")
        assert result.fetch(1) == [["on"]]
        result.close()
        with pytest.raises(StatementError) as exc:
            conn.run("CREATE TEMP TABLE ro_probe (id int)")
        assert exc.value.code == "25006"  # read_only_sql_transaction
    finally:
        conn.close()
```
(Reuse the imports already at the top of that file; add the missing ones.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_safety.py tests/test_postgres_adapter.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.services.safety'`).

- [ ] **Step 3: Implement**

`domain/profiles.py`: add `SafetyLevel = Literal["relaxed", "standard", "strict"]` next to `ProfileColor` and `safety: SafetyLevel = "standard"` on `_ProfileCommon` (after `read_only`).

`backend/dbaguy_backend/services/safety.py`:
```python
from enum import StrEnum

from pydantic import BaseModel

from ..domain.profiles import SafetyLevel
from ..sql.classifier import Classification, StatementKind, classify
from ..sql.dialects import Dialect
from ..sql.lexer import TokenKind, tokenize

READ_ONLY = "read_only"


class GuardAction(StrEnum):
    RUN = "run"
    CONFIRM = "confirm"
    BLOCK = "block"


class GuardDecision(BaseModel):
    action: GuardAction
    reasons: list[str] = []
    classification: Classification


class SafetyPolicy:
    """Decides, per statement, whether it runs, needs the user's confirmation first, or is blocked."""

    def check(
        self, sql: str, dialect: Dialect, level: SafetyLevel, read_only: bool, confirmed: bool = False
    ) -> GuardDecision:
        return self.decide(sql, classify(sql, dialect), dialect, level, read_only, confirmed)

    def decide(
        self, sql: str, classification: Classification, dialect: Dialect, level: SafetyLevel, read_only: bool,
        confirmed: bool = False,
    ) -> GuardDecision:
        if read_only and not self._allowed_when_read_only(sql, classification, dialect):
            return GuardDecision(action=GuardAction.BLOCK, reasons=[READ_ONLY], classification=classification)
        reasons = self._confirmation_reasons(classification, level)
        if reasons and not confirmed:
            return GuardDecision(action=GuardAction.CONFIRM, reasons=reasons, classification=classification)
        return GuardDecision(action=GuardAction.RUN, classification=classification)

    @staticmethod
    def _allowed_when_read_only(sql: str, classification: Classification, dialect: Dialect) -> bool:
        if classification.kind == StatementKind.READ:
            return True
        if classification.kind == StatementKind.TRANSACTION:
            return not any(t.kind == TokenKind.WORD and t.upper == "WRITE" for t in tokenize(sql, dialect))
        return False

    @staticmethod
    def _confirmation_reasons(classification: Classification, level: SafetyLevel) -> list[str]:
        if level == "relaxed":
            return []
        if level == "strict" and classification.kind != StatementKind.READ:
            return list(classification.dangerous_reasons) or [f"strict_{classification.kind}"]
        return list(classification.dangerous_reasons)
```

`adapters/postgres/adapter.py` `open()`: right after `psycopg.connect(...)` succeeds:
```python
        if profile.read_only:
            # Enforced by the server too, so a function with side effects called from a SELECT cannot write either.
            try:
                conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            except psycopg.Error as exc:
                conn.close()
                raise ConnectionFailed(f"Could not make the connection read-only: {exc}") from exc
```
and include `read_only=True` in the existing "Connected to PostgreSQL …" log line when set.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_safety.py tests/test_postgres_adapter.py -q && .venv/bin/pytest -q`
Expected: all pass. If a statement's classification differs from what a rule above needs (e.g. `RESET ALL` not classified `session`), fix the classifier's keyword table (Task 3), not the test.

- [ ] **Step 5: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck` (profiles gained `safety`).
```bash
git add backend src/lib/api
git commit -m "feat(backend): safety policy per profile and read-only enforced by PostgreSQL

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 9: Transaction control in the adapters

**Files:**
- Modify: `backend/dbaguy_backend/adapters/base.py`, `adapters/postgres/adapter.py`, `adapters/sqlite/adapter.py`, `backend/tests/fake_db.py`
- Test: `backend/tests/test_transactions.py`

**Interfaces:**
- Produces on `DatabaseConnection` (all abstract, worker thread only):
  - `autocommit() -> bool`
  - `set_autocommit(enabled: bool) -> None` — raises `StatementError(code="transaction_open")` while a transaction is open; a no-op when already in that mode.
  - `commit() -> None`, `rollback() -> None` — no-ops when no transaction is open; driver errors are translated to `StatementError`.
- PG: psycopg `conn.autocommit`; in manual mode psycopg opens a transaction before the first statement, so `run()` must treat "IDLE in manual mode" like "in a transaction" — row-returning statements go through the server-side cursor (`_CursorResult`) or buffered, **never** `_StreamResult` (abandoning a stream cancels the statement and would abort the transaction — the ① C-1 failure, now reachable through manual mode).
- SQLite: `isolation_level = None` (auto) / `"DEFERRED"` (manual: the driver issues BEGIN before the first INSERT/UPDATE/DELETE/REPLACE).
- `tests/fake_db.py` `FakeConnection` gains the same methods (state in plain attributes) plus `run()` words used by Task 10's tests; existing `rows N` / `block` behaviour is unchanged.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_transactions.py`:
```python
import sqlite3
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementError
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.profiles import SqliteProfile
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, make_pg_profile, requires_pg


def drain(result):
    rows = []
    while chunk := result.fetch(500):
        rows += chunk
    result.close()
    return rows


def scalar(conn, sql):
    return drain(conn.run(sql))[0][0]


def test_sqlite_manual_commit_mode(tmp_path):
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript("CREATE TABLE t (id INTEGER);")
    conn = SqliteAdapter().open(SqliteProfile(id=uuid4(), name="t", engine="sqlite", path=str(db)), None, None, None)
    try:
        assert conn.autocommit()
        conn.set_autocommit(False)
        assert not conn.autocommit()
        drain(conn.run("INSERT INTO t VALUES (1)"))
        assert conn.in_transaction()
        with pytest.raises(StatementError) as exc:
            conn.set_autocommit(True)
        assert exc.value.code == "transaction_open"
        conn.rollback()
        assert not conn.in_transaction() and scalar(conn, "SELECT count(*) FROM t") == 0
        drain(conn.run("INSERT INTO t VALUES (2)"))
        conn.commit()
        assert scalar(conn, "SELECT max(id) FROM t") == 2
        conn.commit()  # nothing open: a no-op
        conn.set_autocommit(True)
        assert conn.autocommit()
    finally:
        conn.close()


@requires_pg
def test_postgres_manual_commit_mode():
    conn = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        drain(conn.run("CREATE TEMP TABLE tx_probe (id int)"))
        conn.set_autocommit(False)
        drain(conn.run("INSERT INTO tx_probe VALUES (1)"))
        assert conn.in_transaction()
        with pytest.raises(StatementError) as exc:
            conn.set_autocommit(True)
        assert exc.value.code == "transaction_open"
        conn.rollback()
        assert int(scalar(conn, "SELECT count(*) FROM tx_probe")) == 0
        drain(conn.run("INSERT INTO tx_probe VALUES (2)"))
        conn.commit()
        assert int(scalar(conn, "SELECT max(id) FROM tx_probe")) == 2
        conn.rollback()  # the SELECT above opened a transaction in manual mode

        # A partly read large result in manual mode must not abort the transaction (no streaming here).
        result = conn.run("SELECT g FROM generate_series(1, 200000) g")
        assert len(result.fetch(10)) == 10
        result.close()
        assert conn.in_transaction() and not conn.transaction_failed()
        conn.rollback()
        conn.set_autocommit(True)
        assert conn.autocommit() and not conn.in_transaction()
    finally:
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_transactions.py -q`
Expected: FAIL (`AttributeError: ... 'autocommit'`).

- [ ] **Step 3: Implement**

`adapters/base.py` — add to `DatabaseConnection`:
```python
    @abstractmethod
    def autocommit(self) -> bool: ...

    @abstractmethod
    def set_autocommit(self, enabled: bool) -> None:
        """Switch between auto-commit and manual commit. Raises StatementError(code="transaction_open") while a
        transaction is open; a no-op when the mode is already `enabled`."""

    @abstractmethod
    def commit(self) -> None:
        """Commit the open transaction; a no-op when none is open."""

    @abstractmethod
    def rollback(self) -> None:
        """Roll back the open transaction; a no-op when none is open."""
```
and a module-level helper both adapters use:
```python
TRANSACTION_OPEN = "transaction_open"


def transaction_open_error() -> StatementError:
    return StatementError("Commit or roll back the open transaction first", code=TRANSACTION_OPEN)
```

`PostgresConnection`:
```python
    def autocommit(self) -> bool:
        return self._conn.autocommit

    def set_autocommit(self, enabled: bool) -> None:
        if self._conn.autocommit == enabled:
            return
        if self._conn.info.transaction_status != pq.TransactionStatus.IDLE:
            raise transaction_open_error()
        self._conn.autocommit = enabled

    def commit(self) -> None:
        try:
            self._conn.commit()  # psycopg skips it when the connection is idle
        except psycopg.Error as exc:
            raise self.translate_error(exc, "COMMIT") from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except psycopg.Error as exc:
            raise self.translate_error(exc, "ROLLBACK") from exc
```
`run()`: replace the status branch with
```python
            if _likely_returns_rows(sql):
                status = self._conn.info.transaction_status
                if status == pq.TransactionStatus.IDLE and self._conn.autocommit:
                    return self._run_streamed(sql)  # no transaction: cancelling an abandoned stream is harmless
                # In a transaction, or about to open one (manual mode): never stream (see C-1).
                if status in (pq.TransactionStatus.IDLE, pq.TransactionStatus.INTRANS) and _declarable(sql):
                    return _CursorResult(self, self._server_cursor(), sql)
```
(keep the comment about SHOW/EXPLAIN/RETURNING being read in full).

`SqliteConnection`:
```python
    def autocommit(self) -> bool:
        return self._conn.isolation_level is None

    def set_autocommit(self, enabled: bool) -> None:
        if self.autocommit() == enabled:
            return
        if self._conn.in_transaction:
            raise transaction_open_error()
        self._conn.isolation_level = None if enabled else "DEFERRED"

    def commit(self) -> None:
        try:
            self._conn.commit()
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error as exc:
            raise _translate(exc) from exc
```
Log at INFO in both adapters' `set_autocommit` ("Auto-commit on/off") — commit/rollback are logged by the worker (Task 10).

`tests/fake_db.py` `FakeConnection` — add state and methods, and widen `run()`:
```python
    # in __init__:
        self.tx = False
        self.auto = True
        self.commits = 0
        self.rollbacks = 0

    def run(self, sql: str) -> StatementResult:
        """`rows N` → N rows; `block` → waits for cancel(); `fail MSG` → StatementError; `begin` opens a
        transaction; anything else → an empty result."""
        self.runs.append(sql)
        word, _, arg = sql.partition(" ")
        if word == "block":
            self.blocked.set()
            assert self._unblock.wait(10), "block was never cancelled"
            self._unblock.clear()
            raise StatementCancelled()
        if word == "fail":
            raise StatementError(arg or "boom", code="42000")
        if word == "begin":
            self.tx = True
        return FakeResult(int(arg) if word == "rows" else 0)

    def in_transaction(self) -> bool:
        return self.tx

    def autocommit(self) -> bool:
        return self.auto

    def set_autocommit(self, enabled: bool) -> None:
        if self.tx and enabled != self.auto:
            raise transaction_open_error()
        self.auto = enabled

    def commit(self) -> None:
        self.commits += 1
        self.tx = False

    def rollback(self) -> None:
        self.rollbacks += 1
        self.tx = False

    def schema_snapshot(self):
        raise NotImplementedError
```
(`schema_snapshot` may already have been added by Task 4; keep one.) Clearing `_unblock` after a cancel lets one fake connection serve several `block` statements in one test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_transactions.py tests/test_postgres_sessions.py tests/test_session_worker.py -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(adapters): auto-commit switch, commit and rollback for PostgreSQL and SQLite

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 10: Execution protocol — confirmation, timeout, transactions, session state, history and schema hooks

**Files:**
- Modify: `backend/dbaguy_backend/domain/execution.py`, `backend/dbaguy_backend/sessions/worker.py`, `backend/dbaguy_backend/sessions/manager.py` (`attach` replays the state), `backend/tests/test_api.py` / `tests/test_sessions.py` (event-order expectations that now include `session_state`)
- Test: `backend/tests/test_worker_protocol.py`, `backend/tests/test_postgres_sessions.py` (timeout test added)

**Interfaces:**
- Consumes: `GuardDecision`, `GuardAction`, `SafetyPolicy` (Task 8); `Classification`, `StatementKind` (Task 3); connection transaction methods (Task 9); `Dialect`.
- Produces — protocol (`domain/execution.py`):
  - `ExecuteCommand` + `confirmed: bool = False`, `timeout_s: float | None = Field(default=None, ge=0, le=86_400)` (None or 0 = no timeout), `history: bool = True`.
  - `CommitCommand(type="commit", request_id)`, `RollbackCommand(type="rollback", request_id)`, `SetAutocommitCommand(type="set_autocommit", request_id, enabled: bool)` — added to `ClientCommand`.
  - `ConfirmationRequiredEvent(type="confirmation_required", request_id, reasons: list[str], classification: Classification)`.
  - `SessionStateEvent(type="session_state", autocommit: bool, in_transaction: bool, transaction_failed: bool, transaction_started_at: datetime | None = None, idle_seconds: float | None = None)` — no `request_id`.
  - `FinishedEvent` + `timed_out: bool = False`, `skipped: bool = False`.
  - Session-level messages (idle-transaction warnings, Task 13) are `MessageEvent` with `request_id=""`.
- Produces — worker (`sessions/worker.py`):
  - `ExecutionRecord` (frozen dataclass): `request_id, sql, started_at: datetime, elapsed_ms: int, rows: int, status: Literal["ok", "error", "cancelled", "timeout"], error: str | None`.
  - `WorkerHooks` (frozen dataclass) with defaults that run everything and record nothing: `dialect: Dialect = Dialect.SQLITE`, `check: Callable[[str, bool], GuardDecision]` (sql, confirmed), `on_record: Callable[[ExecutionRecord], None]`, `on_schema_change: Callable[[], None]`.
  - `SessionWorker.configure(hooks: WorkerHooks)` — called once before the session is registered.
  - `SessionWorker.replay_state()` — sends the last known `session_state` (with a fresh `idle_seconds`) to the current emitter; any thread.
  - `SessionWorker.check_idle_transaction(warn_after_s: float, rollback_after_s: float)` — any thread; Task 13 calls it from housekeeping (0 disables either).

Behaviour:
- Every request still ends with exactly one `finished`; immediately **before** every `finished` the worker sends `session_state`, so a client that has seen `finished` always has the current state. The state is also computed once right after the connection opens, before `start()` returns (cached; no emitter is attached yet), and replayed by `SessionManager.attach`.
- **Policy before running** (after `started`): `block` → `error(code="read_only", message="This connection is read-only: <kind> statements are not allowed")` + `finished`; `confirm` without `confirmed` → `confirmation_required` + `finished(skipped=true)`. Nothing reaches the connection. Every execute is re-checked (a confirmation is never remembered).
- **Timeout** covers `run()` and the first page: a `threading.Timer` armed with `timeout_s` and cancelled when the request pauses or ends. When it fires while that request is still the running one it marks the request `timed_out`, sets the cancel flag and cancels the connection. The request then ends with message `"Timed out after {timeout_s:g} s"` (warning) and `finished(cancelled=true, timed_out=true)`.
- **Transaction commands** run on the worker thread after discarding any paused result: `started` → driver call → `error` on `StatementError` → `finished` (→ `session_state`). Logged at INFO.
- **History:** when an execute that passed the policy ends (`ok`, `error`, `cancelled`, `timeout`) and `history=True`, call `on_record` once. `elapsed_ms` in the record is the time until the first pause or the end (reading time is not query time); `rows` = rows sent + affected rows; `error` is the full message (the history service truncates). Hook failures are logged at WARNING and never affect execution.
- **Schema invalidation:** a `ddl` statement that ended `ok` calls `on_schema_change` — immediately when no transaction is open, otherwise once the transaction ends (commit, rollback, COMMIT statement…; checked whenever the state is refreshed), because other connections cannot see uncommitted DDL.
- **Transaction start time:** set to the `started_at` of the request after which the state first shows `in_transaction`, cleared when it ends.
- **Idle transaction:** idle = in a transaction and no request running (a paused result counts as idle — it still holds the transaction open). `check_idle_transaction` sends one warning per idle stretch (`"Transaction idle for N min — commit or roll back to release its locks"`, request_id `""`) and, when `rollback_after_s > 0` and exceeded, queues a rollback that only happens if nothing ran in between; it then sends a warning `"Rolled back the transaction after N min idle"` and `session_state`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_worker_protocol.py`:
```python
import time

import pytest

from dbaguy_backend.domain.execution import (
    CommitCommand, ExecuteCommand, FetchMoreCommand, RollbackCommand, SetAutocommitCommand,
)
from dbaguy_backend.services.safety import GuardAction, GuardDecision, SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.classifier import Classification, StatementKind
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
from tests.test_sessions import Recorder

policy = SafetyPolicy()


def standard(read_only=False, level="standard"):
    return lambda sql, confirmed: policy.check(sql, Dialect.SQLITE, level, read_only, confirmed)


@pytest.fixture
def make_worker():
    workers = []

    def make(**hooks):
        lease = FakeLease()
        worker = SessionWorker("fake", lambda: lease)
        worker.start()
        worker.configure(WorkerHooks(**hooks))
        recorder = Recorder()
        worker.set_emitter(recorder)
        workers.append(worker)
        return worker, lease.connection, recorder

    yield make
    for worker in workers:
        worker.close()


def run(worker, recorder, rid, sql, **fields):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql, **fields))
    return recorder.wait_for(lambda e: e.type == "finished" and e.request_id == rid)


def test_confirmation_is_required_every_time_until_confirmed(make_worker):
    worker, conn, rec = make_worker(check=standard())
    done = run(worker, rec, "a", "DELETE FROM t")
    assert done.skipped and conn.runs == []
    [confirm] = rec.of("a", "confirmation_required")
    assert confirm.reasons == ["delete_without_where"] and confirm.classification.kind == "write"
    run(worker, rec, "b", "DELETE FROM t", confirmed=True)
    assert conn.runs == ["DELETE FROM t"]
    assert run(worker, rec, "c", "DELETE FROM t").skipped  # replaying without confirmation is checked again
    assert conn.runs == ["DELETE FROM t"]


def test_read_only_blocks_before_reaching_the_connection(make_worker):
    worker, conn, rec = make_worker(check=standard(read_only=True))
    run(worker, rec, "a", "INSERT INTO t VALUES (1)", confirmed=True)
    [error] = rec.of("a", "error")
    assert error.code == "read_only" and conn.runs == []
    run(worker, rec, "b", "SELECT 1")
    assert conn.runs == ["SELECT 1"]


def test_timeout_cancels_the_statement(make_worker):
    worker, conn, rec = make_worker()
    done = run(worker, rec, "a", "block", timeout_s=0.2)
    assert done.cancelled and done.timed_out and conn.cancel_calls == 1
    assert rec.of("a", "message")[-1].text == "Timed out after 0.2 s"
    assert not run(worker, rec, "b", "rows 1").timed_out  # the session keeps working


def test_timeout_stops_counting_once_the_first_page_is_out(make_worker):
    worker, conn, rec = make_worker()
    worker.submit(ExecuteCommand(request_id="a", sql="rows 50", page_size=10, timeout_s=0.2))
    rec.wait_for(lambda e: e.type == "result_set_end" and e.has_more)
    time.sleep(0.4)
    assert conn.cancel_calls == 0
    worker.submit(FetchMoreCommand(request_id="a", count=100))
    assert not rec.wait_for(lambda e: e.type == "finished" and e.request_id == "a").timed_out


def test_history_records(make_worker):
    records = []
    worker, _, rec = make_worker(check=standard(), on_record=records.append)
    run(worker, rec, "ok", "rows 3")
    run(worker, rec, "err", "fail no such table")
    run(worker, rec, "quiet", "rows 1", history=False)
    run(worker, rec, "skip", "DELETE FROM t")  # needs confirmation: never ran
    run(worker, rec, "slow", "block", timeout_s=0.1)
    assert [(r.request_id, r.status, r.rows) for r in records] == [
        ("ok", "ok", 3), ("err", "error", 0), ("slow", "timeout", 0),
    ]
    assert records[1].error == "no such table" and records[0].sql == "rows 3"


def ddl_aware(sql, confirmed):
    kind = StatementKind.DDL if sql.startswith("ddl") else StatementKind.READ
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=kind))


def test_schema_change_is_reported_once_other_connections_can_see_it(make_worker):
    changes = []
    worker, _, rec = make_worker(check=ddl_aware, on_schema_change=lambda: changes.append(1))
    run(worker, rec, "a", "ddl create")
    assert len(changes) == 1
    run(worker, rec, "b", "begin")
    run(worker, rec, "c", "ddl alter")
    assert len(changes) == 1  # still inside the transaction
    worker.submit(CommitCommand(request_id="d"))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "d")
    assert len(changes) == 2


def test_transaction_commands_and_session_state(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    state = [e for e in rec.events if e.type == "session_state"][-1]
    assert state.in_transaction and state.transaction_started_at is not None and state.autocommit
    worker.submit(SetAutocommitCommand(request_id="b", enabled=False))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "b")
    assert rec.of("b", "error")[0].code == "transaction_open"
    worker.submit(RollbackCommand(request_id="c"))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "c")
    assert conn.rollbacks == 1
    worker.submit(SetAutocommitCommand(request_id="d", enabled=False))
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "d")
    state = [e for e in rec.events if e.type == "session_state"][-1]
    assert not state.in_transaction and not state.autocommit and state.transaction_started_at is None


def test_replay_state_sends_the_cached_state(make_worker):
    worker, _, _ = make_worker()
    late = Recorder()
    worker.set_emitter(late)
    worker.replay_state()
    assert late.wait_for(lambda e: e.type == "session_state").in_transaction is False


def test_idle_transaction_warning_then_rollback(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    time.sleep(0.15)
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0)
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0)
    warnings = [e for e in rec.events if e.type == "message" and e.request_id == ""]
    assert len(warnings) == 1 and "idle" in warnings[0].text
    worker.check_idle_transaction(warn_after_s=0.1, rollback_after_s=0.1)
    rec.wait_for(lambda e: e.type == "message" and e.text.startswith("Rolled back"))
    assert conn.rollbacks == 1 and not conn.tx
    assert [e for e in rec.events if e.type == "session_state"][-1].in_transaction is False


def test_no_idle_rollback_when_something_ran_in_between(make_worker):
    worker, conn, rec = make_worker()
    run(worker, rec, "a", "begin")
    time.sleep(0.15)
    conn.blocked.clear()
    worker.submit(ExecuteCommand(request_id="b", sql="block"))
    assert conn.blocked.wait(5)
    worker.check_idle_transaction(warn_after_s=0, rollback_after_s=0.1)  # running: not idle
    worker.cancel("b")
    rec.wait_for(lambda e: e.type == "finished" and e.request_id == "b")
    assert conn.rollbacks == 0
```

Add to `backend/tests/test_postgres_sessions.py` (uses the existing `pg_worker` fixture; adapt the call style to it):
```python
def test_statement_timeout_on_postgres(pg_worker):
    worker, recorder = pg_worker
    worker.submit(ExecuteCommand(request_id="t", sql="SELECT pg_sleep(5)", timeout_s=0.3))
    done = recorder.wait_for(lambda e: e.type == "finished" and e.request_id == "t")
    assert done.timed_out and done.elapsed_ms < 3000
    worker.submit(ExecuteCommand(request_id="u", sql="SELECT 1"))
    assert not recorder.wait_for(lambda e: e.type == "finished" and e.request_id == "u").cancelled
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_worker_protocol.py -q`
Expected: FAIL (`ImportError: cannot import name 'CommitCommand'`).

- [ ] **Step 3: Extend the protocol models**

In `domain/execution.py` add the fields, commands and events listed under Interfaces (import `Classification` from `..sql.classifier` and `datetime`), add the commands to `ClientCommand` and `ConfirmationRequiredEvent | SessionStateEvent` to `ServerEvent`.

- [ ] **Step 4: Restructure the worker around a per-request record**

Replace `_OpenExecution.request_id/started` with a reference to a new `_Request`, and route every end of an execute through one method. New module-level pieces in `sessions/worker.py`:
```python
@dataclass(frozen=True)
class ExecutionRecord:
    request_id: str
    sql: str
    started_at: datetime
    elapsed_ms: int
    rows: int
    status: Literal["ok", "error", "cancelled", "timeout"]
    error: str | None = None


def _run_everything(sql: str, confirmed: bool) -> GuardDecision:
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))


def _ignore(*_: object) -> None:
    return None


@dataclass(frozen=True)
class WorkerHooks:
    """How a worker reaches the rest of the app. The defaults run everything and record nothing."""

    dialect: Dialect = Dialect.SQLITE
    check: Callable[[str, bool], GuardDecision] = _run_everything
    on_record: Callable[[ExecutionRecord], None] = _ignore
    on_schema_change: Callable[[], None] = _ignore


@dataclass
class _Request:
    """One execute (or export) request, from `started` to `finished`."""

    request_id: str
    sql: str
    history: bool = True
    timeout_s: float | None = None
    started: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    kind: StatementKind = StatementKind.UNKNOWN
    rows: int = 0
    first_page_ms: int | None = None  # set at the first pause: reading time is not query time
    timed_out: bool = False

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


@dataclass
class _OpenExecution:
    req: _Request
    result: StatementResult
    index: int = 0
    sent: int = 0
    columns_sent: bool = False
    peeked: list[list[Any]] = field(default_factory=list)

    @property
    def request_id(self) -> str:
        return self.req.request_id
```
New worker state in `__init__` (guarded by `_state_lock` where other threads read it): `self._hooks = WorkerHooks()`, `self._state: SessionStateEvent | None = None`, `self._idle_since = time.monotonic()`, `self._idle_warned = False`, `self._tx_started_at: datetime | None = None`, `self._ddl_pending = False` (worker thread only).

Core methods (adapt names to the existing code; keep its locking rules and comments):
```python
    def configure(self, hooks: WorkerHooks) -> None:
        """Set before the session is registered, so no command can run with the defaults."""
        self._hooks = hooks

    def _execute(self, cmd: ExecuteCommand) -> None:
        self._discard_open()
        req = _Request(request_id=cmd.request_id, sql=cmd.sql, history=cmd.history, timeout_s=cmd.timeout_s or None)
        skipped = self._begin_request(cmd.request_id)
        self._send(StartedEvent(request_id=cmd.request_id))
        if skipped is not None:
            self._send(MessageEvent(request_id=cmd.request_id, severity=MessageSeverity.WARNING, text=skipped))
            self._send_finished(cmd.request_id, req.elapsed_ms(), cancelled=True)
            return
        try:
            if not self._passes_policy(req, cmd.confirmed):
                return
            logger.debug("[{}] execute {}: {}", self.name, cmd.request_id, cmd.sql[:2000])
            with self._timeout(req):
                result = self.connection.run(cmd.sql)
                self._open_exec = _OpenExecution(req=req, result=result)
                self._pump(self._open_exec, cmd.page_size)
        except StatementError as exc:
            self._fail(req, exc)
        except Exception as exc:
            logger.exception("Unexpected error executing on {}", self.name)
            self._fail(req, StatementError(f"Internal error: {exc}"))
        finally:
            self._set_running(None)

    def _passes_policy(self, req: _Request, confirmed: bool) -> bool:
        decision = self._hooks.check(req.sql, confirmed)
        req.kind = decision.classification.kind
        if decision.action == GuardAction.BLOCK:
            logger.info("[{}] blocked {} statement {} (read-only)", self.name, req.kind, req.request_id)
            self._send(ErrorEvent(
                request_id=req.request_id, code="read_only",
                message=f"This connection is read-only: {req.kind} statements are not allowed",
            ))
            self._send_finished(req.request_id, req.elapsed_ms())
            return False
        if decision.action == GuardAction.CONFIRM:
            self._send(ConfirmationRequiredEvent(
                request_id=req.request_id, reasons=decision.reasons, classification=decision.classification,
            ))
            self._send_finished(req.request_id, req.elapsed_ms(), skipped=True)
            return False
        return True

    @contextmanager
    def _timeout(self, req: _Request) -> Iterator[None]:
        """Cancel `req` if it is still the running request after `req.timeout_s` (until its first pause or end)."""
        if not req.timeout_s:
            yield
            return
        timer = threading.Timer(req.timeout_s, self._time_out, args=(req,))
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def _time_out(self, req: _Request) -> None:  # timer thread
        with self._state_lock:
            if self._running_request != req.request_id:
                return
            logger.info("Request {} on {} timed out after {:g} s", req.request_id, self.name, req.timeout_s)
            req.timed_out = True
            self._cancel_requested = True
            self._cancel_connection()

    @staticmethod
    def _cancel_text(req: _Request, default: str) -> str:
        return f"Timed out after {req.timeout_s:g} s" if req.timed_out else default

    def _complete(self, req: _Request, *, error: StatementError | None = None, cancelled: bool = False) -> None:
        """End a request that ran: history record, then session state (+ schema invalidation), then `finished` —
        so whoever has seen `finished` can rely on all three."""
        timed_out = cancelled and req.timed_out
        if req.history:
            status = "error" if error else "timeout" if timed_out else "cancelled" if cancelled else "ok"
            self._notify("history", self._hooks.on_record, ExecutionRecord(
                request_id=req.request_id, sql=req.sql, started_at=req.started_at,
                elapsed_ms=req.first_page_ms if req.first_page_ms is not None else req.elapsed_ms(),
                rows=req.rows, status=status, error=error.message if error else None,
            ))
        self._send_finished(req.request_id, req.elapsed_ms(), cancelled=cancelled, timed_out=timed_out,
                            began_at=req.started_at, ddl_done=error is None and not cancelled and req.kind == StatementKind.DDL)

    def _notify(self, what: str, hook: Callable[..., None], *args: object) -> None:
        try:
            hook(*args)
        except Exception:
            logger.warning("The {} hook of {} failed", what, self.name, exc_info=True)
```
`_send_finished(request_id, elapsed_ms, *, cancelled=False, timed_out=False, skipped=False, began_at=None, ddl_done=False)` first calls `self._refresh_state(began_at, ddl_done)` and then sends `FinishedEvent` (taking `in_transaction`/`transaction_failed` from the same refresh):
```python
    def _refresh_state(self, began_at: datetime | None = None, ddl_done: bool = False) -> None:
        """Recompute and send `session_state` (worker thread); report DDL once it is visible to others."""
        try:
            connection = self.connection
            state = (connection.autocommit(), connection.in_transaction(), connection.transaction_failed())
        except Exception:
            return  # the connection is gone; the next request reports it
        autocommit, in_transaction, failed = state
        self._ddl_pending = self._ddl_pending or ddl_done
        with self._state_lock:
            if not in_transaction:
                self._tx_started_at = None
            elif self._tx_started_at is None:
                self._tx_started_at = began_at or datetime.now(UTC)
            self._idle_since = time.monotonic()
            self._idle_warned = False
            self._state = SessionStateEvent(
                autocommit=autocommit, in_transaction=in_transaction, transaction_failed=failed,
                transaction_started_at=self._tx_started_at,
            )
            event = self._state
        if self._ddl_pending and not in_transaction:
            self._ddl_pending = False
            self._notify("schema change", self._hooks.on_schema_change)
        self._send(event)
```
Call `_refresh_state()` once in `_run` right after the opener succeeds and **before** `self._opened.set()` (the emitter still discards; the state is cached for `replay_state`, and `start()` returning guarantees it exists).

Adapt the rest:
- `_pump`: add `ex.req.rows += len(rows)` and `ex.req.rows += affected`; in `_pause` set `ex.req.first_page_ms = ex.req.first_page_ms or ex.req.elapsed_ms()` before sending `result_set_end`.
- `_fetch_more`: errors go to `self._fail(execution.req, exc)`; the "no open result" path uses `_send_finished(cmd.request_id, 0)`.
- `_discard(request_id, cancelled)`: the message text is `self._cancel_text(execution.req, "Query cancelled")`.
- `_finish(ex, cancelled)`: close the result, then `self._complete(ex.req, cancelled=cancelled)`.
- `_fail(req, exc)`: close any open result; cancelled → warning message `self._cancel_text(req, exc.message)` and `_complete(req, cancelled=True)`; otherwise `ErrorEvent` and `_complete(req, error=exc)`.

Transaction commands, state replay and idle checks:
```python
    def submit(self, command: BaseModel) -> None:
        match command:
            ...  # existing cases
            case CommitCommand() | RollbackCommand() | SetAutocommitCommand():
                self._put(lambda: self._transaction_command(command))

    def _transaction_command(self, cmd: CommitCommand | RollbackCommand | SetAutocommitCommand) -> None:
        self._discard_open()
        started = time.monotonic()
        self._send(StartedEvent(request_id=cmd.request_id))
        try:
            match cmd:
                case CommitCommand():
                    self.connection.commit()
                case RollbackCommand():
                    self.connection.rollback()
                case SetAutocommitCommand():
                    self.connection.set_autocommit(cmd.enabled)
            logger.info("[{}] {} done", self.name, cmd.type)
        except StatementError as exc:
            self._send(ErrorEvent(request_id=cmd.request_id, message=exc.message, code=exc.code))
        except Exception as exc:
            logger.exception("Unexpected error in {} on {}", cmd.type, self.name)
            self._send(ErrorEvent(request_id=cmd.request_id, message=f"Internal error: {exc}"))
        self._send_finished(cmd.request_id, int((time.monotonic() - started) * 1000))

    def _idle_seconds_locked(self) -> float | None:
        if self._state is None or not self._state.in_transaction or self._running_request is not None:
            return None
        return time.monotonic() - self._idle_since

    def replay_state(self) -> None:
        with self._state_lock:
            state, idle = self._state, self._idle_seconds_locked()
        if state is not None:
            self._send(state.model_copy(update={"idle_seconds": idle}))

    def check_idle_transaction(self, warn_after_s: float, rollback_after_s: float) -> None:
        with self._state_lock:
            idle = self._idle_seconds_locked()
            if idle is None:
                return
            warn = 0 < warn_after_s <= idle and not self._idle_warned
            self._idle_warned = self._idle_warned or warn
            idle_since = self._idle_since
        if warn:
            self._send(MessageEvent(request_id="", severity=MessageSeverity.WARNING,
                                    text=f"Transaction idle for {idle / 60:.0f} min — commit or roll back to release its locks"))
        if 0 < rollback_after_s <= idle:
            try:
                self._put(lambda: self._idle_rollback(idle_since, rollback_after_s))
            except SessionBusy:
                pass  # closing anyway

    def _idle_rollback(self, idle_since: float, after_s: float) -> None:
        with self._state_lock:
            if self._idle_since != idle_since or self._running_request is not None:
                return  # something ran meanwhile: the transaction is not idle any more
        self._discard_open()
        try:
            self.connection.rollback()
        except StatementError as exc:
            logger.warning("Idle rollback failed on {}: {}", self.name, exc.message)
            return
        logger.info("Rolled back an idle transaction on {}", self.name)
        self._send(MessageEvent(request_id="", severity=MessageSeverity.WARNING,
                                text=f"Rolled back the transaction after {after_s / 60:g} min idle"))
        self._refresh_state()
```
Note: `_discard_open()` in `_idle_rollback` finishes a paused result, which refreshes the state and resets `_idle_since`; the rollback must still happen, so compare `idle_since` **before** discarding (as written).

`SessionManager.attach`: after `entry.worker.set_emitter(emit)` call `entry.worker.replay_state()`.

- [ ] **Step 5: Update event-order expectations in existing tests**

`session_state` now precedes every `finished` and arrives first on every WebSocket attach. Update tests in `tests/test_api.py` and `tests/test_sessions.py` that read events in strict order (skip `session_state` where the test is not about it, e.g. a helper `receive_until(ws, type)`); do not weaken what they assert otherwise.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_worker_protocol.py tests/test_session_worker.py tests/test_sessions.py tests/test_postgres_sessions.py tests/test_api.py -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 7: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck`. If the ① console's event handling no longer typechecks because the event union grew, add the new event types as ignored cases there — no behaviour change (②c replaces the console).
```bash
git add backend src
git commit -m "feat(sessions): confirmation, timeout, transaction commands, session state and history/schema hooks

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 11: Export a full result to a file

**Files:**
- Modify: `backend/dbaguy_backend/domain/execution.py` (imports `domain/formats.py` from Task 7 — never `sql/formatting.py`, which imports `domain/execution.py`), `backend/dbaguy_backend/sessions/worker.py`
- Test: `backend/tests/test_export.py`

**Interfaces:**
- Consumes: `open_writer` (Task 7), `_Request`, `WorkerHooks`, `_complete`, `_timeout`-free request lifecycle (Task 10).
- Produces:
  - `ExportCommand(type="export", request_id, sql, format: FormatKind, path: str, options: RowFormatOptions = RowFormatOptions())` in `ClientCommand`.
  - `ProgressEvent(type="progress", request_id, rows: int)` in `ServerEvent` — after every 10,000 rows written.
- Behaviour:
  - Queued like an execute (same queued/cancel bookkeeping — generalise `_submit_execute(cmd)` to take the runner).
  - Discards any paused result first (one connection: the grid's open result ends with its `finished`).
  - Checks, in order, each failing with `error` + `finished` and nothing written: `format == in_list` → `code="unsupported_format"`; `path` not absolute or its parent directory missing → `code="invalid_path"`; the policy decision is `block` → `read_only`; the classification is not `read` → `code="export_requires_select"`.
  - Runs `sql` with `connection.run()`; no columns → `export_requires_select`. Writes **raw** rows (`fetch(1000)` chunks) through `open_writer(format, fp, columns, options, hooks.dialect)` into `<path>.part` (UTF-8, `newline=""`), then `os.replace` onto `path`; on error or cancel the `.part` file is removed and `path` is untouched.
  - Honours cancel between chunks (check `_cancel_requested` under the state lock → raise `StatementCancelled`) and from the driver.
  - Success: info message `"Exported {n:,} rows to {path}"`, `finished`. Export requests are not written to history (`history=False`) and have no timeout.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_export.py`:
```python
import csv
import sqlite3
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand
from dbaguy_backend.domain.formats import FormatKind
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.safety import SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
from tests.test_sessions import Recorder

BIG = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5000000) SELECT x, 'row ' || x FROM c"


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "e.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT, data BLOB)")
    raw.executemany("INSERT INTO t (note, data) VALUES (?, ?)", [(f"n{i}", b"\x01\x02") for i in range(25_000)])
    raw.commit()
    raw.close()
    profile = SqliteProfile(id=uuid4(), name="e", engine="sqlite", path=str(db))
    # Opened by the worker thread itself: sqlite3 connections refuse use from another thread.
    worker = SessionWorker("export", lambda: FakeLease(SqliteAdapter().open(profile, None, None, None)))
    worker.start()
    policy = SafetyPolicy()
    worker.configure(WorkerHooks(
        dialect=Dialect.SQLITE,
        check=lambda sql, confirmed: policy.check(sql, Dialect.SQLITE, "standard", False, confirmed),
    ))
    recorder = Recorder()
    worker.set_emitter(recorder)
    yield worker, recorder, tmp_path
    worker.close()


def export(worker, recorder, rid, sql, path, fmt=FormatKind.CSV):
    worker.submit(ExportCommand(request_id=rid, sql=sql, format=fmt, path=str(path)))
    return recorder.wait_for(lambda e: e.type == "finished" and e.request_id == rid)


def test_export_csv_with_progress(session):
    worker, rec, tmp = session
    target = tmp / "out.csv"
    done = export(worker, rec, "x", "SELECT id, note, data FROM t ORDER BY id", target)
    assert not done.cancelled and [p.rows for p in rec.of("x", "progress")] == [10_000, 20_000]
    rows = list(csv.reader(target.open(encoding="utf-8", newline="")))
    assert rows[0] == ["id", "note", "data"] and rows[1] == ["1", "n0", "0x0102"] and len(rows) == 25_001
    assert "Exported 25,000 rows" in rec.of("x", "message")[-1].text
    assert not (tmp / "out.csv.part").exists()


@pytest.mark.parametrize(("sql", "fmt", "path", "code"), [
    ("DELETE FROM t", FormatKind.CSV, "out.csv", "export_requires_select"),
    ("SELECT 1", FormatKind.IN_LIST, "out.txt", "unsupported_format"),
    ("SELECT 1", FormatKind.CSV, "relative.csv", "invalid_path"),
    ("SELECT 1", FormatKind.CSV, "missing-dir/out.csv", "invalid_path"),
])
def test_export_rejections(session, sql, fmt, path, code):
    worker, rec, tmp = session
    target = path if path == "relative.csv" else tmp / path
    export(worker, rec, "r", sql, target, fmt)
    assert rec.of("r", "error")[0].code == code
    assert not (tmp / path).exists()


def test_cancel_removes_the_partial_file(session):
    worker, rec, tmp = session
    target = tmp / "big.jsonl"
    worker.submit(ExportCommand(request_id="c", sql=BIG, format=FormatKind.JSONL, path=str(target)))
    rec.wait_for(lambda e: e.type == "progress" and e.request_id == "c")
    worker.cancel("c")
    assert rec.wait_for(lambda e: e.type == "finished" and e.request_id == "c").cancelled
    assert not target.exists() and not (tmp / "big.jsonl.part").exists()


def test_export_ends_a_paused_result(session):
    worker, rec, tmp = session
    worker.submit(ExecuteCommand(request_id="grid", sql="SELECT * FROM t", page_size=10))
    rec.wait_for(lambda e: e.type == "result_set_end" and e.has_more)
    export(worker, rec, "x", "SELECT id FROM t", tmp / "ids.csv")
    assert rec.of("grid", "finished")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_export.py -q`
Expected: FAIL (`ImportError: cannot import name 'ExportCommand'`).

- [ ] **Step 3: Implement**

Add `ExportCommand`/`ProgressEvent` to `domain/execution.py`.

Worker:
```python
_EXPORT_CHUNK = 1000
_PROGRESS_EVERY = 10_000

    def _export(self, cmd: ExportCommand) -> None:
        self._discard_open()
        req = _Request(request_id=cmd.request_id, sql=cmd.sql, history=False)
        skipped = self._begin_request(cmd.request_id)
        self._send(StartedEvent(request_id=cmd.request_id))
        if skipped is not None:
            self._send(MessageEvent(request_id=cmd.request_id, severity=MessageSeverity.WARNING, text=skipped))
            self._send_finished(cmd.request_id, req.elapsed_ms(), cancelled=True)
            return
        target = Path(cmd.path).expanduser()
        part = target.with_name(target.name + ".part")
        try:
            self._check_export(cmd, target, req)
            logger.info("[{}] export {} as {} to {}", self.name, cmd.request_id, cmd.format, target)
            written = self._write_export(cmd, part, req)
            os.replace(part, target)
            self._send(MessageEvent(request_id=cmd.request_id, severity=MessageSeverity.INFO,
                                    text=f"Exported {written:,} rows to {target}"))
            self._complete(req)
        except StatementError as exc:
            part.unlink(missing_ok=True)
            self._fail(req, exc)
        except OSError as exc:
            part.unlink(missing_ok=True)
            self._fail(req, StatementError(f"Cannot write {target}: {exc.strerror or exc}", code="export_io"))
        except Exception as exc:
            part.unlink(missing_ok=True)
            logger.exception("Unexpected error exporting on {}", self.name)
            self._fail(req, StatementError(f"Internal error: {exc}"))
        finally:
            self._set_running(None)

    def _check_export(self, cmd: ExportCommand, target: Path, req: _Request) -> None:
        if cmd.format == FormatKind.IN_LIST:
            raise StatementError("An IN list cannot be exported; copy it from the grid", code="unsupported_format")
        if not target.is_absolute() or not target.parent.is_dir():
            raise StatementError(f"Choose an existing folder to export into: {cmd.path}", code="invalid_path")
        decision = self._hooks.check(cmd.sql, False)
        req.kind = decision.classification.kind
        if decision.action == GuardAction.BLOCK:
            raise StatementError("This connection is read-only", code="read_only")
        if req.kind != StatementKind.READ:
            raise StatementError(_EXPORT_SELECT_ONLY, code="export_requires_select")

    def _write_export(self, cmd: ExportCommand, part: Path, req: _Request) -> int:
        result = self.connection.run(cmd.sql)
        try:
            columns = result.columns()
            if columns is None:
                raise StatementError(_EXPORT_SELECT_ONLY, code="export_requires_select")
            written = 0
            with part.open("w", encoding="utf-8", newline="") as fp:
                writer = open_writer(cmd.format, fp, [FormatColumn(name=c.name, category=c.category) for c in columns],
                                     cmd.options, self._hooks.dialect)
                while rows := result.fetch(_EXPORT_CHUNK):
                    self._raise_if_cancelled()
                    writer.write(rows)
                    before, written = written, written + len(rows)
                    req.rows = written
                    if written // _PROGRESS_EVERY > before // _PROGRESS_EVERY:
                        self._send(ProgressEvent(request_id=cmd.request_id, rows=written // _PROGRESS_EVERY * _PROGRESS_EVERY))
                writer.close()
            return written
        finally:
            result.close()

    def _raise_if_cancelled(self) -> None:
        with self._state_lock:
            if self._cancel_requested:
                raise StatementCancelled()
```
with `_EXPORT_SELECT_ONLY = "Only a statement that returns rows (SELECT, WITH, VALUES…) can be exported"`. Route `ExportCommand` in `submit()` through the generalised `_submit_request(cmd, self._export)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_export.py tests/sql -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck`
```bash
git add backend src
git commit -m "feat(sessions): export a full result (raw values) to a file with progress and cancel

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 12: Settings, workspace and scripts stores

**Files:**
- Create: `backend/dbaguy_backend/storage/json_files.py`, `backend/dbaguy_backend/domain/settings.py`, `backend/dbaguy_backend/domain/workspace.py`, `backend/dbaguy_backend/domain/scripts.py`, `backend/dbaguy_backend/services/settings_service.py`, `backend/dbaguy_backend/services/workspace_service.py`, `backend/dbaguy_backend/services/scripts_service.py`, `backend/dbaguy_backend/api/settings.py`, `backend/dbaguy_backend/api/workspace.py`, `backend/dbaguy_backend/api/scripts.py`
- Modify: `backend/dbaguy_backend/storage/profile_store.py` (use `json_files`), `backend/dbaguy_backend/storage/paths.py`, `backend/dbaguy_backend/domain/errors.py` (`StorageError`, `AlreadyExists`), `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/app.py` (routers)
- Test: `backend/tests/test_local_stores.py`, `backend/tests/test_api.py`

**Interfaces:**
- Produces:
  - `storage/json_files.py`: `overlay(raw: dict, model: BaseModel) -> dict`, `move_aside(path, reason, error: type[DomainError]) -> None`, `write_text_atomic(path, text, error) -> None`, `write_json_atomic(path, payload, error) -> None` (moved out of `ProfileStore`, which now calls them — its tests pass unchanged); `JsonDocument[M](path, model)` with `load() -> M` and `save(value: M)` — unknown fields survive a round trip (overlay); a missing file gives the defaults; an unreadable or invalid file is moved aside (`<name>.corrupt-<stamp>-<n>`, logged at ERROR with field paths only, never values) and the defaults are used.
  - `StorageError(DomainError)` (`storage_error`, 500); `AlreadyExists(DomainError)` (`already_exists`, 409).
  - `paths.py`: `settings_file`, `workspace_file`, `history_file` (`history.db`, used by Task 13), `default_scripts_dir` (`<data_dir>/scripts`).
  - `AppSettings` (spec §4.9; all sections `extra="forbid"`, defaults as listed): `editor{font_size=13 (8–32), vim=False, word_wrap=False, tab_size=2 (1–8), keyword_case="upper"|"preserve"}`, `run{default="statement"|"all", continue_on_error=False, page_size=500 (1–10000), statement_timeout_s=0 (0–86400)}`, `safety{idle_transaction_warn_minutes=3 (0–1440), idle_transaction_rollback_minutes=0 (0–1440)}`, `notifications{long_query_seconds=15 (≥0)}`, `scripts_dir: str | None = None` (None = default folder).
  - `SettingsService(path, default_scripts_dir)`: `get() -> AppSettings` (cached), `update(value) -> AppSettings` (`scripts_dir` must be absolute after `~` expansion, else `ValidationFailed`), `scripts_root() -> Path` (created if missing).
  - `WorkspaceState{version=1, tabs: list[WorkspaceTab], active_tab_id: str | None, sidebar_tab: str | None}`; `WorkspaceTab{id (1–100 chars), kind: "query"|"table", title="", profile_id: UUID | None, database: str | None, script_path: str | None, object_path: list[PathSegment] | None, buffer: str | None, cursor: int | None (≥0, UTF-16), selection: TextSelection{anchor, head} | None, autocommit: bool | None}`. `WorkspaceService(path)`: `get()`, `put(state)`.
  - `ScriptNode{name, path ("/"-separated, relative), kind: "file"|"folder", children: list[ScriptNode] = []}`, `ScriptTree{root: str, items}`, `ScriptFile{path, content, modified_at}`, `ScriptWrite{path, content}`, `ScriptRename{from, to}` (aliases of `source`/`target`), `ScriptFolder{path}`.
  - `ScriptsService(root: Callable[[], Path])`: `tree()`, `read(path)`, `write(path, content) -> ScriptFile`, `rename(source, target)`, `delete(path)`, `create_folder(path)`.
  - HTTP: `GET/PUT /settings`; `GET /workspace`, `PUT /workspace` → 204; `GET /scripts` → `ScriptTree`; `GET /scripts/file?path=`; `PUT /scripts/file` → `ScriptFile`; `POST /scripts/rename` → 204; `DELETE /scripts/file?path=` → 204; `POST /scripts/folder` → 204. All behind the token.
- Script path rules: non-empty, relative, `/`-separated (no `\`, no NUL), no `..` component; the resolved path (symlinks followed) must be the root or inside it, else `ValidationFailed`. Writes and rename targets of files must end in `.sql` (case-insensitive) so a repository used as the scripts folder never gets other files overwritten; content ≤ 10 MB; parent folders are created inside the root. Rename never overwrites (`AlreadyExists`). Delete removes a file or an **empty** folder (a non-empty one → `ValidationFailed`). Missing → `NotFound`. The tree lists folders and `*.sql` files, folders first, then case-insensitive name order, skips names starting with `.`, never follows symlinks, and stops at depth 10 or 10,000 nodes (WARNING).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_local_stores.py`:
```python
import json

import pytest

from dbaguy_backend.domain.errors import AlreadyExists, NotFound, ValidationFailed
from dbaguy_backend.domain.settings import AppSettings
from dbaguy_backend.domain.workspace import WorkspaceState
from dbaguy_backend.services.scripts_service import ScriptsService
from dbaguy_backend.services.settings_service import SettingsService
from dbaguy_backend.services.workspace_service import WorkspaceService


def test_settings_defaults_and_unknown_fields_survive(tmp_path):
    path = tmp_path / "settings.json"
    assert SettingsService(path, tmp_path / "scripts").get() == AppSettings()
    path.write_text(json.dumps({"editor": {"font_size": 15, "future": 1}, "future_section": {"x": 2}}))
    service = SettingsService(path, tmp_path / "scripts")
    current = service.get()
    assert current.editor.font_size == 15 and current.run.page_size == 500
    service.update(current.model_copy(update={"run": current.run.model_copy(update={"page_size": 1000})}))
    saved = json.loads(path.read_text())
    assert saved["run"]["page_size"] == 1000 and saved["future_section"] == {"x": 2} and saved["editor"]["future"] == 1


@pytest.mark.parametrize("content", ["{not json", '{"editor": {"font_size": 999}}', "[]"])
def test_unreadable_settings_are_moved_aside(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    assert SettingsService(path, tmp_path / "scripts").get() == AppSettings()
    assert len(list(tmp_path.glob("settings.json.corrupt-*"))) == 1


def test_scripts_root(tmp_path):
    service = SettingsService(tmp_path / "settings.json", tmp_path / "scripts")
    assert service.scripts_root() == tmp_path / "scripts" and (tmp_path / "scripts").is_dir()
    with pytest.raises(ValidationFailed):
        service.update(AppSettings(scripts_dir="relative/dir"))
    service.update(AppSettings(scripts_dir=str(tmp_path / "repo")))
    assert service.scripts_root() == tmp_path / "repo"


def test_workspace_round_trip(tmp_path):
    service = WorkspaceService(tmp_path / "workspace.json")
    assert service.get() == WorkspaceState()
    state = WorkspaceState.model_validate({
        "tabs": [{"id": "t1", "kind": "query", "title": "Q", "buffer": "SELECT '😀'", "cursor": 11,
                  "selection": {"anchor": 0, "head": 6}}],
        "active_tab_id": "t1",
    })
    service.put(state)
    assert WorkspaceService(tmp_path / "workspace.json").get() == state


@pytest.fixture
def scripts(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    return ScriptsService(lambda: root), root


def test_scripts_crud_and_tree(scripts):
    service, root = scripts
    service.create_folder("reports")
    service.write("reports/daily.sql", "SELECT 1;")
    service.write("a.sql", "SELECT 2;")
    (root / "notes.txt").write_text("x")
    (root / ".hidden.sql").write_text("x")
    tree = service.tree()
    assert tree.root == str(root)
    assert [(n.name, n.kind) for n in tree.items] == [("reports", "folder"), ("a.sql", "file")]
    assert tree.items[0].children[0].path == "reports/daily.sql"
    assert service.read("reports/daily.sql").content == "SELECT 1;"
    service.rename("a.sql", "reports/b.sql")
    with pytest.raises(AlreadyExists):
        service.rename("reports/b.sql", "reports/daily.sql")
    with pytest.raises(ValidationFailed):
        service.delete("reports")  # not empty
    service.delete("reports/b.sql")
    service.delete("reports/daily.sql")
    service.delete("reports")
    assert service.tree().items == []
    with pytest.raises(NotFound):
        service.read("gone.sql")


@pytest.mark.parametrize("path", ["../x.sql", "/etc/passwd", "a/../../x.sql", "", "a\\b.sql", "nul\0.sql"])
def test_paths_outside_the_root_are_rejected(scripts, path):
    service, _ = scripts
    with pytest.raises(ValidationFailed):
        service.read(path)
    with pytest.raises(ValidationFailed):
        service.write(path, "x")


def test_symlink_escapes_are_rejected(scripts, tmp_path):
    service, root = scripts
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.sql").write_text("s")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationFailed):
        service.read("link/secret.sql")
    with pytest.raises(ValidationFailed):
        service.write("link/new.sql", "x")
    assert service.tree().items == []  # symlinks are never followed


def test_only_sql_files_are_written(scripts):
    service, _ = scripts
    with pytest.raises(ValidationFailed):
        service.write("x.txt", "x")
    service.write("x.sql", "x")
    with pytest.raises(ValidationFailed):
        service.rename("x.sql", "x.txt")
```

Add to `backend/tests/test_api.py`:
```python
def test_settings_and_workspace_api(client):
    settings = client.get("/settings", headers=AUTH).json()
    assert settings["run"]["page_size"] == 500
    settings["editor"]["vim"] = True
    assert client.put("/settings", json=settings, headers=AUTH).json()["editor"]["vim"] is True
    workspace = {"version": 1, "tabs": [{"id": "t1", "kind": "query", "buffer": "SELECT '😀'"}], "active_tab_id": "t1"}
    assert client.put("/workspace", json=workspace, headers=AUTH).status_code == 204
    assert client.get("/workspace", headers=AUTH).json()["tabs"][0]["buffer"] == "SELECT '😀'"


def test_scripts_api(client):
    assert client.put("/scripts/file", json={"path": "a.sql", "content": "SELECT 1"}, headers=AUTH).status_code == 200
    assert client.get("/scripts", headers=AUTH).json()["items"][0]["path"] == "a.sql"
    assert client.get("/scripts/file", params={"path": "a.sql"}, headers=AUTH).json()["content"] == "SELECT 1"
    assert client.post("/scripts/rename", json={"from": "a.sql", "to": "b.sql"}, headers=AUTH).status_code == 204
    assert client.get("/scripts/file", params={"path": "../x.sql"}, headers=AUTH).status_code == 422
    assert client.delete("/scripts/file", params={"path": "b.sql"}, headers=AUTH).status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_local_stores.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.domain.settings'`).

- [ ] **Step 3: Extract the JSON file helpers**

Move `_overlay` (→ `overlay`), `_move_aside` (→ `move_aside(path, reason, error)`) and the temp-file + `os.replace` write (→ `write_text_atomic` / `write_json_atomic`, `ensure_ascii=False`, `indent=2`) from `storage/profile_store.py` into `storage/json_files.py`; `ProfileStore` passes `ProfileStoreError`. Then:
```python
class JsonDocument(Generic[M]):
    """One pydantic model stored as a JSON file. Fields this build does not know survive a round trip; an unreadable
    or invalid file is moved aside and the defaults are used."""

    def __init__(self, path: Path, model: type[M]) -> None:
        self._path = path
        self._model = model
        self._raw: dict[str, Any] = {}
        self._lock = threading.Lock()

    def load(self) -> M:
        with self._lock:
            self._raw = {}
            if not self._path.exists():
                return self._model()
            try:
                raw = json.loads(self._path.read_text("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not a JSON object")
                value = self._model.model_validate(raw, extra="ignore")
            except ValidationError as exc:
                move_aside(self._path, f"invalid {error_fields(exc)}", StorageError)
                return self._model()
            except (ValueError, UnicodeDecodeError) as exc:
                move_aside(self._path, str(exc), StorageError)
                return self._model()
            except OSError as exc:
                raise StorageError(f"Cannot read {self._path.name}: {exc.strerror or exc}") from exc
            self._raw = raw
            return value

    def save(self, value: M) -> None:
        with self._lock:
            payload = overlay(self._raw, value)
            write_json_atomic(self._path, payload, StorageError)
            self._raw = payload
```
(`error_fields` is `ProfileStore`'s `_error_fields`, moved too. Order the `except` clauses as written: pydantic's `ValidationError` is a `ValueError`.)

- [ ] **Step 4: Implement the models, services and routers**

`SettingsService`:
```python
class SettingsService:
    def __init__(self, path: Path, default_scripts_dir: Path) -> None:
        self._doc = JsonDocument(path, AppSettings)
        self._default_scripts_dir = default_scripts_dir
        self._current: AppSettings | None = None
        self._lock = threading.Lock()

    def get(self) -> AppSettings:
        with self._lock:
            if self._current is None:
                self._current = self._doc.load()
            return self._current

    def update(self, value: AppSettings) -> AppSettings:
        if value.scripts_dir is not None and not Path(value.scripts_dir).expanduser().is_absolute():
            raise ValidationFailed("The scripts folder must be an absolute path")
        with self._lock:
            self._doc.save(value)
            self._current = value
        logger.info("Settings saved")
        return value

    def scripts_root(self) -> Path:
        configured = self.get().scripts_dir
        root = Path(configured).expanduser() if configured else self._default_scripts_dir
        root.mkdir(parents=True, exist_ok=True)
        return root
```
`WorkspaceService` is the same shape without `scripts_root` (`get`/`put`; log at DEBUG — it is written every few seconds).

`ScriptsService` — the path guard every method goes through:
```python
_MAX_SCRIPT_BYTES = 10 * 1024 * 1024
_MAX_DEPTH = 10
_MAX_NODES = 10_000


class ScriptsService:
    """The scripts folder: `.sql` files and folders below one root, never anything outside it."""

    def __init__(self, root: Callable[[], Path]) -> None:
        self._root = root

    def _resolve(self, relative: str) -> tuple[Path, Path]:
        """(root, target) — both resolved; rejects anything that is not strictly inside the root."""
        if not relative or "\\" in relative or "\0" in relative or relative.startswith("/"):
            raise ValidationFailed("Script paths are relative to the scripts folder, separated by '/'")
        parts = PurePosixPath(relative).parts
        if ".." in parts:
            raise ValidationFailed("Script paths cannot contain '..'")
        root = self._root().resolve()
        target = root.joinpath(*parts).resolve()  # follows symlinks: a link pointing outside is caught below
        if target == root or not target.is_relative_to(root):
            raise ValidationFailed(f"{relative} is outside the scripts folder")
        return root, target

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()
```
Implement `tree/read/write/rename/delete/create_folder` to the rules above with `os.scandir` (`is_dir(follow_symlinks=False)` / `is_file(follow_symlinks=False)`), `write_text_atomic` for writes, `modified_at` from `stat().st_mtime` (UTC). Log writes, renames and deletes at INFO with the relative path.

Routers follow the existing style (`APIRouter(prefix=…, tags=[…], dependencies=[Depends(require_token)])`, `get_container`); `DELETE /scripts/file` and `GET /scripts/file` take `path` as a query parameter. Container: `self.app_settings = SettingsService(settings_file(d), default_scripts_dir(d))` (`self.settings` is already the process `Settings`), `self.workspace = WorkspaceService(workspace_file(d))`, `self.scripts = ScriptsService(self.app_settings.scripts_root)`. Register the routers in `app.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_local_stores.py tests/test_profiles.py tests/test_api.py -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 6: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck`
```bash
git add backend src/lib/api
git commit -m "feat(backend): settings, workspace and scripts stores with atomic writes and path guards

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 13: Query history and session wiring (policy, history, schema invalidation, idle transactions)

**Files:**
- Create: `backend/dbaguy_backend/domain/history.py`, `backend/dbaguy_backend/services/history_service.py`, `backend/dbaguy_backend/sessions/hooks.py`, `backend/dbaguy_backend/api/history.py`
- Modify: `backend/dbaguy_backend/sessions/manager.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/app.py`
- Test: `backend/tests/test_history.py`, `backend/tests/test_session_wiring.py`, `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `WorkerHooks`, `ExecutionRecord`, `SessionWorker.configure/check_idle_transaction` (Task 10); `SafetyPolicy` (Task 8); `SchemaCache.invalidate` (Task 5); `SettingsService` (Task 12); `history_file` (Task 12).
- Produces:
  - `HistoryStatus = Literal["ok", "error", "cancelled", "timeout"]`; `HistoryRecord{profile_id: UUID, profile_name, database: str | None, sql, started_at: datetime, elapsed_ms: int, rows: int, status, error: str | None = None}`; `HistoryEntry(HistoryRecord){id: int}`; `HistoryPage{items: list[HistoryEntry], next_before: int | None}`.
  - `HistoryService(path, retention=50_000, prune_every=500)`: `record(rec)` (error truncated to 500 characters), `search(q="", profile_id=None, limit=100, before=None) -> HistoryPage` (newest first; `next_before` = id to pass as `before` for the next page, None on the last page), `clear()`, `close()`. Thread-safe (one connection, `check_same_thread=False`, a lock), WAL.
  - Search: `q` of 3+ characters → FTS5 trigram phrase match (case-insensitive substring, `"` doubled); 1–2 characters → `LIKE '%q%'` with `%`/`_`/`\` escaped; empty → everything.
  - A history file that is not a database is moved aside (`move_aside`, Task 12) and recreated.
  - `SessionHooksFactory(registry, profiles, policy, history, schema_cache).build(profile_id, database) -> WorkerHooks` — the policy check reads the profile **each time** (safety level / read-only can change while a session is open); history records carry the profile name at session start; schema changes invalidate every database of the profile.
  - `SessionManager(connections, profiles, hooks: SessionHooksFactory | None = None, idle_timeout=600.0)`; `create()` calls `worker.configure(hooks.build(profile.id, server.current_database))` before registering; `check_idle_transactions(warn_after_s, rollback_after_s)` calls every worker's `check_idle_transaction` (a failing one is logged, the rest still run).
  - Container: `self.history`, `self.policy = SafetyPolicy()`, `self.schema_cache` built before `self.sessions` (reorder), sessions get the factory; housekeeping runs every **15 s** and adds the idle-transaction check with the current `app_settings.safety` minutes × 60; `close()` closes the history service last.
  - HTTP: `GET /history?q=&profile_id=&limit=100&before=` → `HistoryPage` (`limit` 1–1000); `DELETE /history` → 204.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_history.py`:
```python
from datetime import UTC, datetime
from uuid import uuid4

from dbaguy_backend.domain.history import HistoryRecord
from dbaguy_backend.services.history_service import HistoryService

PID = uuid4()


def rec(sql, status="ok", profile=PID, error=None):
    return HistoryRecord(profile_id=profile, profile_name="p", database=None, sql=sql, started_at=datetime.now(UTC),
                         elapsed_ms=5, rows=1, status=status, error=error)


def sqls(page):
    return [e.sql for e in page.items]


def test_search(tmp_path):
    history = HistoryService(tmp_path / "h.db")
    for sql in ["SELECT * FROM customers", "select email from users", "UPDATE orders SET x = 1", "SELECT 1"]:
        history.record(rec(sql))
    assert sqls(history.search("CUSTOM")) == ["SELECT * FROM customers"]  # trigram, case-insensitive
    assert sqls(history.search("1")) == ["SELECT 1", "UPDATE orders SET x = 1"]  # short: LIKE, newest first
    assert sqls(history.search("%")) == []  # LIKE wildcards are literal
    assert sqls(history.search('a"b"c')) == []  # quotes cannot break the FTS query
    assert sqls(history.search())[0] == "SELECT 1"
    history.close()


def test_paging_profile_filter_and_truncation(tmp_path):
    history = HistoryService(tmp_path / "h.db")
    for i in range(5):
        history.record(rec(f"SELECT {i}"))
    first = history.search(limit=2)
    assert sqls(first) == ["SELECT 4", "SELECT 3"] and first.next_before is not None
    second = history.search(limit=2, before=first.next_before)
    assert sqls(second) == ["SELECT 2", "SELECT 1"]
    assert history.search(limit=10).next_before is None
    other = uuid4()
    history.record(rec("SELECT other", profile=other, status="error", error="x" * 2000))
    [entry] = history.search(profile_id=other).items
    assert entry.status == "error" and len(entry.error) == 500
    history.close()


def test_retention_and_clear(tmp_path):
    history = HistoryService(tmp_path / "h.db", retention=3, prune_every=1)
    for i in range(5):
        history.record(rec(f"SELECT {i}"))
    assert sqls(history.search()) == ["SELECT 4", "SELECT 3", "SELECT 2"]
    assert sqls(history.search("SELECT 0")) == []  # the full-text index follows deletions
    history.clear()
    assert sqls(history.search()) == [] and sqls(history.search("SELECT")) == []
    history.close()


def test_a_corrupt_file_is_moved_aside(tmp_path):
    (tmp_path / "h.db").write_bytes(b"not a database at all" * 100)
    history = HistoryService(tmp_path / "h.db")
    history.record(rec("SELECT 1"))
    assert sqls(history.search()) == ["SELECT 1"]
    assert list(tmp_path.glob("h.db.corrupt-*"))
    history.close()
```

`backend/tests/test_session_wiring.py`:
```python
import sqlite3
import time

import pytest

from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.history_service import HistoryService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.safety import SafetyPolicy
from dbaguy_backend.sessions.hooks import SessionHooksFactory
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.test_sessions import Recorder
from tests.test_worker_protocol import run


class FakeCache:
    def __init__(self):
        self.invalidated = []

    def invalidate(self, profile_id, database=None):
        self.invalidated.append((profile_id, database))


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "w.db"
    sqlite3.connect(db).executescript("CREATE TABLE t (id INTEGER);")
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    body = {"profile": {"engine": "sqlite", "name": "w", "path": str(db)}}
    pid = profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id
    registry = AdapterRegistry([SqliteAdapter()])
    connections = ConnectionService(registry, profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    history = HistoryService(tmp_path / "history.db")
    cache = FakeCache()
    manager = SessionManager(connections, profiles, SessionHooksFactory(registry, profiles, SafetyPolicy(), history, cache))

    def open_session():
        info = manager.create(SessionCreateRequest(profile_id=pid))
        recorder = Recorder()
        manager.attach(info.session_id, recorder)
        return manager.worker(info.session_id), recorder

    def set_profile(**fields):
        profiles.update(pid, ProfileUpsertRequest.model_validate({"profile": {**body["profile"], **fields}}))

    yield open_session, set_profile, pid, history, cache, manager
    manager.close_all()
    history.close()


def test_attach_replays_state_and_ddl_is_recorded_and_invalidates(env):
    open_session, _, pid, history, cache, _ = env
    worker, rec = open_session()
    assert rec.wait_for(lambda e: e.type == "session_state").autocommit
    run(worker, rec, "a", "CREATE TABLE z (id INTEGER)")
    assert cache.invalidated == [(pid, None)]
    [entry] = history.search().items
    assert (entry.sql, entry.profile_name, entry.status) == ("CREATE TABLE z (id INTEGER)", "w", "ok")


def test_safety_settings_apply_to_open_sessions(env):
    open_session, set_profile, *_ = env
    worker, rec = open_session()
    assert not run(worker, rec, "a", "INSERT INTO t VALUES (1)").skipped
    set_profile(safety="strict")
    assert run(worker, rec, "b", "INSERT INTO t VALUES (2)").skipped
    set_profile(read_only=True)
    run(worker, rec, "c", "INSERT INTO t VALUES (3)", confirmed=True)
    assert rec.of("c", "error")[0].code == "read_only"


def test_idle_transactions_are_rolled_back(env):
    open_session, _, _, _, _, manager = env
    worker, rec = open_session()
    run(worker, rec, "a", "BEGIN")
    run(worker, rec, "b", "INSERT INTO t VALUES (9)")
    time.sleep(0.2)
    manager.check_idle_transactions(warn_after_s=0.1, rollback_after_s=0.1)
    rec.wait_for(lambda e: e.type == "message" and e.text.startswith("Rolled back"))
    run(worker, rec, "c", "SELECT count(*) FROM t")
    assert rec.of("c", "rows")[0].rows == [[0]]
```

Add to `backend/tests/test_api.py`:
```python
def test_history_api(client):
    container = client.app.state.container
    from datetime import UTC, datetime
    from uuid import uuid4
    from dbaguy_backend.domain.history import HistoryRecord
    container.history.record(HistoryRecord(profile_id=uuid4(), profile_name="p", database=None, sql="SELECT 42",
                                           started_at=datetime.now(UTC), elapsed_ms=1, rows=1, status="ok"))
    page = client.get("/history", params={"q": "42"}, headers=AUTH).json()
    assert [e["sql"] for e in page["items"]] == ["SELECT 42"]
    assert client.delete("/history", headers=AUTH).status_code == 204
    assert client.get("/history", headers=AUTH).json()["items"] == []
```
(Move those imports to the top of the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_history.py tests/test_session_wiring.py -q`
Expected: FAIL (`No module named 'dbaguy_backend.domain.history'`).

- [ ] **Step 3: Implement the history service**

`backend/dbaguy_backend/services/history_service.py`:
```python
import sqlite3
import threading
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..domain.errors import StorageError
from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord
from ..storage.json_files import move_aside

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    database_name TEXT,
    sql TEXT NOT NULL,
    started_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS history_by_profile ON history (profile_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(sql, content='history', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
    INSERT INTO history_fts (rowid, sql) VALUES (new.id, new.sql);
END;
CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
    INSERT INTO history_fts (history_fts, rowid, sql) VALUES ('delete', old.id, old.sql);
END;
"""
_COLUMNS = "id, profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status, error"
_MAX_ERROR = 500
_TRIGRAM = 3


def _like_pattern(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class HistoryService:
    """Executed statements in a local SQLite database, searchable by substring."""

    def __init__(self, path: Path, retention: int = 50_000, prune_every: int = 500) -> None:
        self._path = path
        self._retention = retention
        self._prune_every = prune_every
        self._inserts = 0
        self._lock = threading.Lock()
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:
            move_aside(self._path, str(exc), StorageError)
            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def record(self, rec: HistoryRecord) -> None:
        error = rec.error[:_MAX_ERROR] if rec.error else None
        with self._lock:
            self._conn.execute(
                f"INSERT INTO history ({_COLUMNS.removeprefix('id, ')}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
                 rec.elapsed_ms, rec.rows, rec.status, error),
            )
            self._inserts += 1
            if self._inserts % self._prune_every == 0:
                self._conn.execute(
                    "DELETE FROM history WHERE id <= (SELECT max(id) FROM history) - ?", (self._retention,)
                )

    def search(
        self, q: str = "", profile_id: UUID | None = None, limit: int = 100, before: int | None = None
    ) -> HistoryPage:
        where: list[str] = []
        params: list[object] = []
        if profile_id is not None:
            where.append("profile_id = ?")
            params.append(str(profile_id))
        if before is not None:
            where.append("id < ?")
            params.append(before)
        q = q.strip()
        if len(q) >= _TRIGRAM:
            where.append("id IN (SELECT rowid FROM history_fts WHERE history_fts MATCH ?)")
            params.append('"' + q.replace('"', '""') + '"')
        elif q:
            where.append("sql LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(q))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM history {clause} ORDER BY id DESC LIMIT ?", (*params, limit + 1)
            ).fetchall()
        items = [
            HistoryEntry(id=r[0], profile_id=r[1], profile_name=r[2], database=r[3], sql=r[4], started_at=r[5],
                         elapsed_ms=r[6], rows=r[7], status=r[8], error=r[9])
            for r in rows[:limit]
        ]
        return HistoryPage(items=items, next_before=items[-1].id if len(rows) > limit else None)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history")
        logger.info("History cleared")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
```
(If `sqlite3.connect` itself succeeds on a garbage file and the error only appears on the first statement, the `_connect` structure above already covers it: the `PRAGMA`/schema statements raise `DatabaseError`.)

- [ ] **Step 4: Implement the wiring**

`backend/dbaguy_backend/sessions/hooks.py`:
```python
from uuid import UUID

from ..adapters.registry import AdapterRegistry
from ..domain.history import HistoryRecord
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .worker import ExecutionRecord, WorkerHooks


class SessionHooksFactory:
    """Connects a session's worker to the safety policy, the history and the schema cache."""

    def __init__(
        self, registry: AdapterRegistry, profiles: ProfileService, policy: SafetyPolicy, history: HistoryService,
        schema_cache: SchemaCache,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._policy = policy
        self._history = history
        self._schema_cache = schema_cache

    def build(self, profile_id: UUID, database: str | None) -> WorkerHooks:
        profile = self._profiles.get(profile_id)
        dialect = self._registry.get(profile.engine).dialect

        def check(sql: str, confirmed: bool) -> GuardDecision:
            current = self._profiles.get(profile_id)  # safety level and read-only may change while it is open
            return self._policy.check(sql, dialect, current.safety, current.read_only, confirmed)

        def on_record(record: ExecutionRecord) -> None:
            self._history.record(HistoryRecord(
                profile_id=profile_id, profile_name=profile.name, database=database, sql=record.sql,
                started_at=record.started_at, elapsed_ms=record.elapsed_ms, rows=record.rows,
                status=record.status, error=record.error,
            ))

        return WorkerHooks(
            dialect=dialect, check=check, on_record=on_record,
            on_schema_change=lambda: self._schema_cache.invalidate(profile_id),
        )
```
In Task 10 the worker passes the full error message in `ExecutionRecord.error`; the 500-character limit is applied by `HistoryService.record` only.

`SessionManager`: new optional `hooks` parameter (keep `idle_timeout` keyword-compatible), `worker.configure(...)` inside the existing `try` of `create()` right after `server_info`, and
```python
    def check_idle_transactions(self, warn_after_s: float, rollback_after_s: float) -> None:
        with self._lock:
            entries = list(self._sessions.items())
        for session_id, entry in entries:
            try:
                entry.worker.check_idle_transaction(warn_after_s, rollback_after_s)
            except Exception:
                logger.exception("Idle-transaction check failed for session {}", session_id)
```
Container: reorder construction (`metadata` → `schema_cache` → `history`/`policy` → `sessions` with the factory), `_HOUSEKEEPING_INTERVAL_S = 15`, add
```python
    def _check_idle_transactions(self) -> None:
        safety = self.app_settings.get().safety
        self.sessions.check_idle_transactions(
            safety.idle_transaction_warn_minutes * 60, safety.idle_transaction_rollback_minutes * 60
        )
```
to `_housekeep` (guarded like the reapers), and `self._guarded("Closing history", self.history.close)` at the end of `close()`. `api/history.py` with the two routes; register it in `app.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_history.py tests/test_session_wiring.py tests/test_api.py tests/test_container.py -q && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 6: Regenerate API types and commit**

Run: `npm run gen:api && npm run typecheck`
```bash
git add backend src/lib/api
git commit -m "feat(backend): searchable query history and session wiring for policy, history and idle transactions

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 14: The `/sql` API (UTF-16 offsets) and editor-ready error positions

**Files:**
- Create: `backend/dbaguy_backend/api/sql.py`
- Modify: `backend/dbaguy_backend/app.py` (router), `backend/dbaguy_backend/adapters/positions.py` (UTF-16 column)
- Test: `backend/tests/test_sql_api.py`, `backend/tests/test_core_utils.py` (positions)

**Interfaces:**
- Consumes: `split` (Task 2), `Utf16Index` (Task 1), `complete`, `describe_at`, `CompletionItem`, `DescribeResult` (Task 6), `format_rows`, `FormatResult`, `FormatColumn`, `FormatKind`, `RowFormatOptions` (Tasks 7/11), `build_browse_select`, `SortKey` (Task 7), `SchemaCache.index` (Tasks 5–6), `container.policy`, `GuardDecision` (Tasks 8/13).
- Produces (request models `extra="forbid"`; every offset in and out is UTF-16):
  - `POST /sql/split {text, dialect}` → `{statements: [{start, end, terminator_end}]}`.
  - `POST /sql/analyze {profile_id, statements: list[str] (≤10,000)}` → `list[GuardDecision]` (as if unconfirmed; dialect, level and read-only from the profile).
  - `POST /sql/complete {profile_id?, dialect?, database?, text, cursor}` → `{start, end, items}`; the dialect comes from the profile when given, else `dialect` is required (422). Without a profile, or when the snapshot cannot be built (WARNING logged, spec §5), only keywords are returned.
  - `POST /sql/describe {profile_id, database?, text, cursor}` → `DescribeResult | null`.
  - `POST /sql/format-rows {dialect, kind, columns, rows (≤100,000), options}` → `FormatResult` (rows are the grid's encoded cells: `source="encoded"`).
  - `POST /sql/browse-select {profile_id, path, where?, order_by: list[SortKey] = [], count: bool = false}` → `{sql}`.
  - `ErrorPosition.column` counts UTF-16 code units (the editor's unit); `line` is unchanged.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_sql_api.py`:
```python
from tests.test_api import AUTH, client, sqlite_profile_id  # noqa: F401  (fixtures)

EMOJI_SQL = "SELECT '😀'; SELECT * FROM t WHERE t.i"


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def test_split_uses_utf16_offsets(client):
    statements = client.post("/sql/split", json={"text": EMOJI_SQL, "dialect": "sqlite"}, headers=AUTH).json()["statements"]
    assert statements[0] == {"start": 0, "end": 11, "terminator_end": 12}  # '😀' is two UTF-16 units
    assert statements[1]["start"] == 13 and statements[1]["end"] == utf16_len(EMOJI_SQL)


def test_complete_after_an_astral_character(client, sqlite_profile_id):
    cursor = utf16_len(EMOJI_SQL)
    body = {"profile_id": sqlite_profile_id, "text": EMOJI_SQL, "cursor": cursor}
    result = client.post("/sql/complete", json=body, headers=AUTH).json()
    assert [i["label"] for i in result["items"]] == ["id"]
    assert (result["start"], result["end"]) == (cursor - 1, cursor)


def test_complete_without_a_profile_gives_keywords(client):
    result = client.post("/sql/complete", json={"dialect": "postgres", "text": "SEL", "cursor": 3}, headers=AUTH).json()
    assert "SELECT" in [i["label"] for i in result["items"]]
    assert client.post("/sql/complete", json={"text": "SEL", "cursor": 3}, headers=AUTH).status_code == 422


def test_describe(client, sqlite_profile_id):
    body = {"profile_id": sqlite_profile_id, "text": "SELECT * FROM t", "cursor": 15}
    found = client.post("/sql/describe", json=body, headers=AUTH).json()
    assert found["name"] == "t" and [c["name"] for c in found["columns"]] == ["id", "v"]
    body["cursor"] = 2
    assert client.post("/sql/describe", json=body, headers=AUTH).json() is None


def test_analyze(client, sqlite_profile_id):
    body = {"profile_id": sqlite_profile_id, "statements": ["SELECT 1", "DELETE FROM t"]}
    decisions = client.post("/sql/analyze", json=body, headers=AUTH).json()
    assert [d["action"] for d in decisions] == ["run", "confirm"]
    assert decisions[1]["reasons"] == ["delete_without_where"]


def test_format_rows_and_browse_select(client, sqlite_profile_id):
    body = {"dialect": "sqlite", "kind": "insert", "columns": [{"name": "id", "category": "number"}],
            "rows": [[1], [2]], "options": {"table_name": "t"}}
    assert client.post("/sql/format-rows", json=body, headers=AUTH).json()["text"].splitlines() == [
        "INSERT INTO t (id) VALUES (1);", "INSERT INTO t (id) VALUES (2);",
    ]
    path = [{"kind": "schema", "name": "main"}, {"kind": "folder", "name": "tables"}, {"kind": "table", "name": "t"}]
    body = {"profile_id": sqlite_profile_id, "path": path, "where": "id > 1", "order_by": [{"column": "v", "descending": True}]}
    assert client.post("/sql/browse-select", json=body, headers=AUTH).json()["sql"] == (
        "SELECT * FROM main.t\nWHERE id > 1\nORDER BY v DESC"
    )
    body["where"] = "1=1; DROP TABLE t"
    assert client.post("/sql/browse-select", json=body, headers=AUTH).status_code == 422
```

Add to `backend/tests/test_core_utils.py`:
```python
def test_error_columns_count_utf16_units():
    from dbaguy_backend.adapters.positions import offset_to_line_col
    assert offset_to_line_col("SELECT '😀', nope", 13) == (1, 14)  # 'n' is the 13th character, 14th UTF-16 unit
    assert offset_to_line_col("a\n😀b", 4) == (2, 3)
    assert offset_to_line_col("SELECT x", 8) == (1, 8)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_sql_api.py tests/test_core_utils.py -q`
Expected: FAIL (404 for `/sql/split`; the emoji column assertion fails).

- [ ] **Step 3: Implement**

`adapters/positions.py`:
```python
from ..sql.offsets import Utf16Index


def offset_to_line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based character offset → 1-based (line, column), clamped to the text. The column counts UTF-16 code units,
    the editor's unit (JavaScript strings), so characters outside the BMP earlier on the line do not shift it."""
    index = max(0, min(offset - 1, len(text)))
    line = text.count("\n", 0, index) + 1
    before = text[text.rfind("\n", 0, index) + 1:index]
    return line, Utf16Index(before).to_utf16(len(before)) + 1
```

`backend/dbaguy_backend/api/sql.py`:
```python
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.errors import ValidationFailed
from ..domain.formats import FormatColumn, FormatKind, RowFormatOptions
from ..domain.metadata import PathSegment
from ..services.safety import GuardDecision
from ..sql.completion import CompletionItem, DescribeResult, SnapshotIndex, complete, describe_at
from ..sql.dialects import Dialect
from ..sql.formatting import FormatResult, format_rows
from ..sql.offsets import Utf16Index
from ..sql.select_builder import SortKey, build_browse_select
from ..sql.splitter import split
from .deps import get_container, require_token

router = APIRouter(prefix="/sql", tags=["sql"], dependencies=[Depends(require_token)])


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SplitRequest(_Input):
    text: str
    dialect: Dialect


class StatementRange(BaseModel):
    start: int
    end: int
    terminator_end: int


class SplitResponse(BaseModel):
    statements: list[StatementRange]


class AnalyzeRequest(_Input):
    profile_id: UUID
    statements: list[str] = Field(max_length=10_000)


class TextAtCursor(_Input):
    profile_id: UUID | None = None
    dialect: Dialect | None = None
    database: str | None = None
    text: str
    cursor: int = Field(ge=0)  # UTF-16


class CompletionResponse(BaseModel):
    start: int
    end: int
    items: list[CompletionItem]


class FormatRowsRequest(_Input):
    dialect: Dialect
    kind: FormatKind
    columns: list[FormatColumn]
    rows: list[list[Any]] = Field(max_length=100_000)
    options: RowFormatOptions = Field(default_factory=RowFormatOptions)


class BrowseSelectRequest(_Input):
    profile_id: UUID
    path: list[PathSegment]
    where: str | None = None
    order_by: list[SortKey] = []
    count: bool = False


class BrowseSelectResponse(BaseModel):
    sql: str


def _profile_dialect(container: AppContainer, profile_id: UUID) -> Dialect:
    return container.registry.get(container.profiles.get(profile_id).engine).dialect


def _dialect(container: AppContainer, body: TextAtCursor) -> Dialect:
    if body.profile_id is not None:
        return _profile_dialect(container, body.profile_id)
    if body.dialect is None:
        raise ValidationFailed("Give a profile_id or a dialect")
    return body.dialect


def _snapshot_index(container: AppContainer, body: TextAtCursor) -> SnapshotIndex | None:
    if body.profile_id is None:
        return None
    try:
        return container.schema_cache.index(body.profile_id, body.database)
    except Exception as exc:  # unreachable server, locked profile…: keywords only (spec §5)
        logger.warning("No schema snapshot for {}: {}", body.profile_id, exc)
        return None


@router.post("/split", response_model=SplitResponse)
def split_statements(body: SplitRequest) -> SplitResponse:
    index = Utf16Index(body.text)
    return SplitResponse(statements=[
        StatementRange(start=index.to_utf16(s.start), end=index.to_utf16(s.end),
                       terminator_end=index.to_utf16(s.terminator_end))
        for s in split(body.text, body.dialect)
    ])


@router.post("/analyze", response_model=list[GuardDecision])
def analyze(body: AnalyzeRequest, container: AppContainer = Depends(get_container)) -> list[GuardDecision]:
    profile = container.profiles.get(body.profile_id)
    dialect = _profile_dialect(container, body.profile_id)
    return [container.policy.check(sql, dialect, profile.safety, profile.read_only) for sql in body.statements]


@router.post("/complete", response_model=CompletionResponse)
def complete_at(body: TextAtCursor, container: AppContainer = Depends(get_container)) -> CompletionResponse:
    dialect = _dialect(container, body)
    index = Utf16Index(body.text)
    result = complete(body.text, index.from_utf16(body.cursor), dialect, _snapshot_index(container, body))
    return CompletionResponse(start=index.to_utf16(result.start), end=index.to_utf16(result.end), items=result.items)


@router.post("/describe", response_model=DescribeResult | None)
def describe(body: TextAtCursor, container: AppContainer = Depends(get_container)) -> DescribeResult | None:
    if body.profile_id is None:
        raise ValidationFailed("describe needs a profile_id")
    snapshot_index = _snapshot_index(container, body)
    if snapshot_index is None:
        return None
    return describe_at(body.text, Utf16Index(body.text).from_utf16(body.cursor), _dialect(container, body), snapshot_index)


@router.post("/format-rows", response_model=FormatResult)
def format_rows_for_copy(body: FormatRowsRequest) -> FormatResult:
    return format_rows(body.kind, body.columns, body.rows, body.options, body.dialect, "encoded")


@router.post("/browse-select", response_model=BrowseSelectResponse)
def browse_select(body: BrowseSelectRequest, container: AppContainer = Depends(get_container)) -> BrowseSelectResponse:
    dialect = _profile_dialect(container, body.profile_id)
    return BrowseSelectResponse(sql=build_browse_select(dialect, body.path, body.where, body.order_by, body.count))
```
Register the router in `app.py`. Log each `/sql/*` call only at DEBUG (they run on every keystroke).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_sql_api.py tests/test_core_utils.py tests/test_postgres_adapter.py -q && .venv/bin/pytest -q`
Expected: all pass. A PG error-position test from ① that assumes code-point columns on ASCII text still passes (ASCII columns are unchanged).

- [ ] **Step 5: Regenerate API types, run the full checks and commit**

Run: `npm run gen:api && npm run typecheck && cd backend && .venv/bin/ruff check . 2>/dev/null; .venv/bin/pytest -q`
```bash
git add backend src/lib/api
git commit -m "feat(api): /sql endpoints (split, analyze, complete, describe, format-rows, browse-select) with UTF-16 offsets

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

## Rulings recorded while planning (deviations from the spec)

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| P1 | `Classification` has no `tables[]` (spec §3.4). | No consumer in ②; ⑤ gets its table from the browse path. | Add a field and a sqlglot walk later. |
| P2 | `session_state` is sent just **before** each `finished`, not after (spec §3.3). | A client that has seen `finished` then always has the current state; tests are deterministic. | Frontend reorders two handlers. |
| P3 | Read-only profiles also block `session` statements (`SET`, `RESET`, `DISCARD`, `PRAGMA x = y`). | `SET default_transaction_read_only = off` / `RESET ALL` would undo the DB-level guard. | `SET search_path` needs the profile's `options` instead on read-only connections. |
| P4 | Statement timeout covers execution until the first page; reading later pages is not timed. | A user reading a paused result must not be cancelled. | A slow `fetch_more` is not bounded. |
| P5 | DDL invalidates the schema cache when it becomes visible (immediately, or when the transaction ends), for every database of the profile. | Other connections cannot see uncommitted DDL; invalidating all databases avoids key mismatches (`None` vs. the default database name). | One extra snapshot rebuild per DDL. |
| P6 | Scripts: writes and rename targets must be `.sql`; delete removes files and empty folders permanently (the UI confirms). | A user's git repo as the scripts folder must never get other files overwritten; moving to Trash needs a new dependency. | Add Trash support later. |
| P7 | An invalid `settings.json` / `workspace.json` is moved aside as a whole and defaults are used. | Same recovery rule as `profiles.json`, simple and lossless (the file is kept). | One bad hand-edited value resets all settings (the backup has them). |
| P8 | Export discards a paused result on the same session. | One connection cannot serve two open cursors (PG streaming). | The grid's "load more" ends when exporting. |
| P9 | Error columns are UTF-16 code units. | The editor (JavaScript) counts UTF-16; spec requires UTF-16 offsets in APIs. | None for ASCII. |
| P10 | System schemas carry only function names in the snapshot (spec §3.2): `pg_catalog`/`information_schema` relations are not listed. | Keeps snapshots small; system tables are rarely typed. | `pg_catalog.pg_class` is not completed. |
