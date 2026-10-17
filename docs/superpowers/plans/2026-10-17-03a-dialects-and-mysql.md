# 03a: Dialect groundwork + MySQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MySQL 8.x a first-class DbaGuy engine (connect incl. SSH and TLS, object tree, DDL, table data, streaming, cancel, timeout, manual commit, read-only) and lay the dialect groundwork that 03b's T-SQL builds on: named lexer rules, MySQL `DELIMITER` and compound-body splitting, one source for literals and identifier quoting, and capability-driven UI.

**Architecture:** `sql/` gains a third dialect only through its rule tables (`LexRules` with named fields, `BodyRule`/`ClientCommand` enums, literal and quoting functions in `sql/dialects.py`); the splitter learns `DELIMITER` lines and nested `BEGIN … END` bodies. A new `adapters/mysql/` package (settings, catalog/introspection/snapshot, types, adapter) implements the unchanged adapter contract with mysql-connector-python in pure mode: verbatim unbuffered raw cursors, `KILL QUERY` from a short side connection for cancel and early close, the protocol's transaction flag plus a cached autocommit flag. Capabilities reach the frontend through `EngineInfo`, replacing `dialect === "postgres"` checks; "Filter by this value" moves to `POST /sql/value-condition`.

**Tech Stack:** Python 3.13, FastAPI, pydantic v2, loguru, sqlglot 30.23 (`MySQL` dialect imported by name), mysql-connector-python 26.7.0 (`use_pure=True`), MySQL 8.4 in Docker (OrbStack); React 19 + TypeScript, `@codemirror/lang-sql` 6.10.0 (`MySQL`), `sql-formatter` 15.9.0 (`"mysql"`).

**Spec:** docs/superpowers/specs/2026-10-17-03-mysql-mssql-design.md

## Global Constraints

- Scope is spec §5 item 1. T-SQL and SQL Server are 03b: no `Dialect.TSQL`, no `adapters/mssql/`, no `GO` handling, no `set_message_listener`, no `StatementSpan.repeat`, no `synonym` kind. The only 03b placeholder added here is the `server_read_only` capability field (03b fills its UI note).
- Branch `feat/03-mysql-mssql` (base 20bec5e). 03b continues on the same branch; do not merge to `main` in 03a.
- Python `>=3.13,<3.14`, poetry in-project venv (`backend/.venv`). New dependency, main group, pinned like the repo pins psycopg: `mysql-connector-python>=26.7.0,<27`. The driver always runs with `use_pure=True`. No new frontend dependencies.
- Dev MySQL: image `mysql:8.4`, `127.0.0.1:33067` → container 3306, user `dbaguy` / password `dbaguy` (root password `dbaguy`), databases `dbaguy` and `analytics`, seeded from `dev/mysql/init/*.sql`; port override `DBAGUY_TEST_MYSQL_PORT`; the bastion (`127.0.0.1:2222`, `dev`/`dev`) reaches it as `mysql:3306`.
- Start the dev databases with `docker compose -f dev/docker-compose.yml up -d --build --wait`. From Task 6 on, run every backend test with the containers up.
- The repo is public: only the fake dev credentials above. Never commit real hosts, users or passwords.
- Never touch `~/Library/Application Support/DbaGuy`. `scripts/dev.sh --web` uses `dev/data`. Ad-hoc sidecar runs use `DBAGUY_DATA_DIR=$(mktemp -d)` and profiles with `"save_password": false`, so nothing is written to the Keychain.
- User SQL is sent verbatim, without parameters, one statement per `run()` (`client_flags=[-ClientFlag.MULTI_STATEMENTS]`). Never use `cursor.execute()` for user SQL. Internal catalog queries may use driver `%s` parameters.
- Literals and identifiers come only from `sql/dialects.py` (`quote_ident`, `force_quote_ident`, `qualified`, `quote_literal`, `bool_literal`, `bytes_literal`). No hand-built quoting anywhere else, frontend included.
- `sql/` stays pure: no I/O, and SQL text is never logged above DEBUG. Every dialect table is a dict keyed by `Dialect`. Text offsets crossing HTTP are UTF-16 and are converted only in `api/sql.py` via `Utf16Index`.
- Logging is loguru. INFO: connection open (engine, host, database, TLS mode, thread id, read-only; never a password), cancel (method and milliseconds), auto-commit changes. WARNING: a failed cancel or kill, ignored option names. SQL text only at DEBUG.
- Tests cover only error-prone logic: lexer, splitter, classifier and safety, literals and quoting, value-condition, formatting, type conversion, connection settings, and adapter integration against the dev MySQL (`requires_mysql`). The frontend has no test framework; verify it with `npm run typecheck && npm run build` and a manual browser check.
- `cd backend && .venv/bin/pytest -q` must be green after every task.
- After any backend model or API change, run `npm run gen:api` and commit `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts`. Never hand-edit them. TS code imports API types only through `src/lib/api/types.ts`.
- `npm run typecheck && npm run build` must pass after every task that touches `src/` or regenerates the API types.
- Frontend: colours only through the Tailwind token classes (no hex or rgb). Any new key handler must check `isImeKeyEvent(e)` from `@/lib/ime`.
- Commit after each task. The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Bastion tests can fail when re-run within about a minute (OpenSSH PerSourcePenalties). Wait, or run `docker compose -f dev/docker-compose.yml restart bastion`.

## Review Focus

1. **Text containing `delimiter` + whitespace** (a column alias, a comment, a string) is sent byte-for-byte. mysql-connector's `cursor.execute()` runs such text through its own DELIMITER rewriter. → Task 9 `test_text_mentioning_delimiter_is_sent_verbatim`.
2. **mysqldump / Workbench scripts**: `DELIMITER ;;`, `/*!50003 CREATE*/ … */;;` wrapped triggers, `/*!40101 SET … */;` header lines, CRLF line endings. They must split exactly as the mysql client splits them. → Task 2 `test_mysqldump_style_script_with_crlf`.
3. **Identifiers with a backtick, a space, a reserved word or CJK** (`` tick`table ``, `Order Items`, `select`) must survive the tree, SHOW CREATE, the browse SELECT, the sort key, the value filter and the INSERT copy. → Task 3 quoting tests, Task 8 `test_names_with_backticks_round_trip`, Task 9 `test_browse_select_on_a_backtick_name_runs`.
4. **A result that ends in an odd state** must leave the connection usable for the next statement, with no "Commands out of sync". Cases: a `CALL` that fails after its first result set; a small result that the server has fully sent but the client has not read, then closed (KILL QUERY on an idle thread); a cancel that arrives when nothing is running. → Task 9 `test_procedure_failing_after_a_result_set_leaves_the_connection_usable`, `test_closing_a_fully_sent_result_does_not_poison_the_next_statement`, `test_cancel_with_nothing_running_is_harmless`.
5. **Cancel through an SSH tunnel.** The KILL side connection must reach the server through the same tunnel endpoint, with the same TLS settings. → Task 9 `test_mysql_through_the_ssh_bastion_including_cancel`.

## Pain points this plan must address

From `docs/research/2026-10-15-feature-priorities.md`. Every phase review checks its task against these rows as well as against plan completeness.

| Source row | How 03a addresses it, or avoids regressing it |
|---|---|
| §2 Q1 輕、快、低記憶體 (§8: 又重又慢) | Pure-Python driver (469 KB) with the C extension kept out of the bundle (Task 11). Results stream unbuffered page by page and are never read in full (Task 9). The snapshot uses 7 fixed queries and is capped at 10,000 relations (Task 8). Streaming 100k rows is measured against PG with a 3× stop rule (Task 9). Startup is re-measured (Task 11). |
| §2 Q2 穩定、不丟工作 (§8: 當機或丟失工作) | Every driver error becomes a `StatementError`, and `connection_lost` drives the existing Reconnect UI. Closing or cancelling never wedges the connection: KILL + drain, no "Commands out of sync" (Task 9). A MySQL session lives on its own worker thread, so it cannot block other tabs. Workspace persistence is untouched. |
| §2 Q5 多引擎從第一天 (§8: 要切換好幾個工具) | MySQL plugs into the unchanged adapter contract with no engine branches in services. The UI reads `EngineInfo` capabilities instead of `dialect === "postgres"` (Tasks 5 and 10). Every PG/SQLite flow (tree, table data, query, copy, export, history, transactions) works on MySQL, so Navicat or Workbench is not needed. |
| §3 M1 連線管理 (SSH, SSL/CA, URL import) | SSH tunnel incl. cancel (Task 9 bastion test). Every `SslConfig` mode is mapped, with fail-closed `verify-full` through a tunnel and `require` enforced (Task 7). `mysql://`, `mysql+…://`, `jdbc:mysql://` import with `ssl-mode`/`sslmode`/`useSSL` (Tasks 5 and 7). Per-engine SSL labels and URL example (Task 10). |
| §3 M2 完整的物件瀏覽 (procedure, trigger, DDL, metadata speed) | Databases → Tables/Views/Functions/Procedures/Triggers/Events; table → Columns/Indexes/Foreign keys/Triggers. Server DDL comes from `SHOW CREATE`; index and FK leaves get generated DDL (Task 8). One metadata connection serves every database. |
| §3 M5 明確的執行範圍 (`DELIMITER`, batch semantics) | `DELIMITER` lines are consumed and never sent; compound bodies keep their inner `;`; the highlighted range is exactly what is sent (Task 2; manual check in Task 10). |
| §3 M6 Schema-aware 補全 (fast on big schemas) | MySQL keywords, backtick quoting on apply, `#` comments and executable comments silence completion, index hints are not aliases (Task 3). The snapshot is fixed-cost and capped (Task 8). |
| §3 M7 結果 grid 值的呈現 | Text-first type table: big unsigned ints exact, DECIMAL as text, zero dates kept as `0000-00-00`, TIME beyond 24 h, JSON as JSON, binary as bytes (hex preview), BIT as int (Task 9). |
| §3 M9 複製與匯出 | INSERT and IN-list copy and export use MySQL literals: backticks, `X'…'`, backslash-safe strings (Task 3). Export streams through the unchanged worker; 500k-row CSV in the manual check (Task 10). |
| §3 M10 錯誤訊息 (raw error, exact line) | The server message plus SQLSTATE, the errno as the code chip, `at line N` → `ErrorPosition(N, 1)` (Task 9). |
| §3 M11 安全 (DB-level read-only, confirmations, transaction indicator, idle rollback) | Server-side `SET SESSION TRANSACTION READ ONLY` plus the classifier and allow-list; the `/*! */`, `INTO OUTFILE` and `LOAD DATA` holes are closed (Tasks 4 and 9). The transaction badge comes from the protocol flag without round trips, so idle warning and rollback work unchanged (Task 9). |
| §3 M12 取消查詢與每個 tab 的 timeout | `KILL QUERY` from a side connection, within 1 s, and the transaction survives. The worker timeout is reused (Task 9). The export-cancel warning is shown only where cancel really aborts the transaction (Task 10). |
| §3 S5 Visual EXPLAIN (MySQL last) | Not in 03a. Not regressed: `EXPLAIN`/`EXPLAIN ANALYZE` results show in the grid and are classified correctly, including `FORMAT=…` (Task 4). |
| §6 MySQL 5.7 information_schema is slow → lazy per schema | MySQL < 8 is out of scope (spec §1). The 10,000-relation cap loads only the current database on huge servers and logs it (Task 8). Lazy per-schema loading stays deferred (Ruling 19). |
| §7 記下 `CONNECTION_ID()` | The handshake thread id is kept for `KILL QUERY` and logged at open (Task 9). |
| §7 開啟 TCP keepalive | SO_KEEPALIVE with 30/10/3, as for PG, set on the driver's socket (Task 9). |
| §7 不要多開連線 | One metadata connection per MySQL profile (not per database). The kill connection exists only for the milliseconds of a cancel or early close (Task 9). |
| §7 重連時明確告知 session 狀態已遺失 | Dropped connections map to `connection_lost` (errno set plus "connection not available"), so the existing Reconnect flow says so (Task 9). |
| §7 使用者 SQL 一律 `params=None` | Stronger than required: a verbatim cursor bypasses the driver's parameter and DELIMITER rewriting entirely (Task 9). |
| §7 MySQL `CALL` 後要迴圈 `nextset()` | `next_result()` walks every set; early close drains later sets and never calls `nextset()` after an error (Task 9 tests). |
| §7 吃掉 `DELIMITER` 行；MySQL 不支援巢狀註解 | Tasks 1–2 (lexer `nested_block_comments=False` for MySQL, splitter `DELIMITER` lines). |
| §7 MySQL unbuffered tuple cursor; 串流期間整條連線都被佔住 | Raw unbuffered cursor. `in_transaction()`/`autocommit()` never send a query while a result is open (Task 9). |
| §7 MySQL DATE/DATETIME 以字串讀取 (`0000-00-00`) | Task 9 type table plus a seeded zero date (Task 6). |
| §7 大整數和 Decimal 以字串傳到前端 | Ints are converted to Python int and pass through the unchanged `encode_value` (> 2^53 → text); DECIMAL stays text (Task 9). |
| §7 arm64 每個 Mach-O 都要有效的 ad-hoc 簽章；hidden imports | The C extension `.so` is excluded, hidden imports for sqlglot MySQL and mysql.connector are added, and the bundle is verified with `codesign --verify --deep --strict` plus a live MySQL query from the frozen sidecar (Task 11). |

---

## File Structure

```
backend/dbaguy_backend/
  domain/dialect.py            Dialect + MYSQL
  domain/metadata.py           NodeKind.EVENT/FOREIGN_KEY, Folder.EVENTS/FOREIGN_KEYS
  domain/profiles.py           EngineInfo + per_database_connection, cancel_aborts_transaction, server_read_only
  sql/dialects.py              LexRules (named fields), BodyRule, ClientCommand, RULES; reserved words; the one literal
                               and identifier source (quote_ident, force_quote_ident, quote_literal, bool/bytes_literal)
  sql/lexer.py                 scan()/tokenize(): MySQL strings, comments, executable comments, @variables, custom delimiter
  sql/splitter.py              body trackers (PG/SQLite, MySQL compound), DELIMITER lines
  sql/classifier.py            MySQL holes: version comments, INTO OUTFILE/DUMPFILE, LOAD/HANDLER, EXPLAIN FORMAT
  sql/sqlglot_rules.py         MySQL dialect by name; classify_expression(node, dialect)
  sql/formatting.py            literals via dialects.py; value_condition()
  sql/select_builder.py        filter guard rejects executable comments
  sql/completion.py            MySQL keyword lists; # comments and executable comments silence completion
  sql/scope.py                 MySQL index hints stop alias reading
  services/safety.py           per-dialect read-only SESSION allow-lists (PG, MySQL)
  adapters/base.py             AdapterCapabilities + cancel_aborts_transaction, server_read_only; info()
  adapters/registry.py         strips a jdbc: prefix
  adapters/sql_text.py         quote_literal_pg, quote_ident_mysql; quote_ident_sqlite via force_quote_ident
  adapters/postgres/adapter.py capabilities (cancel_aborts_transaction=True)
  adapters/postgres/ddl.py     quote_literal_pg
  adapters/mysql/__init__.py
  adapters/mysql/settings.py   URL import, connect/kill parameters, TLS mapping, options (pure)
  adapters/mysql/catalog.py    catalog query helper, system schemas, missing-object errnos
  adapters/mysql/introspection.py  object tree and DDL
  adapters/mysql/schema.py     schema snapshot (fixed queries, relation cap)
  adapters/mysql/types.py      field type → type name, category, converter
  adapters/mysql/adapter.py    MysqlAdapter, MysqlConnection, MysqlResult, verbatim cursor, cancel/close
  api/sql.py                   POST /sql/value-condition
  container.py                 registers MysqlAdapter
backend/dbaguy-backend.spec    sqlglot MySQL + mysql.connector (pure) for PyInstaller
backend/pyproject.toml, poetry.lock
backend/tests/support.py       MySQL probe, markers, profile and driver helpers
backend/tests/test_mysql_settings.py, test_mysql_introspection.py, test_mysql_types.py,
             test_mysql_adapter.py, test_mysql_sessions.py   (new)
dev/docker-compose.yml         mysql service; bastion depends on it
dev/mysql/init/00-schema.sql, 01-routines.sql, 02-data.sql   (new seed)
src/features/connections/hooks.ts           useEngineInfo
src/features/connections/ConnectionDialog.tsx  per-engine URL example and SSL labels
src/features/query/ConnectionPicker.tsx     database picker by capability
src/features/session/SessionController.ts   public profileId
src/features/grid/ExportDialog.tsx, src/features/session/SessionGuards.tsx, src/features/session/transactions.ts
src/features/table-data/filterValue.ts, TableDataTab.tsx   value condition from the backend
src/features/explorer/kinds.tsx             event / foreign_key icons and labels
src/features/editor/extensions.ts, format.ts   MySQL CodeMirror dialect and formatter language
CLAUDE.md                                   dev DB, architecture notes, limits, progress
```

Task order follows dependencies: the `sql/` rule tables (1–4) come first, because both the adapter and the frontend consume them. The contract change (5) precedes the dev DB (6), which brings the driver dependency that settings (7) and introspection (8) need. Introspection is built on a raw driver connection before the adapter (9), so the adapter task can wire a finished tree, keep its own scope to execution, and register the engine complete. The frontend (10) needs the endpoint, the capabilities and a working adapter. Packaging (11) verifies everything in the bundle.

---

### Task 1: `Dialect.MYSQL`, named lexer rules and the MySQL lexer

**Files:**
- Modify: `backend/dbaguy_backend/domain/dialect.py`
- Modify: `backend/dbaguy_backend/sql/dialects.py` (`BodyRule`, `ClientCommand`, `LexRules`, `RULES`)
- Modify: `backend/dbaguy_backend/sql/lexer.py`
- Modify: `backend/dbaguy_backend/sql/splitter.py` (only `RULES[dialect].body_rule` replaces `block_bodies`)
- Modify: `backend/dbaguy_backend/sql/sqlglot_rules.py` (`SQLGLOT_DIALECT[Dialect.MYSQL]`)
- Modify: `src/features/editor/extensions.ts`, `src/features/editor/format.ts`
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`
- Test: `backend/tests/sql/test_lexer.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Dialect.MYSQL = "mysql"`.
  - `class BodyRule(StrEnum)` with `BEGIN_ATOMIC = "begin_atomic"`, `TRIGGER = "trigger"`, `COMPOUND = "compound"`; `class ClientCommand(StrEnum)` with `DELIMITER = "delimiter"` (both in `sql/dialects.py`).
  - `LexRules` fields: `nested_block_comments`, `line_comment_end`, `dash_comment_needs_space`, `hash_comments`, `executable_comments`, `dollar_quotes`, `dollar_params`, `escape_string_prefixes`, `backslash_escapes`, `string_quotes`, `identifier_quotes`, `variable_prefix`, `body_rule`, `client_command`.
  - `TokenKind.EXEC_COMMENT = "exec_comment"` (not in `INSIGNIFICANT`); `TokenKind.DELIMITER = "delimiter"`.
  - `scan(text: str, dialect: Dialect, start: int = 0, delimiter: str | None = None) -> Iterator[Token]`. `tokenize`, `first_token` and `first_keyword` keep their signatures.
  - `SQLGLOT_DIALECT[Dialect.MYSQL] is MySQL`.

Why the extra edits: adding `Dialect.MYSQL` breaks `test_every_dialect_parses_through_sqlglot_resolved_by_import[mysql]` and `test_dialects_survive_a_bundle_without_sqlglots_by_name_lookups` unless `SQLGLOT_DIALECT` has MySQL, so the line goes in here. After `npm run gen:api`, `SQL_DIALECTS: Record<Dialect, SQLDialect>` in `extensions.ts` fails typecheck until it has a `mysql` entry.

- [ ] **Step 1: Write the failing tests** — append to `backend/tests/sql/test_lexer.py`:

```python
from dbaguy_backend.sql.lexer import INSIGNIFICANT, scan

MY = Dialect.MYSQL


def test_mysql_strings_escape_with_backslashes_and_double_quotes_are_strings():
    assert kinds(r"'it\'s; ok' 'a''b' '\\'", MY) == [
        (K.STRING, r"'it\'s; ok'"), (K.STRING, "'a''b'"), (K.STRING, r"'\\'"),
    ]
    assert kinds('"a;b" "x\\"y" "p""q"', MY) == [(K.STRING, '"a;b"'), (K.STRING, '"x\\"y"'), (K.STRING, '"p""q"')]
    assert kinds("`a``b;c`", MY) == [(K.QUOTED, "`a``b;c`")]
    assert kinds('"x"', PG) == [(K.QUOTED, '"x"')]  # still an identifier in PostgreSQL


def test_mysql_comments():
    assert kinds("SELECT 1 # c; x\nFROM t", MY) == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.COMMENT, "# c; x"), (K.WORD, "FROM"), (K.WORD, "t"),
    ]
    # `--` is a comment only before whitespace, a control character or the end: 1--1 is 1 - (-1)
    assert kinds("SELECT 1--1", MY) == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.OTHER, "-"), (K.OTHER, "-"), (K.NUMBER, "1"),
    ]
    assert kinds("x -- c\ny", MY)[1] == (K.COMMENT, "-- c")
    assert kinds("x --\ty", MY)[1] == (K.COMMENT, "--\ty")
    assert kinds("x --", MY)[-1] == (K.COMMENT, "--")
    assert kinds("-- c\r; x", MY)[0] == (K.COMMENT, "-- c\r; x")  # a line comment ends at LF only
    assert kinds("/* a /* b */ c */", MY)[0] == (K.COMMENT, "/* a /* b */")  # no nesting
    assert kinds("a # b") == [(K.WORD, "a"), (K.OTHER, "#"), (K.WORD, "b")]  # PostgreSQL: an operator


def test_mysql_executable_comments_are_code():
    assert kinds("/*!40101 SET NAMES utf8 */;", MY) == [(K.EXEC_COMMENT, "/*!40101 SET NAMES utf8 */"), (K.SEMI, ";")]
    assert kinds("SELECT /*+ BKA(t) */ 1", MY)[1] == (K.EXEC_COMMENT, "/*+ BKA(t) */")
    assert kinds("/*! x */") == [(K.COMMENT, "/*! x */")]  # PostgreSQL: an ordinary comment
    assert K.EXEC_COMMENT not in INSIGNIFICANT


def test_mysql_variables_are_words():
    assert kinds("SET @x := @@session.sql_mode", MY) == [
        (K.WORD, "SET"), (K.WORD, "@x"), (K.OTHER, ":"), (K.OTHER, "="), (K.WORD, "@@session"), (K.DOT, "."),
        (K.WORD, "sql_mode"),
    ]
    assert kinds("@'v' @`w`", MY) == [(K.WORD, "@"), (K.STRING, "'v'"), (K.WORD, "@"), (K.QUOTED, "`w`")]
    assert kinds("@x") == [(K.OTHER, "@"), (K.WORD, "x")]  # PostgreSQL


def test_dollar_params_and_quotes_are_postgres_only():
    assert kinds("a$b $1 $$x$$", MY) == [
        (K.WORD, "a$b"), (K.OTHER, "$"), (K.NUMBER, "1"), (K.OTHER, "$"), (K.OTHER, "$"), (K.WORD, "x$$"),
    ]


def scanned(text, delimiter, start=0):
    return [(t.kind, t.text) for t in scan(text, MY, start, delimiter) if t.kind != K.SPACE]


def test_scan_with_a_custom_delimiter():
    assert scanned("END$$ x//", "$$") == [
        (K.WORD, "END"), (K.DELIMITER, "$$"), (K.WORD, "x"), (K.OTHER, "/"), (K.OTHER, "/"),
    ]
    assert scanned("SELECT 1// 'a//b' /* // */", "//") == [
        (K.WORD, "SELECT"), (K.NUMBER, "1"), (K.DELIMITER, "//"), (K.STRING, "'a//b'"), (K.COMMENT, "/* // */"),
    ]
    assert scanned("x;;y;z", ";;") == [(K.WORD, "x"), (K.DELIMITER, ";;"), (K.WORD, "y"), (K.SEMI, ";"), (K.WORD, "z")]
    assert scanned("abcXYdef", "XY") == [(K.WORD, "abc"), (K.DELIMITER, "XY"), (K.WORD, "def")]
    assert scanned("1253", "5") == [(K.NUMBER, "12"), (K.DELIMITER, "5"), (K.NUMBER, "3")]
    tokens = list(scan("xx SELECT", MY, 3))
    assert (tokens[0].start, tokens[0].text) == (3, "SELECT")


def test_mysql_offsets_cover_the_text():
    text = "SELECT '中文' # 😀\nFROM `t` WHERE a = \"x\\\"y\" /*! 1 */ AND @v"
    tokens = tokenize(text, MY)
    assert "".join(t.text for t in tokens) == text
    assert all(text[t.start:t.end] == t.text for t in tokens)
```

Add these MySQL cases to the two existing parametrize lists:
- `test_unterminated_tokens_are_flagged`: `("'abc\\'", MY)`, `('"abc', MY)`, `("`abc", MY)`, `("/*! open", MY)`.
- `test_closed_tokens_are_terminated`: `(r"'a\'b'", MY)`, `('"a""b"', MY)`, `("/*!1 x */", MY)`, `("# to the end", MY)`.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_lexer.py -q`
Expected: collection error `ImportError: cannot import name 'scan'`, or `AttributeError: MYSQL`.

- [ ] **Step 3: Implement the rules.** In `domain/dialect.py`, add `MYSQL = "mysql"`. In `sql/dialects.py`, replace `LexRules` and `RULES` with:

```python
class BodyRule(StrEnum):
    """Which statements hold a body whose inner `;` does not end the statement (see splitter)."""

    BEGIN_ATOMIC = "begin_atomic"  # PostgreSQL: CREATE [OR REPLACE] FUNCTION|PROCEDURE … BEGIN ATOMIC … END
    TRIGGER = "trigger"  # SQLite: CREATE [TEMP|TEMPORARY] TRIGGER … BEGIN … END
    COMPOUND = "compound"  # MySQL: CREATE … PROCEDURE|FUNCTION|TRIGGER|EVENT … BEGIN … END, nested blocks


class ClientCommand(StrEnum):
    """A line the client consumes itself: the server never sees it."""

    DELIMITER = "delimiter"  # MySQL: `DELIMITER $$` changes the statement terminator


@dataclass(frozen=True)
class LexRules:
    nested_block_comments: bool
    line_comment_end: re.Pattern[str]  # ends a `--` or `#` comment
    dash_comment_needs_space: bool  # MySQL: `--` starts a comment only before whitespace, a control char or the end
    hash_comments: bool  # MySQL: `#` starts a line comment
    executable_comments: bool  # MySQL: `/*! … */` and `/*+ … */` are code (EXEC_COMMENT), not comments
    dollar_quotes: bool  # PostgreSQL $tag$…$tag$ strings
    dollar_params: bool  # PostgreSQL $1 parameters
    escape_string_prefixes: frozenset[str]  # PostgreSQL E'…': backslash escapes in that string only
    backslash_escapes: bool  # MySQL (default sql_mode): backslash escapes in every string
    string_quotes: frozenset[str]  # characters that open a string
    identifier_quotes: tuple[tuple[str, str], ...]  # (open, close)
    variable_prefix: str  # starts a variable word (MySQL "@": @x, @@session.x); "" for none
    body_rule: BodyRule
    client_command: ClientCommand | None = None


RULES: dict[Dialect, LexRules] = {
    Dialect.POSTGRES: LexRules(
        nested_block_comments=True, line_comment_end=re.compile(r"[\n\r]"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=True, dollar_params=True,
        escape_string_prefixes=frozenset({"e", "E"}), backslash_escapes=False, string_quotes=frozenset("'"),
        identifier_quotes=(('"', '"'),), variable_prefix="", body_rule=BodyRule.BEGIN_ATOMIC,
    ),
    Dialect.SQLITE: LexRules(
        nested_block_comments=False, line_comment_end=re.compile(r"\n"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=False, string_quotes=frozenset("'"),
        identifier_quotes=(('"', '"'), ("[", "]"), ("`", "`")), variable_prefix="", body_rule=BodyRule.TRIGGER,
    ),
    Dialect.MYSQL: LexRules(
        nested_block_comments=False, line_comment_end=re.compile(r"\n"), dash_comment_needs_space=True,
        hash_comments=True, executable_comments=True, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=True, string_quotes=frozenset("'\""),
        identifier_quotes=(("`", "`"),), variable_prefix="@", body_rule=BodyRule.COMPOUND,
        client_command=ClientCommand.DELIMITER,
    ),
}
```

- [ ] **Step 4: Implement the lexer.** In `sql/lexer.py`:
  - Add `EXEC_COMMENT` (after `COMMENT`) and `DELIMITER` (after `SEMI`) to `TokenKind`, with these comments: `# MySQL /*! … */ and /*+ … */: code for the server, never INSIGNIFICANT` and `# a custom terminator from a MySQL DELIMITER line (only when scan() is given one)`.
  - Generalise `_scan_string` to `_scan_string(text, i, quote, backslash)`.
  - Rename `_scan` to the public `scan` with the start and delimiter parameters below.
  - Keep `tokenize = list(scan(text, dialect))`. `first_token` uses `scan`.

```python
def _scan_string(text: str, i: int, quote: str, backslash: bool) -> tuple[int, bool]:
    """i points at the opening quote; returns (index after the closing quote or len(text), whether it closed).
    A doubled quote stands for one; with `backslash`, a backslash escapes the next character."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == quote:
            if i + 1 < n and text[i + 1] == quote:
                i += 2
                continue
            return i + 1, True
        i += 1
    return n, False


def _starts_line_comment(text: str, i: int, rules: LexRules) -> bool:
    if text[i] == "#":
        return rules.hash_comments
    if not text.startswith("--", i):
        return False
    if not rules.dash_comment_needs_space:
        return True
    following = text[i + 2:i + 3]
    return following == "" or following.isspace() or ord(following) < 32


def scan(text: str, dialect: Dialect, start: int = 0, delimiter: str | None = None) -> Iterator[Token]:
    """Tokens of text[start:], lazily; their concatenated texts equal text[start:]. Never raises.

    `delimiter`: a custom statement terminator set by a MySQL `DELIMITER` line. Outside strings, quoted names and
    comments it is one DELIMITER token wherever it begins, even inside what would otherwise be a word or a number
    (`END$$`), which is how the mysql client sees it.
    """
    rules = RULES[dialect]
    quotes = dict(rules.identifier_quotes)
    n = len(text)
    i = start

    def ends_here(j: int) -> bool:
        return delimiter is not None and text.startswith(delimiter, j)

    while i < n:
        ch = text[i]
        begin = i
        terminated = True
        if ends_here(i):
            i += len(delimiter)
            kind = TokenKind.DELIMITER
        elif ch.isspace():
            while i < n and text[i].isspace():
                i += 1
            kind = TokenKind.SPACE
        elif _starts_line_comment(text, i, rules):
            end = rules.line_comment_end.search(text, i)
            i = n if end is None else end.start()
            kind = TokenKind.COMMENT
        elif text.startswith("/*", i):
            executable = rules.executable_comments and text.startswith(("/*!", "/*+"), i)
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                elif rules.nested_block_comments and text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                else:
                    i += 1
            terminated = depth == 0
            kind = TokenKind.EXEC_COMMENT if executable else TokenKind.COMMENT
        elif ch in rules.string_quotes:
            i, terminated = _scan_string(text, i, ch, rules.backslash_escapes)
            kind = TokenKind.STRING
        elif ch in quotes:
            ...  # unchanged QUOTED branch
        elif ch == "$" and rules.dollar_params and (param := _DOLLAR_PARAM.match(text, i)):
            i = param.end()
            kind = TokenKind.PARAM
        elif ch == "$" and rules.dollar_quotes and (tag := _DOLLAR_TAG.match(text, i)) and not (
            i > 0 and _ident_char(text[i - 1])
        ):
            ...  # unchanged dollar-quote branch
        elif ch == rules.variable_prefix:  # "" never equals a character
            while i < n and text[i] == ch and not ends_here(i):  # @ or @@
                i += 1
            while i < n and _ident_char(text[i]) and not ends_here(i):
                i += 1
            kind = TokenKind.WORD
        elif _ident_start(ch):
            while i < n and _ident_char(text[i]) and not ends_here(i):
                i += 1
            if text[begin:i] in rules.escape_string_prefixes and i < n and text[i] == "'":
                i, terminated = _scan_string(text, i, "'", backslash=True)
                kind = TokenKind.STRING
            else:
                kind = TokenKind.WORD
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            match = _NUMBER.match(text, i)
            end = match.end() if match else i + 1
            i = next((j for j in range(i + 1, end) if ends_here(j)), end)
            kind = TokenKind.NUMBER
        else:
            i += 1
            kind = _PUNCT.get(ch, TokenKind.OTHER)
        yield Token(kind, begin, i, text[begin:i], terminated)
```

- [ ] **Step 5: Adapt the splitter and sqlglot.**
  - Splitter: `_BodyTracker.__init__(self, rule: BodyRule)` stores `self._rule`. In `_opens_body`, `"begin_atomic" in self._kinds` becomes `self._rule is BodyRule.BEGIN_ATOMIC`, and `"trigger" in self._kinds` becomes `self._rule is BodyRule.TRIGGER`. `split()` builds `_BodyTracker(RULES[dialect].body_rule)`. `BodyRule.COMPOUND` opens nothing until Task 2.
  - `sqlglot_rules.py`: add `from sqlglot.dialects.mysql import MySQL` (by name, same reason as the others) and `Dialect.MYSQL: MySQL` in `SQLGLOT_DIALECT`.

- [ ] **Step 6: Wire the frontend tripwires.**
  - Run `npm run gen:api`.
  - In `src/features/editor/extensions.ts`, import `MySQL` from `@codemirror/lang-sql` and add `mysql: MySQL` to `SQL_DIALECTS`.
  - In `src/features/editor/format.ts`, add `mysql: "mysql"` to `LANGUAGES`.

- [ ] **Step 7: Run everything**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: all green. The existing PG/SQLite lexer and splitter tests pass unchanged, and `test_every_dialect_parses_through_sqlglot_resolved_by_import[mysql]` passes.

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/domain/dialect.py backend/dbaguy_backend/sql/{dialects,lexer,splitter,sqlglot_rules}.py \
  backend/tests/sql/test_lexer.py src/features/editor/{extensions,format}.ts src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(sql): MySQL dialect with named lexer rules (backslash and double-quoted strings, # and '-- ' comments, executable comments, @variables, custom delimiter scan)"
```

---

### Task 2: Splitter: MySQL compound bodies and `DELIMITER` lines

**Files:**
- Modify: `backend/dbaguy_backend/sql/splitter.py`
- Test: `backend/tests/sql/test_splitter.py`

**Interfaces:**
- Consumes: `scan(text, dialect, start, delimiter)`, `TokenKind.DELIMITER`, `TokenKind.EXEC_COMMENT`, `RULES[d].body_rule`, `RULES[d].client_command`, `BodyRule`, `ClientCommand` (Task 1).
- Produces: `split(text, dialect) -> list[StatementSpan]` (signature and `StatementSpan` unchanged). For MySQL:
  - a `DELIMITER x` line belongs to no span;
  - under a custom delimiter, a span's `text` excludes the delimiter, and `terminator_end` is after it;
  - `has_body=True` for stored programs with a `BEGIN … END` body.

- [ ] **Step 1: Write the failing tests** — append to `backend/tests/sql/test_splitter.py` (with `MY = Dialect.MYSQL`):

```python
def test_mysql_delimiter_lines_are_consumed_and_change_the_terminator():
    sql = (
        "DELIMITER $$\n"
        "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND$$\n"
        "DELIMITER ;\n"
        "CALL p();\nSELECT 3"
    )
    spans = split(sql, MY)
    assert [s.text for s in spans] == ["CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND", "CALL p()", "SELECT 3"]
    assert sql[spans[0].end:spans[0].terminator_end] == "$$"
    assert spans[0].has_body and not spans[1].has_body
    assert sql[spans[1].start:spans[1].terminator_end] == "CALL p();"


def test_a_custom_delimiter_ends_statements_only_outside_literals_and_comments():
    sql = "delimiter //\nSELECT 'a//b'; SELECT 2 /* // */ //\nSELECT 3 // -- the rest is a comment\nSELECT `x//y`//"
    # a span ends at its last significant token: the trailing comment stays outside, as for `;`
    assert texts(sql, MY) == ["SELECT 'a//b'; SELECT 2", "SELECT 3", "SELECT `x//y`"]


def test_delimiter_is_a_command_only_first_on_its_line_at_a_statement_start():
    assert texts("SELECT 1; DELIMITER $$\nSELECT 2", MY) == ["SELECT 1", "DELIMITER $$\nSELECT 2"]
    assert texts("SELECT delimiter FROM t; SELECT 2", MY) == ["SELECT delimiter FROM t", "SELECT 2"]
    assert texts("SELECT 1\nDELIMITER $$\n", MY) == ["SELECT 1\nDELIMITER $$"]  # inside a statement: SQL, not a command
    assert texts("  -- setup\n  DELIMITER $$  -- comment\nSELECT 1$$", MY) == ["SELECT 1"]


_PROC = (
    "CREATE DEFINER=`root`@`%` PROCEDURE p(IN n INT)\n"
    "lbl: BEGIN\n"
    "  DECLARE i INT DEFAULT 0;\n"
    "  WHILE i < n DO SET i = i + 1; END WHILE;\n"
    "  IF i > 1 THEN SELECT 'x;y'; ELSEIF i = 1 THEN SELECT 1; ELSE SELECT 2; END IF;\n"
    "  CASE i WHEN 1 THEN SELECT 1; ELSE BEGIN SELECT 2; END; END CASE;\n"
    "  REPEAT SET i = i - 1; UNTIL i = 0 END REPEAT;\n"
    "  inner_loop: LOOP LEAVE inner_loop; END LOOP inner_loop;\n"
    "  SELECT CASE WHEN i = 0 THEN 'z' END;\n"
    "END lbl"
)


def test_mysql_compound_bodies_keep_their_semicolons_without_delimiter():
    spans = split(f"{_PROC};\nSELECT 9;", MY)
    assert [s.text for s in spans] == [_PROC, "SELECT 9"]
    assert spans[0].has_body


def test_mysql_trigger_event_and_function_bodies():
    trg = "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; SET NEW.b = 2; END"
    ev = "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN DELETE FROM log; END"
    fn = "CREATE FUNCTION f(x INT) RETURNS TEXT DETERMINISTIC BEGIN RETURN IF(x > 0, REPEAT('a', x), ''); END"
    assert texts(f"{trg}; {ev}; {fn}; SELECT 1", MY) == [trg, ev, fn, "SELECT 1"]


def test_mysql_nothing_else_opens_a_body():
    assert texts("CREATE TABLE t (event INT, begin INT); DROP TABLE x", MY) == [
        "CREATE TABLE t (event INT, begin INT)", "DROP TABLE x",
    ]
    assert texts("BEGIN; INSERT INTO t VALUES (1); COMMIT", MY) == ["BEGIN", "INSERT INTO t VALUES (1)", "COMMIT"]
    trg = "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET NEW.begin = 1"  # a column named begin
    assert texts(f"{trg}; DROP TABLE x", MY) == [trg, "DROP TABLE x"]
    assert texts("CREATE PROCEDURE p() SELECT 1; DROP TABLE x", MY) == ["CREATE PROCEDURE p() SELECT 1", "DROP TABLE x"]


def test_mysql_strings_and_comments_hide_semicolons():
    sql = "SELECT 'x\\'; DROP TABLE t; -- ' AS a; SELECT \"q;\" # c; d\n, 2; SELECT 1--1; SELECT 3"
    assert texts(sql, MY) == [
        "SELECT 'x\\'; DROP TABLE t; -- ' AS a", "SELECT \"q;\" # c; d\n, 2", "SELECT 1--1", "SELECT 3",
    ]
    # PostgreSQL has no backslash escapes in plain strings: the same text splits where its server splits it
    assert texts("SELECT 'x\\'; DROP TABLE t", PG) == ["SELECT 'x\\'", "DROP TABLE t"]


def test_mysqldump_style_script_with_crlf():  # Review Focus 2
    dump = (
        "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;\r\n"
        "DELIMITER ;;\r\n"
        "/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER trg BEFORE INSERT ON t FOR EACH ROW "
        "BEGIN SET NEW.a = 1; END */;;\r\n"
        "DELIMITER ;\r\n"
        "INSERT INTO t VALUES (1);\r\n"
    )
    assert texts(dump, MY) == [
        "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */",
        "/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER trg BEFORE INSERT ON t FOR EACH ROW "
        "BEGIN SET NEW.a = 1; END */",
        "INSERT INTO t VALUES (1)",
    ]


def test_offsets_under_a_custom_delimiter_with_cjk_and_emoji():
    sql = "DELIMITER $$\nSELECT '中😀'$$\nDELIMITER ;\nSELECT 2;"
    spans = split(sql, MY)
    assert [sql[s.start:s.end] for s in spans] == ["SELECT '中😀'", "SELECT 2"]
    assert sql[spans[0].end:spans[0].terminator_end] == "$$"


def test_executable_comments_are_statements_in_mysql_only():
    assert texts("/*!40101 SET NAMES utf8 */; SELECT 1", MY) == ["/*!40101 SET NAMES utf8 */", "SELECT 1"]
    assert texts("/*! x */; SELECT 1", PG) == ["SELECT 1"]
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_splitter.py -q`
Expected: the new MySQL tests FAIL (DELIMITER lines end up inside spans, and compound bodies are cut at their inner `;`). The existing tests still pass.

- [ ] **Step 3: Implement** — in `sql/splitter.py`, add the MySQL tracker, the tracker factory and the DELIMITER handling. `_BodyTracker` (PG/SQLite) and `_emit` stay as they are.

```python
_COMPOUND_OBJECTS = frozenset({"PROCEDURE", "FUNCTION", "TRIGGER", "EVENT"})
_UNCOUNTED_BLOCKS = frozenset({"IF", "LOOP", "WHILE", "REPEAT"})  # their END … closes no counted block
_PROGRAM_HEAD = 16  # significant tokens: CREATE OR REPLACE DEFINER = `u` @ `h` SQL SECURITY INVOKER PROCEDURE …
_DELIMITER_ARGUMENT = re.compile(r"[ \t]+(\S+)[^\n]*")  # after the word DELIMITER: the terminator, then the line


def _creates_stored_program(head: list[Token]) -> bool:
    """CREATE [OR REPLACE] [DEFINER = account] [SQL SECURITY {DEFINER|INVOKER}] PROCEDURE|FUNCTION|TRIGGER|EVENT.
    The account is user, 'user'@'host', `user`@`host`, user@host or CURRENT_USER[()], in any quoting."""
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in head]
    if words[:1] != ["CREATE"]:
        return False
    i = 1
    if words[i:i + 2] == ["OR", "REPLACE"]:
        i += 2
    if words[i:i + 1] == ["DEFINER"]:  # skip "=" and the account, up to the next keyword this grammar allows
        i = next((j for j in range(i + 1, len(words)) if words[j] in _COMPOUND_OBJECTS or words[j] == "SQL"), len(words))
    if words[i:i + 2] == ["SQL", "SECURITY"]:
        i += 3
    return i < len(words) and words[i] in _COMPOUND_OBJECTS


class _CompoundTracker:
    """MySQL stored-program bodies, nested blocks included.

    BEGIN and CASE open a block and END closes one. `END IF|LOOP|WHILE|REPEAT` closes a block that was never counted
    (IF() and REPEAT() are also functions), so the END before such a word is taken back. `END CASE` closes the CASE
    that its END already closed. Labels (`lbl: BEGIN … END lbl`) need nothing special. Only the first BEGIN of a
    stored program opens a body, and never one right after a dot (`NEW.begin`). A routine without BEGIN … END whose
    statement uses an unqualified column named `begin` still merges with what follows; the server then rejects the
    merged text (multi-statements are off), so nothing runs.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.has_body = False
        self._head: list[Token] = []
        self._after_end = False
        self._after_dot = False

    def feed(self, token: Token) -> None:
        if len(self._head) < _PROGRAM_HEAD:
            self._head.append(token)
        word = token.upper if token.kind == TokenKind.WORD else ""
        after_end, after_dot = self._after_end, self._after_dot
        self._after_end, self._after_dot = False, token.kind == TokenKind.DOT
        if after_end and word in _UNCOUNTED_BLOCKS:
            self.depth += 1  # that END closed an IF/LOOP/WHILE/REPEAT, not a counted block
        elif after_end and word == "CASE":
            pass  # END CASE: its END already closed the CASE
        elif self.depth == 0:
            if word == "BEGIN" and not after_dot and _creates_stored_program(self._head):
                self.depth, self.has_body = 1, True
        elif word in ("BEGIN", "CASE"):
            self.depth += 1
        elif word == "END":
            self.depth -= 1
            self._after_end = True


def _tracker(rule: BodyRule) -> _BodyTracker | _CompoundTracker:
    return _CompoundTracker() if rule is BodyRule.COMPOUND else _BodyTracker(rule)


def _delimiter_line(text: str, token: Token, current: list[Token]) -> re.Match[str] | None:
    """A MySQL `DELIMITER x` line: the word DELIMITER is first on its line and no statement has started."""
    if token.kind != TokenKind.WORD or token.upper != "DELIMITER":
        return None
    if any(t.kind not in INSIGNIFICANT for t in current):
        return None
    line_start = text.rfind("\n", 0, token.start) + 1
    if text[line_start:token.start].strip(" \t"):
        return None
    return _DELIMITER_ARGUMENT.match(text, token.end)


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    rules = RULES[dialect]
    commands = rules.client_command is ClientCommand.DELIMITER
    spans: list[StatementSpan] = []
    delimiter: str | None = None  # set by a DELIMITER line; None: `;` ends statements (outside bodies)
    start = 0
    while True:
        tracker = _tracker(rules.body_rule)
        current: list[Token] = []
        restart: int | None = None
        for token in scan(text, dialect, start, delimiter):
            if commands and (line := _delimiter_line(text, token, current)):
                delimiter = None if line.group(1) == ";" else line.group(1)
                restart = line.end()  # the line is consumed: it belongs to no statement and never reaches the server
                break
            if token.kind == TokenKind.DELIMITER or (
                delimiter is None and token.kind == TokenKind.SEMI and tracker.depth == 0
            ):
                _emit(spans, text, current, token.end, tracker.has_body)
                current, tracker = [], _tracker(rules.body_rule)
                continue
            current.append(token)
            if token.kind not in INSIGNIFICANT:
                tracker.feed(token)  # also under a custom delimiter: has_body tells the classifier about bodies
        if restart is None:
            _emit(spans, text, current, None, tracker.has_body)
            return spans
        start = restart
```

Import `re`, `scan` and the enums. Drop the now-unused `tokenize` import if nothing else in the module uses it.

- [ ] **Step 4: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q`
Expected: PASS, including all existing PG/SQLite splitter tests.

- [ ] **Step 5: Commit**

```bash
git add backend/dbaguy_backend/sql/splitter.py backend/tests/sql/test_splitter.py
git commit -m "feat(sql): split MySQL DELIMITER scripts and stored-program bodies"
```

---

### Task 3: Dialect tables: quoting, literals, keywords, value condition

**Files:**
- Modify: `backend/dbaguy_backend/sql/dialects.py` (quote tables, MySQL reserved words, literal functions)
- Modify: `backend/dbaguy_backend/sql/formatting.py` (use them; `value_condition`)
- Modify: `backend/dbaguy_backend/sql/select_builder.py` (reject executable comments in a filter)
- Modify: `backend/dbaguy_backend/sql/completion.py`, `backend/dbaguy_backend/sql/scope.py`
- Modify: `backend/dbaguy_backend/adapters/sql_text.py`, `backend/dbaguy_backend/adapters/postgres/ddl.py`
- Modify: `backend/dbaguy_backend/api/sql.py` (`POST /sql/value-condition`)
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`
- Test: `backend/tests/sql/test_dialects_offsets.py`, `test_formatting.py`, `test_select_builder.py`, `test_completion.py`, `backend/tests/test_core_utils.py`, `backend/tests/test_sql_api.py`

**Interfaces:**
- Consumes: `RULES[d].backslash_escapes`, `TokenKind.EXEC_COMMENT` (Task 1).
- Produces (all in `sql/dialects.py` unless noted):
  - `MYSQL_RESERVED: frozenset[str]`; `NUMERIC_LITERAL: re.Pattern[str]`.
  - `needs_quote(dialect, name) -> bool`.
  - `force_quote_ident(dialect: Dialect, name: str) -> str` (always quoted).
  - `quote_ident(dialect, name) -> str` (only when needed; MySQL uses backticks).
  - `qualified(dialect, schema | None, name) -> str` (unchanged signature).
  - `quote_literal(dialect: Dialect, value: str) -> str` (**signature changed**: the dialect comes first).
  - `bool_literal(dialect, value: bool) -> str`; `bytes_literal(dialect, hex_digits: str) -> str`.
  - `formatting.value_condition(dialect: Dialect, column: str, value: Any, category: ColumnCategory) -> str | None`.
  - `sql_text.quote_literal_pg(value: str) -> str`, `sql_text.quote_ident_mysql(name: str) -> str` (always quoted), `sql_text.quote_ident_sqlite` (same behaviour, now via `force_quote_ident`).
  - `POST /sql/value-condition`: request `ValueConditionRequest {dialect: Dialect, column: str, value: Any, category: ColumnCategory}`, response `ValueConditionResponse {condition: str | None}`.
  - `STATEMENT_KEYWORDS[MYSQL]`, `CLAUSE_KEYWORDS[MYSQL]`.

Breaks and fixes (both because `quote_literal` takes the dialect now):
- `tests/sql/test_dialects_offsets.py:31` changes to `quote_literal(PG, "it's")`.
- `tests/test_core_utils.py:10,73` import and call `quote_literal_pg`.
- The completion fuzz test's `@pytest.mark.parametrize("dialect", [PG, LITE])` becomes `list(Dialect)`.

- [ ] **Step 1: Write the failing tests.** Imports to add:
- `test_dialects_offsets.py`: `bool_literal, bytes_literal, force_quote_ident`;
- `test_formatting.py`: `value_condition` (from `formatting`);
- `test_completion.py`: `CLAUSE_KEYWORDS` (from `completion`), `INSIGNIFICANT, tokenize` (from `lexer`), `scope_at` (from `scope`);
- `test_select_builder.py`: `ValidationFailed` (if missing).

`tests/sql/test_dialects_offsets.py`:
- Add `MY = Dialect.MYSQL`.
- Add these rows to `test_quote_ident`: `(MY, "users", "users")`, `(MY, "Users", "Users")`, `(MY, "order", "`order`")`, `(MY, "Order Items", "`Order Items`")`, `(MY, "a`b", "`a``b`")`, `(MY, "a$b", "a$b")`, `(MY, "1abc", "`1abc`")`, `(MY, "中文", "`中文`")`, `(MY, "delimiter", "delimiter")`.
- Fix `quote_literal` in `test_qualified_and_literal` as above.
- Add:

```python
def test_literals_per_dialect():
    assert quote_literal(PG, "it's \\ ok") == "'it''s \\ ok'"
    assert quote_literal(LITE, "a\\b") == "'a\\b'"
    assert quote_literal(MY, "it's \\ ok") == "'it''s \\\\ ok'"  # MySQL reads \ as an escape: doubled
    assert qualified(MY, "dbaguy", "Order Items") == "dbaguy.`Order Items`"
    assert force_quote_ident(MY, "x") == "`x`" and force_quote_ident(LITE, 'a"b') == '"a""b"'
    assert [bool_literal(d, True) for d in (PG, LITE, MY)] == ["TRUE", "1", "TRUE"]
    assert [bool_literal(d, False) for d in (PG, LITE, MY)] == ["FALSE", "0", "FALSE"]
    assert [bytes_literal(d, "dead") for d in (PG, LITE, MY)] == ["'\\xdead'::bytea", "X'dead'", "X'dead'"]


@pytest.mark.parametrize("dialect", list(Dialect))
def test_every_dialect_has_an_entry_in_every_dialect_table(dialect):  # tripwire for 03b's T-SQL
    from dbaguy_backend.sql import completion, dialects, sqlglot_rules

    tables = [
        dialects.RULES, dialects._IDENT_QUOTES, dialects._SIMPLE_IDENT, dialects._RESERVED, dialects._BOOL_LITERALS,
        dialects._BYTES_LITERALS, sqlglot_rules.SQLGLOT_DIALECT, completion.STATEMENT_KEYWORDS,
        completion.CLAUSE_KEYWORDS,
    ]
    assert all(dialect in table for table in tables)
```

`tests/sql/test_formatting.py` (`MY = Dialect.MYSQL`):

```python
def test_insert_in_mysql():
    rows = [[1, "it's \\ ok", True], [2, None, False]]
    result = fmt(F.INSERT, rows=rows, dialect=MY, table_schema="dbaguy", table_name="Order Items")
    assert result.text.splitlines() == [
        r"INSERT INTO dbaguy.`Order Items` (id, note, ok) VALUES (1, 'it''s \\ ok', TRUE);",
        "INSERT INTO dbaguy.`Order Items` (id, note, ok) VALUES (2, NULL, FALSE);",
    ]
    cols = [FormatColumn(name="b", category=C.BINARY)]
    assert fmt(F.INSERT, rows=[[b"\xde\xad"]], cols=cols, source="raw", dialect=MY).text.endswith("(X'dead');")


@pytest.mark.parametrize("dialect, column, value, category, expected", [
    (PG, "note", None, C.TEXT, "note IS NULL"),
    (PG, "Select", "it's", C.TEXT, "\"Select\" = 'it''s'"),
    (PG, "ok", True, C.BOOL, "ok = TRUE"),
    (LITE, "ok", False, C.BOOL, "ok = 0"),
    (MY, "flag", True, C.BOOL, "flag = TRUE"),
    (MY, "order", 42, C.NUMBER, "`order` = 42"),
    (MY, "ratio", 1.5, C.NUMBER, "ratio = 1.5"),
    (MY, "amount", "12345678901234567890.1234567890", C.NUMBER, "amount = 12345678901234567890.1234567890"),
    (MY, "code", "1e3; DROP TABLE t", C.NUMBER, "code = '1e3; DROP TABLE t'"),  # not a number: a string
    (MY, "note", "it's \\ back", C.TEXT, r"note = 'it''s \\ back'"),
    (MY, "a`b", "x", C.TEXT, "`a``b` = 'x'"),
    (MY, "data", {"$type": "bytes", "size": 2, "preview": "dead"}, C.BINARY, "data = X'dead'"),
    (MY, "data", {"$type": "bytes", "size": 100, "preview": "dead"}, C.BINARY, None),  # only a preview
    (MY, "body", {"$type": "text", "preview": "x", "size": 70000, "truncated": True}, C.TEXT, None),
])
def test_value_condition(dialect, column, value, category, expected):
    assert value_condition(dialect, column, value, category) == expected


def test_value_condition_rejects_an_object_that_is_not_a_cell():
    with pytest.raises(ValidationFailed):
        value_condition(MY, "x", {"$type": "nope"}, C.TEXT)
```

`tests/sql/test_select_builder.py`:
- Add `MY_PATH = path(("schema", "dbaguy"), ("folder", "tables"), ("table", "Order Items"))` and `MY_BASE = "SELECT * FROM dbaguy.`Order Items`\nWHERE "`.
- Extend the base selection in the three existing parametrized tests to `{PG: PG_PATH, LITE: LITE_PATH, MY: MY_PATH}` and the matching base strings.
- Add these cases:
  - accepted: `(MY, r"s = 'a\';b' AND t = 1", r"s = 'a\';b' AND t = 1")`, `(MY, 'n = "x;y"', 'n = "x;y"')`, `(MY, "x = 1 # ; DROP TABLE t\n AND y = 2", "x = 1  \n AND y = 2")`, `(MY, "`a;b` = 1", "`a;b` = 1")`;
  - second statement rejected: `(MY, "x = 1; DROP TABLE t")`, `(MY, r"x = 'a\'' ; DROP TABLE t")`;
  - unterminated rejected: `(MY, r"x = 'a\'")`, `(MY, 'x = "abc')`, `(MY, "x = `abc")`.
- Add:

```python
def test_executable_comments_are_not_allowed_in_a_filter():
    with pytest.raises(ValidationFailed, match="Executable comments"):
        build_browse_select(MY, MY_PATH, "x = 1 /*! OR 1 = 1 */", [])


def test_mysql_sort_keys_are_backtick_quoted():
    assert build_browse_select(MY, MY_PATH, None, [SortKey(column="a`b")]) == (
        "SELECT * FROM dbaguy.`Order Items`\nORDER BY `a``b` ASC"
    )
```

`tests/sql/test_completion.py`:
- Make the fuzz test parametrize over `list(Dialect)`.
- Add `"#", "@x", "/*!", "\\'", "DELIMITER"` to `_FUZZ_PIECES`.
- Add:

```python
MY = Dialect.MYSQL


def test_mysql_completion_quotes_with_backticks_and_knows_its_keywords():
    snapshot = SchemaSnapshot(
        database=None, default_schema="dbaguy", search_path=["dbaguy"], built_at=datetime.now(UTC),
        schemas=[SnapshotSchema(name="dbaguy", relations=[table("Order Items", [("select", "varchar(50)"), ("id", "int")])])],
    )
    index = SnapshotIndex(snapshot, MY)
    item = next(i for i in complete("SELECT * FROM Ord", 17, MY, index).items if i.label == "Order Items")
    assert item.apply == "`Order Items`"
    text = "SELECT `Order Items`. FROM `Order Items`"
    items = complete(text, text.index(". ") + 1, MY, index).items
    assert [i.label for i in items] == ["select", "id"] and items[0].apply == "`select`"
    assert "SHOW" in [i.label for i in complete("SHO", 3, MY, None).items]
    assert "ON DUPLICATE KEY UPDATE" in CLAUSE_KEYWORDS[MY] and "RETURNING" not in CLAUSE_KEYWORDS[MY]
    assert complete("SELECT 1 # fro", 14, MY, index).items == []  # inside a # comment
    assert complete("SELECT /*! fro", 14, MY, index).items == []  # inside an executable comment


def test_mysql_index_hints_are_not_aliases():
    sql = "SELECT * FROM orders USE INDEX (ix) WHERE "
    [ref] = scope_at([t for t in tokenize(sql, MY) if t.kind not in INSIGNIFICANT], len(sql)).relations
    assert (ref.name, ref.alias) == ("orders", None)
```

`tests/test_sql_api.py`:

```python
def test_value_condition_endpoint(client):
    body = {"dialect": "mysql", "column": "order", "value": "it's", "category": "text"}
    assert client.post("/sql/value-condition", json=body, headers=AUTH).json() == {"condition": "`order` = 'it''s'"}
    preview = {**body, "value": {"$type": "text", "preview": "x", "size": 70000, "truncated": True}}
    assert client.post("/sql/value-condition", json=preview, headers=AUTH).json() == {"condition": None}
    assert client.post("/sql/value-condition", json={**body, "extra": 1}, headers=AUTH).status_code == 422
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql tests/test_sql_api.py tests/test_core_utils.py -q`
Expected: FAIL. Typical errors: `ImportError` (`force_quote_ident`, `bool_literal`, `value_condition`), `KeyError: <Dialect.MYSQL>`, and 404 for `/sql/value-condition`.

- [ ] **Step 3: Implement the literal source in `sql/dialects.py`.**

```python
MYSQL_RESERVED = frozenset(  # MySQL 8.4 reserved words (+ a few newer ones): an extra one only costs a quote
    """accessible add all alter analyze and as asc asensitive before between bigint binary blob both by call cascade case
    change char character check collate column condition constraint continue convert create cross cube cume_dist
    current_date current_time current_timestamp current_user cursor database databases day_hour day_microsecond
    day_minute day_second dec decimal declare default delayed delete dense_rank desc describe deterministic distinct
    distinctrow div double drop dual each else elseif empty enclosed escaped except exists exit explain false fetch
    first_value float float4 float8 for force foreign from fulltext function generated get grant group grouping groups
    having high_priority hour_microsecond hour_minute hour_second if ignore in index infile inner inout insensitive
    insert int int1 int2 int3 int4 int8 integer intersect interval into io_after_gtids io_before_gtids is iterate join
    json_table key keys kill lag last_value lateral lead leading leave left like limit linear lines load localtime
    localtimestamp lock long longblob longtext loop low_priority manual master_bind master_ssl_verify_server_cert match
    maxvalue mediumblob mediumint mediumtext middleint minute_microsecond minute_second mod modifies natural not
    no_write_to_binlog nth_value ntile null numeric of on optimize optimizer_costs option optionally or order out outer
    outfile over parallel partition percent_rank precision primary procedure purge qualify range rank read reads
    read_write real recursive references regexp release rename repeat replace require resignal restrict return revoke
    right rlike row rows row_number schema schemas second_microsecond select sensitive separator set show signal
    smallint spatial specific sql sqlexception sqlstate sqlwarning sql_big_result sql_calc_found_rows sql_small_result
    ssl starting stored straight_join system table tablesample terminated then tinyblob tinyint tinytext to trailing
    trigger true undo union unique unlock unsigned update usage use using utc_date utc_time utc_timestamp values
    varbinary varchar varcharacter varying virtual when where while window with write xor year_month zerofill""".split()
)
```

Set the tables:
- `_SIMPLE_IDENT[Dialect.MYSQL] = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")` (unquoted MySQL names keep their case);
- `_RESERVED[Dialect.MYSQL] = MYSQL_RESERVED`.

Then add:

```python
_IDENT_QUOTES: dict[Dialect, tuple[str, str]] = {
    Dialect.POSTGRES: ('"', '"'),
    Dialect.SQLITE: ('"', '"'),
    Dialect.MYSQL: ("`", "`"),  # `"` opens a string in MySQL's default sql_mode
}
_BOOL_LITERALS: dict[Dialect, tuple[str, str]] = {
    Dialect.POSTGRES: ("TRUE", "FALSE"), Dialect.SQLITE: ("1", "0"), Dialect.MYSQL: ("TRUE", "FALSE"),
}
_BYTES_LITERALS: dict[Dialect, str] = {  # {} is the hex
    Dialect.POSTGRES: "'\\x{}'::bytea", Dialect.SQLITE: "X'{}'", Dialect.MYSQL: "X'{}'",
}
NUMERIC_LITERAL = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)  # text that can be written as a bare number


def force_quote_ident(dialect: Dialect, name: str) -> str:
    """Always quoted, the closing quote doubled inside: for names interpolated into introspection SQL."""
    opening, closing = _IDENT_QUOTES[dialect]
    return opening + name.replace(closing, closing * 2) + closing


def quote_ident(dialect: Dialect, name: str) -> str:
    return force_quote_ident(dialect, name) if needs_quote(dialect, name) else name


def quote_literal(dialect: Dialect, value: str) -> str:
    """A string literal that the dialect reads back as exactly `value`: quotes are doubled, and where backslash
    escapes exist (MySQL's default sql_mode), backslashes are doubled too."""
    if RULES[dialect].backslash_escapes:
        value = value.replace("\\", "\\\\")
    return "'" + value.replace("'", "''") + "'"


def bool_literal(dialect: Dialect, value: bool) -> str:
    return _BOOL_LITERALS[dialect][0 if value else 1]


def bytes_literal(dialect: Dialect, hex_digits: str) -> str:
    return _BYTES_LITERALS[dialect].format(hex_digits)
```

- [ ] **Step 4: Rewire the consumers.**
  - **`formatting.py`:**
    - Delete `_BOOL_SQL`, `_BYTES_SQL` and `_NUMERIC`. `_SqlLiterals` uses `bool_literal`, `bytes_literal`, `NUMERIC_LITERAL.fullmatch` and `quote_literal(self._dialect, cell)`.
    - Add `value_condition` after `_SqlLiterals`:

    ```python
    def value_condition(dialect: Dialect, column: str, value: Any, category: ColumnCategory) -> str | None:
        """"Filter by this value" (spec §3.7): a condition that matches one encoded grid cell exactly, in the dialect's
        syntax. None when the cell is only a preview (cut text or bytes): no condition matches it."""
        name = quote_ident(dialect, column)
        if value is None:
            return f"{name} IS NULL"
        try:
            cell = _normalise_encoded(value)
        except _MalformedCell as malformed:
            raise ValidationFailed(f"Not a grid cell: {malformed}") from None
        if isinstance(cell, _Truncated) or (isinstance(cell, _Binary) and cell.truncated):
            return None
        return f"{name} = {_SqlLiterals(dialect)(cell, FormatColumn(name=column, category=category))}"
    ```

    Add `value_condition` to `__all__`.
  - **`select_builder._single_expression`:** after the unterminated check, `if any(t.kind == TokenKind.EXEC_COMMENT for t in tokens): raise ValidationFailed("Executable comments (/*! … */, /*+ … */) are not allowed in a filter")`.
  - **`completion.py`:**
    - `STATEMENT_KEYWORDS[Dialect.MYSQL] = (*_STARTERS, "SHOW", "USE", "REPLACE", "CALL")`;
    - `CLAUSE_KEYWORDS[Dialect.MYSQL] = (*(w for w in _CLAUSES if w != "RETURNING"), "ON DUPLICATE KEY UPDATE")`;
    - `_NO_ITEMS_INSIDE` gains `TokenKind.EXEC_COMMENT`;
    - in `_inside_literal`, `token.text.startswith("--")` becomes `token.text.startswith(("--", "#"))`.
  - **`scope.py`:** `_ALIAS_STOP` gains `USE FORCE IGNORE STRAIGHT_JOIN PARTITION` (MySQL index hints and partition selection after a table name).
  - **`adapters/sql_text.py`:**
    - drop the blanket re-export line (the 02a minor);
    - import `Dialect, force_quote_ident, qualified, quote_ident, quote_literal` explicitly;
    - add `quote_literal_pg(value) = quote_literal(Dialect.POSTGRES, value)` and `quote_ident_mysql(name) = force_quote_ident(Dialect.MYSQL, name)`, both with one-line docstrings;
    - `quote_ident_sqlite` becomes `force_quote_ident(Dialect.SQLITE, name)`.
  - **`adapters/postgres/ddl.py`:** imports and calls `quote_literal_pg`.
  - **`api/sql.py`:**

    ```python
    class ValueConditionRequest(_Input):
        dialect: Dialect
        column: str
        value: Any  # one grid cell as encode_row sent it: null, boolean, number, text, or a bytes/text object
        category: ColumnCategory


    class ValueConditionResponse(BaseModel):
        condition: str | None  # None: the cell is only a preview, so no condition matches it exactly


    @router.post("/value-condition", response_model=ValueConditionResponse)
    def value_condition_for_filter(body: ValueConditionRequest) -> ValueConditionResponse:
        return ValueConditionResponse(condition=value_condition(body.dialect, body.column, body.value, body.category))
    ```

- [ ] **Step 5: Regenerate types and run everything**

Run: `npm run gen:api && cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: all green. The PG and SQLite outputs of the existing formatting and select-builder tests are unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/sql backend/dbaguy_backend/adapters/sql_text.py backend/dbaguy_backend/adapters/postgres/ddl.py \
  backend/dbaguy_backend/api/sql.py backend/tests src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(sql): one source for identifiers and literals per dialect, MySQL keywords, POST /sql/value-condition"
```

---

### Task 4: Classifier holes and the MySQL read-only allow-list

**Files:**
- Modify: `backend/dbaguy_backend/sql/classifier.py`, `backend/dbaguy_backend/sql/sqlglot_rules.py`
- Modify: `backend/dbaguy_backend/services/safety.py`
- Test: `backend/tests/sql/test_classifier.py`, `backend/tests/test_safety.py`

**Interfaces:**
- Consumes: `split`, `tokenize`, `TokenKind.EXEC_COMMENT` (Tasks 1–2).
- Produces:
  - `classify(sql, Dialect.MYSQL)` with the spec §3.4 holes closed;
  - `sqlglot_rules.classify_expression(node: exp.Expression, dialect: Dialect) -> Classification | None` (**new parameter**; the only caller is `classifier._from_sqlglot`);
  - `sqlglot_rules.INTO_CREATES_A_TABLE: frozenset[Dialect] = {POSTGRES, SQLITE}`;
  - `SafetyPolicy` read-only decisions for MySQL SESSION statements via `_HARMLESS_SESSION: dict[Dialect, Callable[[str], bool]]`. `_harmless_pg_setting(sql)` loses its `dialect` parameter.

- [ ] **Step 1: Write the failing tests.**

`tests/sql/test_classifier.py`:

```python
MY = Dialect.MYSQL


@pytest.mark.parametrize(("sql", "kind", "reasons"), [
    ("SELECT a INTO @x FROM t", K.READ, []),
    ("SELECT a, b INTO @x, @y FROM t WHERE id = 1", K.READ, []),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", K.WRITE, []),
    ("SELECT * INTO DUMPFILE '/tmp/x' FROM t", K.WRITE, []),
    ("SELECT * FROM t LOCK IN SHARE MODE", K.WRITE, []),
    ("LOAD DATA INFILE '/x' INTO TABLE t", K.WRITE, []),
    ("LOAD XML INFILE '/x' INTO TABLE t", K.WRITE, []),
    ("HANDLER t OPEN", K.WRITE, []),
    ("DO SLEEP(1)", K.WRITE, []),
    ("REPLACE INTO t VALUES (1)", K.WRITE, []),
    ("CALL p(1)", K.WRITE, []),
    ("/*!40101 SET NAMES utf8 */", K.UNKNOWN, []),
    ("SELECT 1 /*! , (SELECT 1 FROM t FOR UPDATE) */", K.UNKNOWN, []),
    ("/*!50000 DELETE FROM t */", K.UNKNOWN, []),
    ("SELECT /*+ MAX_EXECUTION_TIME(1000) */ * FROM t", K.READ, []),
    ("SELECT 'x\\'; DROP TABLE t; -- '", K.READ, []),
    ("SHOW CREATE TABLE t", K.READ, []),
    ("TABLE t", K.READ, []),
    ("UPDATE t SET a = 1", K.WRITE, ["update_without_where"]),
    ("DELETE t FROM t JOIN u ON t.id = u.id", K.WRITE, ["delete_without_where"]),
    ("ALTER TABLE t DROP COLUMN a", K.DDL, ["alter_drop"]),
    ("CREATE DEFINER=`root`@`%` PROCEDURE p() BEGIN DELETE FROM t; DROP TABLE u; END", K.DDL, []),
    ("EXPLAIN ANALYZE FORMAT=TREE DELETE FROM t", K.WRITE, ["delete_without_where"]),
    ("EXPLAIN FORMAT=JSON SELECT 1", K.READ, []),
    ("SET SESSION TRANSACTION READ WRITE", K.SESSION, []),
    ("SET NAMES utf8mb4", K.SESSION, []),
    ("USE analytics", K.SESSION, []),
    ("START TRANSACTION READ ONLY", K.TRANSACTION, []),
    ("FLUSH TABLES", K.UNKNOWN, []),
    ("SELECT 1; DELETE FROM t", K.WRITE, ["delete_without_where"]),
])
def test_mysql(sql, kind, reasons):
    result = classify(sql, MY)
    assert (result.kind, list(result.dangerous_reasons)) == (kind, reasons)


def test_select_into_still_creates_a_table_in_postgres():
    assert classify("SELECT * INTO n FROM t", PG).kind == K.DDL
```

`tests/test_safety.py`:

```python
MY = Dialect.MYSQL


def decide_my(sql, level="standard", read_only=False):
    decision = policy.check(sql, MY, level, read_only)
    return decision.action, decision.reasons


@pytest.mark.parametrize("sql", [
    "USE analytics", "USE `my db`", "SET NAMES utf8mb4", "SET NAMES 'utf8mb4' COLLATE 'utf8mb4_bin'",
    "SET CHARACTER SET utf8mb4", "SET CHARSET utf8mb4", "SET @x = 1", "SET @x := (SELECT MAX(id) FROM t), @y = 2",
    "SET @'odd name' = 1", "SET sql_mode = ''", "SET SESSION sql_mode = 'ANSI'", "SET LOCAL time_zone = 'UTC'",
    "SET @@sql_mode = 'TRADITIONAL'", "SET @@session.time_zone = '+00:00'", "SET @@local.sql_mode := ''",
    "SELECT a INTO @x FROM t", "START TRANSACTION", "START TRANSACTION READ ONLY",
    "SELECT /*+ MAX_EXECUTION_TIME(1000) */ * FROM t",
])
def test_mysql_read_only_allows(sql):
    assert decide_my(sql, read_only=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "SET SESSION TRANSACTION READ WRITE", "SET TRANSACTION READ WRITE",
    "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET SESSION transaction_read_only = 0",
    "SET transaction_read_only = OFF", "SET @@session.transaction_read_only = 0", "SET GLOBAL sql_mode = ''",
    "SET @@global.time_zone = '+00:00'", "SET PERSIST sql_mode = ''", "SET autocommit = 0",
    "SET @x = 1, autocommit = 0", "SET NAMES", "SET sql_mode = ''; DELETE FROM t", "START TRANSACTION READ WRITE",
    "INSERT INTO t VALUES (1)", "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT * INTO DUMPFILE '/tmp/x' FROM t",
    "/*!40000 DELETE FROM t */", "/*!40101 SET NAMES utf8 */", "LOAD DATA INFILE '/x' INTO TABLE t",
    "HANDLER t OPEN", "DO SLEEP(1)", "CALL p()", "DELIMITER $$",
])
def test_mysql_read_only_blocks(sql):
    assert decide_my(sql, read_only=True) == (A.BLOCK, ["read_only"])


def test_mysql_version_comments_are_unknown_statements():
    assert decide_my("/*!40101 SET NAMES utf8 */", "strict") == (A.CONFIRM, ["strict_unknown"])
    assert decide_my("SELECT /*+ BKA(t) */ * FROM t", "strict") == (A.RUN, [])
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_classifier.py tests/test_safety.py -q`
Expected: FAIL. `SELECT a INTO @x` gives `ddl`, `INTO OUTFILE` gives `read`, `/*!…*/` cases differ, and every MySQL allow-list case is blocked.

- [ ] **Step 3: Implement the classifier.**
  - **`sqlglot_rules.py`:** add `INTO_CREATES_A_TABLE = frozenset({Dialect.POSTGRES, Dialect.SQLITE})` (comment: `# SELECT … INTO <table>; MySQL's INTO only fills variables or writes a file`). `classify_expression(node, dialect)` gates the existing DDL branch: `if node.find(exp.Into) and dialect in INTO_CREATES_A_TABLE`.
  - **`classifier.py`:**
    - `_from_sqlglot` passes `dialect` to `classify_expression`.
    - Add:

    ```python
    # Keywords whose meaning differs by dialect, consulted before _KEYWORD_KIND (spec §3.4).
    _DIALECT_KEYWORD_KIND: dict[Dialect, dict[str, StatementKind]] = {
        Dialect.MYSQL: {"LOAD": StatementKind.WRITE, "HANDLER": StatementKind.WRITE},
    }


    def _has_version_comment(sql: str, dialect: Dialect) -> bool:
        """MySQL runs the text of `/*! … */` as code, so nothing in it can be judged (spec §3.4). `/*+ … */` holds only
        optimizer hints, which never change what a statement does."""
        return any(t.kind == TokenKind.EXEC_COMMENT and t.text.startswith("/*!") for t in tokenize(sql, dialect))
    ```

    - In `_lexical`, take `kind = _DIALECT_KEYWORD_KIND.get(dialect, {}).get(first) or _KEYWORD_KIND.get(first, StatementKind.UNKNOWN)`. For SELECT/WITH, compute `pairs = list(zip(words, words[1:]))`. `locks` also accepts the pair `("LOCK", "IN")`. Add `writes_file = any(a == "INTO" and b in ("OUTFILE", "DUMPFILE") for a, b in pairs)`. Any of `has_dml`, `locks` or `writes_file` gives WRITE.
    - In `_explain_inner`, replace the trailing `while` with:

    ```python
        while i < len(tokens):
            word = tokens[i].upper
            if word == "FORMAT" and i + 1 < len(tokens) and tokens[i + 1].text == "=":
                i += 3  # MySQL: EXPLAIN [ANALYZE] FORMAT = TREE|JSON|TRADITIONAL …
            elif word in (*_ANALYZE, "VERBOSE", "QUERY", "PLAN"):
                analyze = analyze or word in _ANALYZE
                i += 1
            else:
                break
    ```

    - The first line of `_classify_statement`: `if _has_version_comment(sql, dialect): return Classification(kind=StatementKind.UNKNOWN)`.

- [ ] **Step 4: Implement the allow-lists in `services/safety.py`.**
  - `_harmless_pg_setting(sql)` drops its `dialect` parameter and its PG check, and splits and tokenizes with `Dialect.POSTGRES`.
  - In `_allowed_when_read_only`, the SESSION branch becomes `check = _HARMLESS_SESSION.get(dialect); return check is not None and check(sql)`.
  - Add:

```python
_MYSQL_HARMLESS_VARIABLES = frozenset({"sql_mode", "time_zone"})
_MYSQL_SESSION_SCOPES = frozenset({"SESSION", "LOCAL"})


def _top_level_items(tokens: list[Token]) -> list[list[Token]]:
    """`tokens` split at commas outside parentheses."""
    items: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        depth += (token.kind == TokenKind.LPAREN) - (token.kind == TokenKind.RPAREN)
        if token.kind == TokenKind.COMMA and depth == 0:
            items.append([])
        else:
            items[-1].append(token)
    return items


def _harmless_mysql_assignment(tokens: list[Token]) -> bool:
    """One item of a MySQL SET: NAMES …, CHARACTER SET …, CHARSET …, a user variable (`@x = …`), or the session's
    sql_mode or time_zone (`[SESSION|LOCAL] name`, `@@name`, `@@session.name`, `@@local.name`), with `=` or `:=`."""
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in tokens]
    if words[:1] in (["NAMES"], ["CHARSET"]):
        return len(tokens) > 1
    if words[:2] == ["CHARACTER", "SET"]:
        return len(tokens) > 2
    operator = next((k for k, t in enumerate(tokens) if t.kind == TokenKind.OTHER and t.text == "="), None)
    if operator is None or operator + 1 >= len(tokens):
        return False  # not an assignment, or nothing assigned
    target = tokens[:operator - 1] if operator > 0 and tokens[operator - 1].text == ":" else tokens[:operator]
    if target and target[0].upper in _MYSQL_SESSION_SCOPES:
        target = target[1:]
    name = "".join(t.text for t in target).lower()
    if name.startswith("@") and not name.startswith("@@"):
        return True  # a user variable
    for prefix in ("@@session.", "@@local.", "@@"):
        if name.startswith(prefix):
            name = name.removeprefix(prefix)
            break
    return name in _MYSQL_HARMLESS_VARIABLES


def _harmless_mysql_session(sql: str) -> bool:
    """One MySQL statement that only shapes how statements run or look (spec §3.4): `USE db`, or a SET whose every
    item is harmless. Never anything about transactions (`SET … TRANSACTION …`, transaction_read_only), GLOBAL or
    PERSIST scopes, or other system variables."""
    spans = split(sql, Dialect.MYSQL)
    if len(spans) != 1:
        return False
    tokens = [t for t in tokenize(spans[0].text, Dialect.MYSQL) if t.kind not in INSIGNIFICANT]
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in tokens]
    if words[:1] == ["USE"]:
        return len(tokens) == 2 and tokens[1].kind in (TokenKind.WORD, TokenKind.QUOTED)
    if words[:1] != ["SET"] or "TRANSACTION" in words or any(t.kind == TokenKind.EXEC_COMMENT for t in tokens):
        return False
    return all(_harmless_mysql_assignment(item) for item in _top_level_items(tokens[1:]))


_HARMLESS_SESSION: dict[Dialect, Callable[[str], bool]] = {
    Dialect.POSTGRES: _harmless_pg_setting,
    Dialect.MYSQL: _harmless_mysql_session,
}  # a dialect without an entry (SQLite) allows no SESSION statement on a read-only connection
```

- [ ] **Step 5: Run everything**

Run: `cd backend && .venv/bin/pytest -q`
Expected: all green, including the existing PG allow-list tests and `test_the_setting_allow_list_is_postgres_only` (SQLite still blocks).

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/sql/{classifier,sqlglot_rules}.py backend/dbaguy_backend/services/safety.py \
  backend/tests/sql/test_classifier.py backend/tests/test_safety.py
git commit -m "feat(safety): classify MySQL statements (version comments, INTO OUTFILE, LOAD, HANDLER) and a MySQL read-only allow-list"
```

---

### Task 5: Adapter contract: capabilities in `EngineInfo`, `jdbc:` URLs, new node kinds

**Files:**
- Modify: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/registry.py`
- Modify: `backend/dbaguy_backend/adapters/postgres/adapter.py` (capabilities)
- Modify: `backend/dbaguy_backend/domain/profiles.py` (`EngineInfo`), `backend/dbaguy_backend/domain/metadata.py`
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`
- Test: `backend/tests/test_core_utils.py`, `backend/tests/test_schema_snapshot.py`, `backend/tests/test_api.py`

**Interfaces:**
- Produces:
  - `AdapterCapabilities(per_database_connection: bool = False, cancel_aborts_transaction: bool = False, server_read_only: bool = True)`.
  - `EngineInfo` gains the required fields `per_database_connection: bool`, `cancel_aborts_transaction: bool`, `server_read_only: bool`, filled by `DatabaseAdapter.info()` from `capabilities`.
  - PG: `AdapterCapabilities(per_database_connection=True, cancel_aborts_transaction=True)`. SQLite keeps the defaults.
  - `AdapterRegistry.parse_url` strips a leading `jdbc:` (any case, after `strip()`) before choosing the scheme and passes the stripped URL to the adapter.
  - `NodeKind.EVENT = "event"`, `NodeKind.FOREIGN_KEY = "foreign_key"`, `Folder.EVENTS = "events"`, `Folder.FOREIGN_KEYS = "foreign_keys"`.
- No abstract method is added, so `tests/fake_db.py` `FakeConnection` is untouched. `server_read_only` is a 03b placeholder: nothing reads it in 03a (03b fills the connection-dialog note).

- [ ] **Step 1: Write the failing tests.**

`tests/test_core_utils.py`:

```python
def test_registry_strips_a_jdbc_prefix_before_choosing_the_adapter():
    seen = []

    class Recording(FakeAdapter):
        def parse_url(self, url):
            seen.append(url)
            return super().parse_url(url)

    registry = AdapterRegistry([Recording()])
    assert registry.parse_url("  JDBC:fake:///tmp/x ").profile.name == "fake"
    assert seen == ["fake:///tmp/x"]
    with pytest.raises(ValidationFailed):
        registry.parse_url("jdbc:nope://x")
```

`tests/test_schema_snapshot.py`, at the end of `test_relation_paths_and_dialects`:

```python
    pg_info, lite_info = pg.info(), lite.info()
    assert (pg_info.per_database_connection, pg_info.cancel_aborts_transaction, pg_info.server_read_only) == (True, True, True)
    assert (lite_info.per_database_connection, lite_info.cancel_aborts_transaction, lite_info.server_read_only) == (
        False, False, True,
    )
```

`tests/test_api.py::test_engines` (Task 9 adds `mysql` to the set):

```python
def test_engines(client):
    engines = {e["engine"]: e for e in client.get("/system/engines", headers=AUTH).json()}
    assert set(engines) == {"postgres", "sqlite"}
    assert engines["postgres"]["per_database_connection"] and engines["postgres"]["cancel_aborts_transaction"]
    assert not engines["sqlite"]["per_database_connection"] and not engines["sqlite"]["cancel_aborts_transaction"]
    assert engines["postgres"]["server_read_only"] and engines["sqlite"]["server_read_only"]
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_core_utils.py tests/test_schema_snapshot.py tests/test_api.py -q`
Expected: FAIL (`AttributeError: cancel_aborts_transaction`, `KeyError`, unsupported scheme `jdbc`).

- [ ] **Step 3: Implement** as listed under Interfaces. Field comments:
  - `cancel_aborts_transaction`: `# PostgreSQL: cancelling a statement aborts the transaction around it (the export-cancel warning)`;
  - `server_read_only`: `# the database can enforce a read-only session; SQL Server cannot (03b)`.

  `relation_path` is unchanged. Then run `npm run gen:api`.

- [ ] **Step 4: Run everything**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green (the new `EngineInfo` fields are additive in TS).

- [ ] **Step 5: Commit**

```bash
git add backend/dbaguy_backend/adapters/{base,registry}.py backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/domain/{profiles,metadata}.py backend/tests src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(adapters): engine capabilities in EngineInfo, jdbc: URL prefix, event and foreign_key node kinds"
```

---

### Task 6: Dev MySQL, seed data, driver dependency and test support

**Files:**
- Modify: `dev/docker-compose.yml`
- Create: `dev/mysql/init/00-schema.sql`, `dev/mysql/init/01-routines.sql`, `dev/mysql/init/02-data.sql`
- Modify: `backend/pyproject.toml`, `backend/poetry.lock`
- Modify: `backend/tests/support.py`, `backend/tests/test_dev_env.py`

**Interfaces:**
- Produces:
  - The compose service `mysql`, seeded exactly as below.
  - In `tests/support.py`:
    - `MYSQL_HOST = "127.0.0.1"`, `MYSQL_PORT = int(os.environ.get("DBAGUY_TEST_MYSQL_PORT", "33067"))`, `MYSQL_USER = "dbaguy"`, `MYSQL_PASSWORD = "dbaguy"`;
    - `mysql_driver_kwargs(**overrides) -> dict` (host, port, user, password, `database="dbaguy"`, `use_pure=True`, `connection_timeout=3`);
    - `MYSQL_AVAILABLE`, `requires_mysql`, `requires_mysql_bastion` (MySQL and bastion);
    - `make_mysql_profile(**overrides) -> ServerProfile` (engine `mysql`, host/port/user above, database `dbaguy`, `SslConfig(mode="disable")`).
  - Seed objects that later tests rely on:
    - tables `customers`, `orders` (index `orders_customer_idx`, FK `orders_customer_fk`, trigger `orders_note`), `types_demo`, `big_table` (100,000 rows), `Order Items`, `` tick`table ``, `job_log`;
    - view `order_summary`; procedures `two_results(n)`, `fail_after_select()`; function `add_numbers`; event `purge_job_log`;
    - database `analytics` with `page_views` (50 rows).

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_dev_env.py`:

```python
import mysql.connector

from tests.support import mysql_driver_kwargs, requires_mysql


@requires_mysql
def test_dev_mysql_is_seeded():
    conn = mysql.connector.connect(**mysql_driver_kwargs())
    try:
        cursor = conn.cursor()

        def one(sql):
            cursor.execute(sql)
            return cursor.fetchone()[0]

        assert one("SELECT COUNT(*) FROM big_table") == 100_000
        assert one("SELECT COUNT(*) FROM analytics.page_views") == 50
        assert one("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'dbaguy'") == 3
        assert one("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = 'dbaguy'") == 1
        assert one("SELECT CAST(born AS CHAR) FROM types_demo WHERE id = 1") == "0000-00-00"
        assert one("SELECT label FROM `tick``table`") == "one"
    finally:
        conn.close()
```

- [ ] **Step 2: Run it and see it fail**

Run: `cd backend && .venv/bin/pytest tests/test_dev_env.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'mysql'`.

- [ ] **Step 3: Add the dependency**

Run: `cd backend && poetry add "mysql-connector-python>=26.7.0,<27"`
Expected: `pyproject.toml` `[project] dependencies` gains the line and `poetry.lock` updates.

- [ ] **Step 4: Add the compose service.** Add the `mysql` service to `dev/docker-compose.yml` and make the bastion `depends_on` both databases. The healthcheck uses TCP (`-h 127.0.0.1`): the image's temporary init server has no networking, so the container reports healthy only after the seed has run.

```yaml
  mysql:
    image: mysql:8.4
    environment:
      MYSQL_ROOT_PASSWORD: dbaguy
      MYSQL_USER: dbaguy
      MYSQL_PASSWORD: dbaguy
      MYSQL_DATABASE: dbaguy
    ports:
      - "127.0.0.1:33067:3306"
    volumes:
      - ./mysql/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uroot -pdbaguy --silent"]
      interval: 2s
      timeout: 3s
      retries: 90
      start_period: 20s
```

```yaml
  bastion:
    build: ./bastion
    ports:
      - "127.0.0.1:2222:22"
    depends_on:
      - postgres
      - mysql
```

- [ ] **Step 5: Write the seed files exactly as below.**

`dev/mysql/init/00-schema.sql`:

```sql
-- DbaGuy dev MySQL (mysql:8.4). Runs once, as root, when the container is created. Fake credentials only.
-- Objects are created with the dev account as DEFINER so that the account DbaGuy connects with can read their DDL.
CREATE DATABASE analytics;
GRANT ALL ON analytics.* TO 'dbaguy'@'%';

USE dbaguy;

CREATE TABLE customers (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(200) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    profile JSON,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT = 'Customer master data';

CREATE TABLE orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id BIGINT UNSIGNED NOT NULL,
    total DECIMAL(30, 10) NOT NULL,
    placed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note TEXT,
    KEY orders_customer_idx (customer_id),
    CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

CREATE TABLE types_demo (
    id INT PRIMARY KEY,
    huge BIGINT UNSIGNED,
    big_signed BIGINT,
    amount DECIMAL(30, 10),
    ratio DOUBLE,
    flag TINYINT(1),
    bits BIT(8),
    born DATE,
    seen DATETIME,
    stamped TIMESTAMP NULL,
    lasted TIME,
    yr YEAR,
    doc JSON,
    data BLOB,
    code BINARY(4),
    mood ENUM('happy', 'sad'),
    tags SET('a', 'b'),
    note VARCHAR(100),
    body TEXT
);

CREATE TABLE big_table (id INT PRIMARY KEY, label VARCHAR(50) NOT NULL, value DOUBLE);

-- Identifiers that need quoting: a space, reserved words, a backtick inside names (Review Focus 3).
CREATE TABLE `Order Items` (id INT AUTO_INCREMENT PRIMARY KEY, `select` VARCHAR(50), `order` INT);
CREATE TABLE `tick``table` (`a``b` INT PRIMARY KEY, label VARCHAR(20));

CREATE TABLE job_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    message VARCHAR(200),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE DEFINER = `dbaguy`@`%` SQL SECURITY INVOKER VIEW order_summary AS
SELECT c.name, COUNT(o.id) AS orders, SUM(o.total) AS total
FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.name;

USE analytics;
CREATE TABLE page_views (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    path VARCHAR(200) NOT NULL,
    viewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`dev/mysql/init/01-routines.sql`:

```sql
-- Stored programs, written the way the mysql client needs them (and the way DbaGuy splits them): DELIMITER lines.
USE dbaguy;

DELIMITER $$

CREATE DEFINER = `dbaguy`@`%` PROCEDURE two_results(IN n INT)
BEGIN
    SELECT id, label FROM big_table WHERE id <= n ORDER BY id;
    SELECT COUNT(*) AS total FROM big_table;
END$$

CREATE DEFINER = `dbaguy`@`%` PROCEDURE fail_after_select()
BEGIN
    SELECT 1 AS first;
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'boom after the first result';
END$$

CREATE DEFINER = `dbaguy`@`%` FUNCTION add_numbers(a INT, b INT) RETURNS INT DETERMINISTIC
BEGIN
    RETURN a + b;
END$$

CREATE DEFINER = `dbaguy`@`%` TRIGGER orders_note BEFORE INSERT ON orders FOR EACH ROW
BEGIN
    IF NEW.note IS NULL THEN
        SET NEW.note = '';
    END IF;
END$$

CREATE DEFINER = `dbaguy`@`%` EVENT purge_job_log
ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00'
DO BEGIN
    DELETE FROM job_log WHERE created_at < NOW() - INTERVAL 30 DAY;
END$$

DELIMITER ;
```

`dev/mysql/init/02-data.sql`:

```sql
USE dbaguy;

SET SESSION cte_max_recursion_depth = 100000;
INSERT INTO big_table (id, label, value)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 100000)
SELECT n, CONCAT('row ', n), n * 1.5 FROM seq;

INSERT INTO customers (email, name, profile) VALUES
    ('ada@example.com', 'Ada Lovelace', '{"vip": true, "score": 12345678901234567890}'),
    ('lin@example.com', '林小明', '{"note": "中文資料"}'),
    ('empty@example.com', '', NULL);

INSERT INTO orders (customer_id, total, note) VALUES
    (1, 12345678901234567890.1234567890, NULL),
    (1, 10.5, 'gift'),
    (2, 99.99, '');

-- A zero date: strict mode (the default) rejects it, but real tables still hold them.
SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION';
INSERT INTO types_demo VALUES
    (1, 18446744073709551615, -9007199254740993, 12345678901234567890.1234567890, 1.5, 1, b'10100101',
     '0000-00-00', '2026-10-17 12:34:56', '2026-10-17 12:34:56', '-12:30:00', 2026,
     '{"a": [1, 2], "b": null}', x'DEADBEEF', x'01020304', 'happy', 'a,b', 'it''s \\ back', '中文 😀'),
    (2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
SET SESSION sql_mode = DEFAULT;

INSERT INTO `Order Items` (`select`, `order`) VALUES ('first', 1);
INSERT INTO `tick``table` (`a``b`, label) VALUES (1, 'one');
INSERT INTO job_log (message) VALUES ('seeded');

INSERT INTO analytics.page_views (path)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 50)
SELECT CONCAT('/page/', n) FROM seq;
```

- [ ] **Step 6: Add the support helpers.** In `tests/support.py`, add the names under Interfaces. `_mysql_ready()` mirrors `_pg_ready()`: port open, then a real `mysql.connector.connect(**mysql_driver_kwargs())` closed again; `mysql.connector.Error` → False. Evaluate it once at import into `MYSQL_AVAILABLE`.

- [ ] **Step 7: Start the containers and run the tests**

Run: `docker compose -f dev/docker-compose.yml up -d --build --wait && cd backend && .venv/bin/pytest -q`
Expected: all green, `test_dev_mysql_is_seeded` passes (not skipped). If `--wait` times out on a slow first pull, run it again.

- [ ] **Step 8: Commit**

```bash
git add dev/docker-compose.yml dev/mysql backend/pyproject.toml backend/poetry.lock backend/tests/support.py backend/tests/test_dev_env.py
git commit -m "chore(dev): MySQL 8.4 dev database with seed, mysql-connector-python dependency and test support"
```

---

### Task 7: MySQL connection settings: URL import, TLS, options (pure)

**Files:**
- Create: `backend/dbaguy_backend/adapters/mysql/__init__.py` (empty), `backend/dbaguy_backend/adapters/mysql/settings.py`
- Test: `backend/tests/test_mysql_settings.py`

**Interfaces:**
- Consumes: `Endpoint`, `ParsedUrl` (`adapters/base.py`); `ServerProfile`, `ServerProfileDraft`, `SslConfig`, `SslMode`; `ConnectionFailed`, `ValidationFailed`.
- Produces (`adapters/mysql/settings.py`):
  - `URL_SCHEMES = ("mysql", "mysql+mysqlconnector", "mysql+pymysql")`; `RESERVED_OPTIONS: frozenset[str]`.
  - `parse_mysql_url(url: str) -> ParsedUrl` (receives the URL with any `jdbc:` already stripped by the registry).
  - `connect_params(profile: ServerProfile, endpoint: Endpoint, password: str | None, database: str | None) -> dict[str, Any]`. Raises `ConnectionFailed` for TLS settings that cannot work.
  - `kill_params(params: Mapping[str, Any]) -> dict[str, Any]`.
  - `requires_tls(ssl: SslConfig) -> bool`.

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_mysql_settings.py`:

```python
from uuid import uuid4

import pytest
from mysql.connector.constants import ClientFlag

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mysql.settings import connect_params, kill_params, parse_mysql_url, requires_tls
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.profiles import ServerProfile, SslConfig

DIRECT = Endpoint("db.example.com", 3306)
TUNNEL = Endpoint("127.0.0.1", 52001, tls_server_name="db.example.com")


def profile(**overrides) -> ServerProfile:
    data = {"id": uuid4(), "name": "m", "engine": "mysql", "host": "db.example.com", "database": "app", "username": "me"}
    return ServerProfile(**{**data, **overrides})


def test_parse_url():
    parsed = parse_mysql_url("mysql://me:p%40ss@db.example.com:3307/app?ssl-mode=REQUIRED&connection_timeout=5&serverTimezone=UTC")
    draft = parsed.profile
    assert (draft.engine, draft.host, draft.port, draft.database, draft.username) == ("mysql", "db.example.com", 3307, "app", "me")
    assert parsed.password == "p@ss" and draft.ssl.mode == "require"
    assert draft.options == {"connection_timeout": "5"}  # serverTimezone is JDBC-only: dropped
    jdbc = parse_mysql_url("mysql://db/app?user=u&password=x&useSSL=false")
    assert (jdbc.profile.username, jdbc.password, jdbc.profile.ssl.mode, jdbc.profile.options) == ("u", "x", "disable", {})
    alt = parse_mysql_url("mysql+pymysql://u@h/db?sslmode=verify-full&ssl-ca=/tmp/ca.pem")
    assert (alt.profile.ssl.mode, alt.profile.ssl.ca_file) == ("verify-full", "/tmp/ca.pem")
    assert parse_mysql_url("mysql://h/db?ssl_mode=VERIFY_IDENTITY").profile.ssl.mode == "verify-full"


@pytest.mark.parametrize("url, message", [
    ("mysql:///app", "needs a host"),
    ("mysql://h:abc/app", "port"),
    ("mysql://h/app?ssl-mode=bogus", "ssl-mode"),
])
def test_parse_url_errors(url, message):
    with pytest.raises(ValidationFailed, match=message):
        parse_mysql_url(url)


def test_connect_params_keep_the_target_and_the_safety_settings():
    options = {"host": "evil", "init_command": "SET SESSION TRANSACTION READ WRITE", "allow_local_infile": "true",
               "connection_timeout": "5", "auth_plugin": "mysql_native_password", "compress": "true"}
    params = connect_params(profile(options=options), DIRECT, "pw", None)
    assert (params["host"], params["port"], params["user"], params["password"], params["database"]) == (
        "db.example.com", 3306, "me", "pw", "app")
    assert "init_command" not in params and params["allow_local_infile"] is False
    assert params["connection_timeout"] == 5 and params["auth_plugin"] == "mysql_native_password" and params["compress"] is True
    assert params["use_pure"] is True and params["autocommit"] is True
    assert params["client_flags"] == [-ClientFlag.MULTI_STATEMENTS]
    assert connect_params(profile(), DIRECT, None, "other")["database"] == "other"
    assert "database" not in connect_params(profile(database=None), DIRECT, None, None)


@pytest.mark.parametrize("mode, expected", [
    ("disable", {"ssl_disabled": True}),
    ("prefer", {}),
    ("require", {}),
    ("verify-ca", {"ssl_verify_cert": True, "ssl_ca": "/tmp/ca.pem"}),
    ("verify-full", {"ssl_verify_cert": True, "ssl_verify_identity": True, "ssl_ca": "/tmp/ca.pem"}),
])
def test_tls_modes(mode, expected):
    params = connect_params(profile(ssl=SslConfig(mode=mode, ca_file="/tmp/ca.pem")), DIRECT, None, None)
    assert {k: v for k, v in params.items() if k.startswith("ssl_")} == expected
    assert requires_tls(SslConfig(mode=mode)) == (mode in ("require", "verify-ca", "verify-full"))


def test_tls_settings_that_cannot_work_fail_before_connecting():
    with pytest.raises(ConnectionFailed, match="SSH tunnel"):
        connect_params(profile(ssl=SslConfig(mode="verify-full")), TUNNEL, None, None)
    assert connect_params(profile(ssl=SslConfig(mode="verify-ca")), TUNNEL, None, None)["ssl_verify_cert"] is True
    with pytest.raises(ConnectionFailed, match="certificate and its key"):
        connect_params(profile(ssl=SslConfig(mode="require", cert_file="/tmp/c.pem")), DIRECT, None, None)


def test_kill_params():
    params = connect_params(profile(), DIRECT, "pw", None)
    kill = kill_params(params)
    assert kill["connection_timeout"] == 3 and "database" not in kill and kill["password"] == "pw"
    assert "conn_attrs" not in kill and kill["client_flags"] == params["client_flags"]
    assert params["connection_timeout"] == 10  # the original is untouched
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_mysql_settings.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.mysql`.

- [ ] **Step 3: Implement `adapters/mysql/settings.py`.** Module docstring: `How a MySQL profile becomes mysql-connector arguments, and a MySQL URL a profile draft. Pure: no I/O.`

```python
URL_SCHEMES = ("mysql", "mysql+mysqlconnector", "mysql+pymysql")
CONNECT_TIMEOUT_S = 10
KILL_CONNECT_TIMEOUT_S = 3  # the worker holds its state lock while a cancel connects
_DEFAULTS: dict[str, Any] = {"connection_timeout": CONNECT_TIMEOUT_S, "charset": "utf8mb4"}
# Arguments that pick the target, carry secrets or decide DbaGuy's guarantees (one statement per call, no LOCAL INFILE,
# read-only not undone by init_command, pure driver): never taken from free-form options or a URL.
RESERVED_OPTIONS = frozenset({
    "user", "password", "passwd", "password1", "password2", "password3", "host", "port", "database", "db",
    "unix_socket", "client_flags", "use_pure", "allow_local_infile", "allow_local_infile_in_path", "autocommit",
    "consume_results", "raw", "buffered", "converter_class", "dsn", "option_files", "option_groups", "init_command",
    "conn_attrs", "pool_name", "pool_size", "pool_reset_session", "ssl_disabled", "ssl_ca", "ssl_cert", "ssl_key",
    "ssl_verify_cert", "ssl_verify_identity",
})
_SSL_MODES: dict[str, SslMode] = {  # MySQL ssl-mode, Connector/J sslMode and libpq sslmode spellings
    "disabled": "disable", "disable": "disable", "preferred": "prefer", "prefer": "prefer",
    "required": "require", "require": "require", "verify-ca": "verify-ca", "verify-identity": "verify-full",
    "verify-full": "verify-full",
}
_SSL_QUERY_KEYS = {"sslmode": "mode", "sslca": "ca_file", "sslcert": "cert_file", "sslkey": "key_file"}


def _fixed() -> dict[str, Any]:
    """Fresh each time: the driver adds its own entries to `conn_attrs`."""
    return {
        "use_pure": True,
        "autocommit": True,
        "client_flags": [-ClientFlag.MULTI_STATEMENTS],  # the server then rejects a second statement (1064)
        "allow_local_infile": False,
        "consume_results": False,
        "conn_attrs": {"program_name": "DbaGuy"},
    }
```

The functions must follow these rules:
- **`_option_value(value: str) -> str | int | bool`:** `"true"`/`"false"` (any case) → bool; `-?\d+` → int; anything else stays a str.
- **`_driver_options(options)`:** drop every `RESERVED_OPTIONS` key (case-insensitive) and log `logger.warning("Ignoring reserved MySQL connection options: {}", names)`, names only. Coerce the values. Unknown keys pass through: the driver rejects them at connect, and Task 9 turns that into `ConnectionFailed`.
- **`_tls_params(ssl, endpoint)`:**
  - `disable` → `{"ssl_disabled": True}`.
  - `verify-full` with `endpoint.tls_server_name` → `ConnectionFailed("verify-full checks the server's host name, which mysql-connector cannot do through an SSH tunnel (it would check 127.0.0.1); use verify-ca, or connect without the tunnel")`.
  - Exactly one of `cert_file`/`key_file` → `ConnectionFailed("Give both a client certificate and its key, or neither")`.
  - `verify-ca`/`verify-full` add `ssl_verify_cert=True`, plus `ssl_ca=os.path.expanduser(ca_file)` when one is set (without one the driver uses the system trust store). `verify-full` adds `ssl_verify_identity=True`.
  - `cert_file`/`key_file` → `ssl_cert`/`ssl_key` (expanded) in any non-disabled mode.
  - `prefer` and `require` add nothing else (the driver tries TLS without verifying; Task 9 checks `require` after connecting).
- **`connect_params`:** `{**_DEFAULTS, **_driver_options(profile.options), **_tls_params(...), **_fixed(), "host": endpoint.host, "port": endpoint.port, "user": profile.username or "", "password": password or ""}`, plus `"database"` = `database or profile.database` when that is not empty.
- **`kill_params`:** a copy without `"database"` and `"conn_attrs"`, with `connection_timeout=KILL_CONNECT_TIMEOUT_S` and `client_flags=list(params["client_flags"])`. The caller passes a fresh `conn_attrs` for every connection, because the driver adds its own entries to the dict it is given.
- **`parse_mysql_url`:**
  - `urlparse`; reading `.port` raises ValueError → `ValidationFailed("Invalid port in the MySQL URL")`.
  - Query keys are matched after `key.lower().replace("-", "").replace("_", "")`: `_SSL_QUERY_KEYS` fill `SslConfig` fields. The mode goes through `_SSL_MODES[value.strip().lower().replace("_", "-")]`; an unknown value raises `ValidationFailed(f"Unknown ssl-mode '{value}' in the MySQL URL")`. `usessl=false`/`true` → `disable`/`require` when no mode was given.
  - Query `user`/`password` fill what the authority leaves empty and are never kept as options. Username and password are `unquote`d.
  - The host is required: `ValidationFailed("A MySQL URL needs a host, e.g. mysql://user@host:3306/db")`. The database is the unquoted path or None. The name is `f"{host}/{database}"` or `host`.
  - The remaining query keys are kept as options only when they are in `mysql.connector.constants.DEFAULT_CONFIGURATION` and not reserved. Log the dropped names at WARNING.

- [ ] **Step 4: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_mysql_settings.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/dbaguy_backend/adapters/mysql backend/tests/test_mysql_settings.py
git commit -m "feat(mysql): connection settings — URL import (mysql://, jdbc:, ssl-mode), TLS mapping, guarded options"
```

---

### Task 8: MySQL object tree, DDL and schema snapshot

**Files:**
- Create: `backend/dbaguy_backend/adapters/mysql/catalog.py`, `backend/dbaguy_backend/adapters/mysql/introspection.py`, `backend/dbaguy_backend/adapters/mysql/schema.py`
- Test: `backend/tests/test_mysql_introspection.py`

**Interfaces:**
- Consumes: `quote_ident_mysql` (Task 3); `NodeKind.EVENT`, `NodeKind.FOREIGN_KEY`, `Folder.EVENTS`, `Folder.FOREIGN_KEYS` (Task 5); `mysql_driver_kwargs`, `requires_mysql` (Task 6).
- Produces (all take a mysql-connector `MySQLConnection`):
  - `catalog.SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})`;
  - `catalog.MISSING_OBJECT_ERRNOS = frozenset({1049, 1146, 1305, 1360, 1539})` (unknown database, table, routine, trigger, event);
  - `catalog.query(conn, sql: str, params: Sequence[Any] = ()) -> list[tuple]` and `catalog.first_row(conn, sql, params=()) -> tuple | None`. Rules:
    - build the cursor as `MySQLCursorBuffered(conn)`, not with `conn.cursor()` (which sends a PING first);
    - call `cursor.execute(sql, tuple(params))`, giving Python values;
    - decode `bytes`/`bytearray` cells as UTF-8 (errors replaced);
    - always close the cursor;
    - internal SQL only, and it never contains the word "delimiter" (see Task 9 for why).
  - `introspection.list_children(conn, path: list[PathSegment]) -> list[ObjectNode]`;
  - `introspection.object_detail(conn, path) -> ObjectDetail`;
  - `schema.RELATION_LIMIT = 10_000`; `schema.build_snapshot(conn) -> SchemaSnapshot`.

Tree shape (spec §3.5; MySQL databases are `schema` nodes):
- `[]` → every schema in `information_schema.SCHEMATA`, `has_children=True`, `system` for `SYSTEM_SCHEMAS`, user schemas first, each group by name.
- `[schema]` → the non-empty folders, in the order `tables, views, functions, procedures, triggers, events`, found with one `UNION ALL` count query.
- `[schema, folder]` → objects:

  | Folder | Kind | Children | Detail |
  |---|---|---|---|
  | tables | `table` | yes | — |
  | views | `view` | yes | — |
  | functions | `function` | no | — |
  | procedures | `procedure` | no | — |
  | triggers | `trigger` | no | `on <table>` |
  | events | `event` | no | `enabled` / `disabled` |

- `[schema, folder, table|view]` → `columns` always. A table also gets `indexes`, `foreign_keys` and `triggers` when non-empty (one count query).
- `[…, folder]` leaves:

  | Folder | Kind | Detail |
  |---|---|---|
  | columns | `column` | `COLUMN_TYPE [NOT NULL] [PK]` |
  | indexes | `index` | `primary` / `unique` / none; PRIMARY first |
  | foreign_keys | `foreign_key` | `(a, b) → db.t (x, y)` |
  | triggers | `trigger` | `before insert` |

- The first segment must be kind `schema`, and the schema must exist (`NotFound(f"Schema '{name}' not found")`). An unknown folder name raises `ValidationFailed`.
- Every name interpolated into SQL goes through `quote_ident_mysql`. Every value goes through `%s` parameters.

`object_detail`:
- Server DDL via `SHOW CREATE <WORD> {q(schema)}.{q(name)}` with `ddl_is_generated=False`, then `ddl + ";"`; a NULL DDL (no privilege) gives `ddl=None`. The kinds:

  | Kind | Statement | Row index |
  |---|---|---|
  | table | `TABLE` | 1 |
  | view | `VIEW` | 1 |
  | procedure | `PROCEDURE` | 2 |
  | function | `FUNCTION` | 2 |
  | trigger | `TRIGGER` | 2 |
  | event | `EVENT` | 3 |

- Tables and views also carry `columns` (`ColumnInfo` from `information_schema.COLUMNS`). Tables carry `comment` = `TABLE_COMMENT` or None.
- `index` and `foreign_key` leaves get generated DDL with `ddl_is_generated=True` (their table is `path[2].name`). `column` returns the matching `ColumnInfo` like SQLite.
- Any other kind raises `ValidationFailed(f"No details available for {kind}")`. A `mysql.connector.Error` whose errno is in `MISSING_OBJECT_ERRNOS` becomes `NotFound(f"{kind} '{name}' not found")`. Other driver errors propagate (`MetadataService` retries once, then reports `QueryFailed`).

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_mysql_introspection.py`:

```python
import mysql.connector
import pytest

from dbaguy_backend.adapters.mysql import introspection, schema as mysql_schema
from dbaguy_backend.domain.errors import NotFound
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import mysql_driver_kwargs, requires_mysql

pytestmark = requires_mysql


@pytest.fixture
def conn():
    connection = mysql.connector.connect(**mysql_driver_kwargs())
    yield connection
    connection.close()


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def names(nodes):
    return [n.name for n in nodes]


DB = [seg("schema", "dbaguy")]
ORDERS = [*DB, seg("folder", "tables"), seg("table", "orders")]


def test_tree(conn):
    root = introspection.list_children(conn, [])
    assert {"dbaguy", "analytics"} <= set(names(root))
    system = {n.name for n in root if n.system}
    assert {"information_schema", "mysql", "performance_schema", "sys"} <= system
    assert all(not n.system for n in root[:2]) and all(n.kind == "schema" for n in root)  # user schemas first
    assert names(introspection.list_children(conn, DB)) == ["tables", "views", "functions", "procedures", "triggers", "events"]
    assert names(introspection.list_children(conn, [seg("schema", "analytics")])) == ["tables"]
    tables = set(names(introspection.list_children(conn, [*DB, seg("folder", "tables")])))
    assert {"big_table", "customers", "orders", "Order Items", "tick`table", "types_demo"} <= tables
    assert names(introspection.list_children(conn, [*DB, seg("folder", "views")])) == ["order_summary"]
    assert set(names(introspection.list_children(conn, [*DB, seg("folder", "procedures")]))) == {"two_results", "fail_after_select"}
    [trigger] = introspection.list_children(conn, [*DB, seg("folder", "triggers")])
    assert (trigger.kind, trigger.name, trigger.detail) == ("trigger", "orders_note", "on orders")
    [event] = introspection.list_children(conn, [*DB, seg("folder", "events")])
    assert (event.kind, event.name, event.detail) == ("event", "purge_job_log", "enabled")
    assert names(introspection.list_children(conn, ORDERS)) == ["columns", "indexes", "foreign_keys", "triggers"]
    indexes = introspection.list_children(conn, [*ORDERS, seg("folder", "indexes")])
    assert [(n.name, n.detail) for n in indexes] == [("PRIMARY", "primary"), ("orders_customer_idx", None)]
    [fk] = introspection.list_children(conn, [*ORDERS, seg("folder", "foreign_keys")])
    assert (fk.kind, fk.name, fk.detail) == ("foreign_key", "orders_customer_fk", "(customer_id) → dbaguy.customers (id)")
    columns = introspection.list_children(conn, [*ORDERS, seg("folder", "columns")])
    assert columns[0].name == "id" and columns[0].detail == "bigint NOT NULL PK"


def test_object_detail(conn):
    table = introspection.object_detail(conn, ORDERS)
    assert table.ddl.startswith("CREATE TABLE `orders`") and table.ddl.endswith(";") and not table.ddl_is_generated
    assert [c.name for c in table.columns][:2] == ["id", "customer_id"] and table.columns[0].is_primary_key
    customers = introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "customers")])
    assert customers.comment == "Customer master data"
    proc = introspection.object_detail(conn, [*DB, seg("folder", "procedures"), seg("procedure", "two_results")])
    assert "SELECT COUNT(*) AS total FROM big_table" in proc.ddl
    event = introspection.object_detail(conn, [*DB, seg("folder", "events"), seg("event", "purge_job_log")])
    assert "EVENT `purge_job_log`" in event.ddl
    trigger = introspection.object_detail(conn, [*ORDERS, seg("folder", "triggers"), seg("trigger", "orders_note")])
    assert "BEFORE INSERT ON `orders`" in trigger.ddl
    index = introspection.object_detail(conn, [*ORDERS, seg("folder", "indexes"), seg("index", "orders_customer_idx")])
    assert index.ddl == "CREATE INDEX `orders_customer_idx` ON `dbaguy`.`orders` (`customer_id`);" and index.ddl_is_generated
    primary = introspection.object_detail(conn, [*ORDERS, seg("folder", "indexes"), seg("index", "PRIMARY")])
    assert primary.ddl == "ALTER TABLE `dbaguy`.`orders` ADD PRIMARY KEY (`id`);"
    fk = introspection.object_detail(conn, [*ORDERS, seg("folder", "foreign_keys"), seg("foreign_key", "orders_customer_fk")])
    assert fk.ddl.startswith(
        "ALTER TABLE `dbaguy`.`orders` ADD CONSTRAINT `orders_customer_fk` FOREIGN KEY (`customer_id`) "
        "REFERENCES `dbaguy`.`customers` (`id`)"
    ) and "ON DELETE CASCADE" in fk.ddl


def test_names_with_backticks_round_trip(conn):  # Review Focus 3
    path = [*DB, seg("folder", "tables"), seg("table", "tick`table")]
    detail = introspection.object_detail(conn, path)
    assert detail.ddl.startswith("CREATE TABLE `tick``table`") and [c.name for c in detail.columns] == ["a`b", "label"]
    assert names(introspection.list_children(conn, [*path, seg("folder", "columns")])) == ["a`b", "label"]
    spaced = introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "Order Items")])
    assert [c.name for c in spaced.columns] == ["id", "select", "order"]


def test_missing_objects_are_not_found(conn):
    with pytest.raises(NotFound):
        introspection.object_detail(conn, [*DB, seg("folder", "tables"), seg("table", "nope")])
    with pytest.raises(NotFound):
        introspection.list_children(conn, [seg("schema", "nope")])


def rel(snapshot, schema_name, name):
    schema = next(s for s in snapshot.schemas if s.name == schema_name)
    return next(r for r in schema.relations if r.name == name)


def test_snapshot(conn):
    snap = mysql_schema.build_snapshot(conn)
    assert (snap.database, snap.default_schema, snap.search_path) == (None, "dbaguy", ["dbaguy"])
    big = rel(snap, "dbaguy", "big_table")
    assert [(c.name, c.type_name) for c in big.columns] == [("id", "int"), ("label", "varchar(50)"), ("value", "double")]
    orders = rel(snap, "dbaguy", "orders")
    assert orders.primary_key == ["id"]
    assert [(f.name, f.columns, f.ref_schema, f.ref_table, f.ref_columns) for f in orders.foreign_keys] == [
        ("orders_customer_fk", ["customer_id"], "dbaguy", "customers", ["id"])
    ]
    assert rel(snap, "dbaguy", "order_summary").kind == "view"
    assert rel(snap, "analytics", "page_views").kind == "table"
    dbaguy = next(s for s in snap.schemas if s.name == "dbaguy")
    assert dbaguy.functions == ["add_numbers"] and not dbaguy.system
    mysql_db = next(s for s in snap.schemas if s.name == "mysql")
    assert mysql_db.system and mysql_db.relations == []


def test_snapshot_over_the_limit_loads_only_the_current_database(conn, monkeypatch):
    monkeypatch.setattr(mysql_schema, "RELATION_LIMIT", 5)
    snap = mysql_schema.build_snapshot(conn)
    assert rel(snap, "dbaguy", "big_table")
    assert next(s for s in snap.schemas if s.name == "analytics").relations == []
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_mysql_introspection.py -q`
Expected: `ImportError` for `introspection` and `schema`.

- [ ] **Step 3: Implement `catalog.py` and `introspection.py`** following the rules above. These queries go in as written:

```python
_FOLDER_COUNTS = """
SELECT 'tables', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE <> 'VIEW'
UNION ALL SELECT 'views', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'VIEW'
UNION ALL SELECT 'functions', COUNT(*) FROM information_schema.ROUTINES
    WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'FUNCTION'
UNION ALL SELECT 'procedures', COUNT(*) FROM information_schema.ROUTINES
    WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'PROCEDURE'
UNION ALL SELECT 'triggers', COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s
UNION ALL SELECT 'events', COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s
"""
_TABLE_FOLDER_COUNTS = """
SELECT 'indexes', COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
UNION ALL SELECT 'foreign_keys', COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = %s AND TABLE_NAME = %s
UNION ALL SELECT 'triggers', COUNT(*) FROM information_schema.TRIGGERS
    WHERE EVENT_OBJECT_SCHEMA = %s AND EVENT_OBJECT_TABLE = %s
"""
_OBJECTS: dict[Folder, str] = {  # (name, detail)
    Folder.TABLES: "SELECT TABLE_NAME, NULL FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE <> 'VIEW' ORDER BY TABLE_NAME",
    Folder.VIEWS: "SELECT TABLE_NAME, NULL FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'VIEW' ORDER BY TABLE_NAME",
    Folder.FUNCTIONS: "SELECT ROUTINE_NAME, NULL FROM information_schema.ROUTINES "
                      "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'FUNCTION' ORDER BY ROUTINE_NAME",
    Folder.PROCEDURES: "SELECT ROUTINE_NAME, NULL FROM information_schema.ROUTINES "
                       "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'PROCEDURE' ORDER BY ROUTINE_NAME",
    Folder.TRIGGERS: "SELECT TRIGGER_NAME, CONCAT('on ', EVENT_OBJECT_TABLE) FROM information_schema.TRIGGERS "
                     "WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME",
    Folder.EVENTS: "SELECT EVENT_NAME, LOWER(STATUS) FROM information_schema.EVENTS "
                   "WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME",
}
_COLUMNS = """
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, COLUMN_COMMENT
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION
"""
_INDEXES = """
SELECT INDEX_NAME, MIN(NON_UNIQUE) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
GROUP BY INDEX_NAME ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME
"""
_FOREIGN_KEYS = """
SELECT CONSTRAINT_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION SEPARATOR ', '),
       REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME,
       GROUP_CONCAT(REFERENCED_COLUMN_NAME ORDER BY ORDINAL_POSITION SEPARATOR ', ')
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL
GROUP BY CONSTRAINT_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME ORDER BY CONSTRAINT_NAME
"""
_TABLE_TRIGGERS = """
SELECT TRIGGER_NAME, LOWER(CONCAT(ACTION_TIMING, ' ', EVENT_MANIPULATION)) FROM information_schema.TRIGGERS
WHERE EVENT_OBJECT_SCHEMA = %s AND EVENT_OBJECT_TABLE = %s ORDER BY TRIGGER_NAME
"""
_INDEX_PARTS = """
SELECT NON_UNIQUE, INDEX_TYPE, COLUMN_NAME, SUB_PART, COLLATION, EXPRESSION FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s ORDER BY SEQ_IN_INDEX
"""
_FOREIGN_KEY_PARTS = """
SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.UPDATE_RULE, r.DELETE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND r.TABLE_NAME = k.TABLE_NAME
WHERE k.TABLE_SCHEMA = %s AND k.TABLE_NAME = %s AND k.CONSTRAINT_NAME = %s ORDER BY k.ORDINAL_POSITION
"""
```

The generated DDL (`q = quote_ident_mysql`):

```python
def _index_ddl(schema: str, table: str, name: str, parts: list[tuple]) -> str:
    """parts: (NON_UNIQUE, INDEX_TYPE, COLUMN_NAME, SUB_PART, COLLATION, EXPRESSION) in index order."""
    columns = ", ".join(
        (f"({expression})" if expression else q(column) + (f"({sub_part})" if sub_part else ""))
        + (" DESC" if collation == "D" else "")
        for _, _, column, sub_part, collation, expression in parts
    )
    target = f"{q(schema)}.{q(table)}"
    if name == "PRIMARY":
        return f"ALTER TABLE {target} ADD PRIMARY KEY ({columns});"
    non_unique, index_type = parts[0][0], parts[0][1]
    prefix = {"FULLTEXT": "FULLTEXT ", "SPATIAL": "SPATIAL "}.get(index_type, "" if non_unique else "UNIQUE ")
    return f"CREATE {prefix}INDEX {q(name)} ON {target} ({columns});"


def _foreign_key_ddl(schema: str, table: str, name: str, parts: list[tuple]) -> str:
    """parts: (COLUMN_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, UPDATE_RULE,
    DELETE_RULE) in key order."""
    _, ref_schema, ref_table, _, on_update, on_delete = parts[0]
    columns = ", ".join(q(p[0]) for p in parts)
    ref_columns = ", ".join(q(p[3]) for p in parts)
    return (
        f"ALTER TABLE {q(schema)}.{q(table)} ADD CONSTRAINT {q(name)} FOREIGN KEY ({columns}) "
        f"REFERENCES {q(ref_schema)}.{q(ref_table)} ({ref_columns}) ON UPDATE {on_update} ON DELETE {on_delete};"
    )
```

An index or foreign key whose part query returns no rows raises `NotFound`.

- [ ] **Step 4: Implement `schema.py`.** Use seven fixed queries, whatever the number of tables:
  1. `SCHEMATA`;
  2. `COUNT(*)` of non-system `TABLES`;
  3. `TABLES` (name, `TABLE_TYPE`);
  4. `COLUMNS` (`COLUMN_TYPE` as `type_name`, by ordinal);
  5. `KEY_COLUMN_USAGE` rows with `CONSTRAINT_NAME = 'PRIMARY' OR REFERENCED_TABLE_NAME IS NOT NULL` (by table, constraint, ordinal);
  6. `ROUTINES` with `ROUTINE_TYPE = 'FUNCTION'`;
  7. `SELECT DATABASE()`.

  Each relation query takes a scope predicate built as:

```python
_SYSTEM_LIST = "('information_schema', 'mysql', 'performance_schema', 'sys')"


def _scope(column: str, current_only: bool) -> str:
    """Which databases a snapshot query reads: every non-system one, or (over RELATION_LIMIT) the current one."""
    return f"{column} = DATABASE()" if current_only else f"{column} NOT IN {_SYSTEM_LIST}"
```

  Assembly rules:
  - `build_snapshot` reads `RELATION_LIMIT` at call time (the module global; the test monkeypatches it).
  - Schemas from `SCHEMATA` with `system=name in SYSTEM_SCHEMAS`; system schemas get no relations.
  - `kind="view"` when `TABLE_TYPE = 'VIEW'`, else `"table"`.
  - `primary_key` from the PRIMARY rows. FKs grouped by (schema, table, constraint) into `SnapshotForeignKey(name, columns, ref_schema, ref_table, ref_columns)`.
  - Functions per schema, sorted. Rows of a relation or schema that disappeared meanwhile are skipped.
  - Return `SchemaSnapshot(database=None, default_schema=current, search_path=[current] if current else [], schemas=…, built_at=datetime.now(UTC))`.
  - When the count is over `RELATION_LIMIT`, log INFO `"MySQL schema snapshot limited to the current database {}: {} relations on the server (limit {})"`. With no current database, nothing is loaded.
  - Log DEBUG with the schema and relation counts.

- [ ] **Step 5: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_mysql_introspection.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/adapters/mysql backend/tests/test_mysql_introspection.py
git commit -m "feat(mysql): object tree, SHOW CREATE and generated index/FK DDL, schema snapshot with a relation cap"
```

---

### Task 9: MySQL adapter: connect, run, types, errors, cancel, transactions, read-only; registration

**Files:**
- Create: `backend/dbaguy_backend/adapters/mysql/types.py`, `backend/dbaguy_backend/adapters/mysql/adapter.py`
- Modify: `backend/dbaguy_backend/container.py` (`default_registry()`)
- Test: `backend/tests/test_mysql_types.py`, `backend/tests/test_mysql_adapter.py`, `backend/tests/test_mysql_sessions.py`
- Modify test: `backend/tests/test_api.py::test_engines` (breaks once MySQL is registered: the engine set grows)

**Interfaces:**
- Consumes:
  - `settings.connect_params`, `kill_params`, `requires_tls`, `parse_mysql_url`, `URL_SCHEMES` (Task 7);
  - `introspection.list_children`, `object_detail`; `schema.build_snapshot`; `catalog.first_row` (Task 8);
  - `first_keyword(sql, Dialect.MYSQL)` (Task 1);
  - `AdapterCapabilities` (Task 5);
  - `build_browse_select`, `SortKey` (Task 3, for the backtick test).
- Produces:
  - `types.BINARY_CHARSET = 63`; `types.ColumnKind(name: str, category: ColumnCategory, convert: Callable[[bytes], Any])`; `types.column_kind(description: tuple) -> ColumnKind`; `types.column_meta(description: tuple) -> ColumnMeta`; `types.row_converter(descriptions: Sequence[tuple]) -> Callable[[Sequence[bytes | None]], list[Any]]`.
  - `adapter.MysqlAdapter`: `engine=Engine.MYSQL`, `url_schemes=URL_SCHEMES`, `default_port=3306`, `dialect=Dialect.MYSQL`, `capabilities=AdapterCapabilities()` (all defaults: one connection sees every database, KILL QUERY leaves the transaction alone, READ ONLY sessions exist).
  - `adapter.MysqlConnection(DatabaseConnection)`, `adapter.MysqlResult(StatementResult)`.
  - `default_registry()` → `AdapterRegistry([PostgresAdapter(), MysqlAdapter(), SqliteAdapter()])`.
- No abstract method is added, so `FakeConnection` is untouched.

- [ ] **Step 1: Write the failing type tests** — `backend/tests/test_mysql_types.py` (pure, no DB):

```python
import pytest
from mysql.connector.constants import FieldFlag, FieldType

from dbaguy_backend.adapters.mysql.types import column_meta, row_converter

UTF8MB4 = 255


def desc(field_type, flags=0, charset=UTF8MB4, name="c"):
    return (name, field_type, None, None, None, None, 1, flags, charset)


@pytest.mark.parametrize("description, raw, value, type_name, category", [
    (desc(FieldType.LONGLONG, FieldFlag.UNSIGNED), b"18446744073709551615", 18446744073709551615, "bigint unsigned", "number"),
    (desc(FieldType.TINY), b"1", 1, "tinyint", "number"),
    (desc(FieldType.DOUBLE), b"1.5", 1.5, "double", "number"),
    (desc(FieldType.NEWDECIMAL), b"12345678901234567890.1234567890", "12345678901234567890.1234567890", "decimal", "number"),
    (desc(FieldType.BIT, FieldFlag.UNSIGNED), b"\xa5", 165, "bit", "number"),
    (desc(FieldType.DATE), b"0000-00-00", "0000-00-00", "date", "datetime"),
    (desc(FieldType.TIME), b"-838:59:59", "-838:59:59", "time", "datetime"),
    (desc(FieldType.YEAR, FieldFlag.UNSIGNED | FieldFlag.ZEROFILL), b"2026", "2026", "year", "datetime"),
    (desc(FieldType.JSON), b'{"a": 1}', '{"a": 1}', "json", "json"),
    (desc(FieldType.BLOB, charset=63), b"\xde\xad", b"\xde\xad", "blob", "binary"),
    (desc(FieldType.BLOB), "中文".encode(), "中文", "text", "text"),
    (desc(FieldType.VAR_STRING, charset=63), b"\x01\x02", b"\x01\x02", "varbinary", "binary"),
    (desc(FieldType.STRING, FieldFlag.ENUM), b"happy", "happy", "enum", "text"),
    (desc(FieldType.STRING, FieldFlag.SET), b"a,b", "a,b", "set", "text"),
    (desc(FieldType.VAR_STRING), b"\xff", "�", "varchar", "text"),  # never raises on bad UTF-8
])
def test_conversion_table(description, raw, value, type_name, category):
    meta = column_meta(description)
    assert (meta.type_name, meta.category) == (type_name, category)
    assert row_converter([description])([raw]) == [value]
    assert row_converter([description])([None]) == [None]


def test_bytearray_input_is_converted_like_bytes():
    assert row_converter([desc(FieldType.LONG), desc(FieldType.VAR_STRING)])([bytearray(b"7"), bytearray(b"x")]) == [7, "x"]
```

- [ ] **Step 2: Write the failing adapter tests** — `backend/tests/test_mysql_adapter.py`:

```python
import threading
import time

import mysql.connector
import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementError
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.errors import ConnectionFailed
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SshConfig, SslConfig
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService, ResolvedSecrets
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.select_builder import SortKey, build_browse_select
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import (
    BASTION_PORT, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, make_mysql_profile, mysql_driver_kwargs, requires_mysql,
    requires_mysql_bastion,
)
from tests.test_ssh_tunnel import TEST_SSH_OPTIONS

pytestmark = requires_mysql
ENDPOINT = Endpoint(MYSQL_HOST, MYSQL_PORT)
ENDLESS = "SELECT a.id FROM big_table a CROSS JOIN big_table b"  # 10^10 rows: the server streams until stopped


def open_mysql(**overrides):
    return MysqlAdapter().open(make_mysql_profile(**overrides), ENDPOINT, MYSQL_PASSWORD, None)


@pytest.fixture
def conn():
    connection = open_mysql()
    yield connection
    connection.close()


def drain(result):
    rows = []
    while batch := result.fetch(5000):
        rows += batch
    return rows


def test_wrong_password():
    with pytest.raises(ConnectionFailed, match="Access denied"):
        MysqlAdapter().open(make_mysql_profile(), ENDPOINT, "wrong", None)


def test_server_info(conn):
    info = conn.server_info()
    assert info.version.startswith("MySQL 8.4") and info.current_database == "dbaguy"
    assert info.current_user.startswith("dbaguy@")


def test_stream_big_table_in_pages(conn):
    result = conn.run("SELECT id, label, value FROM big_table ORDER BY id")
    assert [(c.name, c.type_name, c.category) for c in result.columns()] == [
        ("id", "int", "number"), ("label", "varchar", "text"), ("value", "double", "number"),
    ]
    first = result.fetch(500)
    assert len(first) == 500 and first[0] == [1, "row 1", 1.5]
    assert len(first) + len(drain(result)) == 100_000
    result.close()


def test_types(conn):
    result = conn.run("SELECT * FROM types_demo ORDER BY id")
    assert [c.category for c in result.columns()] == [
        "number", "number", "number", "number", "number", "number", "number", "datetime", "datetime", "datetime",
        "datetime", "datetime", "json", "binary", "binary", "text", "text", "text", "text",
    ]
    full, empty = result.fetch(2)
    assert full == [
        1, 18446744073709551615, -9007199254740993, "12345678901234567890.1234567890", 1.5, 1, 165, "0000-00-00",
        "2026-10-17 12:34:56", "2026-10-17 12:34:56", "-12:30:00", "2026", '{"a": [1, 2], "b": null}',
        b"\xde\xad\xbe\xef", b"\x01\x02\x03\x04", "happy", "a,b", "it's \\ back", "中文 😀",
    ]
    assert empty == [2] + [None] * 18
    result.close()
    probe = conn.run("SELECT CAST(0 AS DATETIME) z, 1.50 d, 18446744073709551615 big, JSON_OBJECT('a', 1) j, x'0102' b")
    assert probe.fetch(1) == [[None, "1.50", 18446744073709551615, '{"a": 1}', b"\x01\x02"]]
    probe.close()


def test_zero_row_select_has_columns_and_dml_reports_affected_rows(conn):
    result = conn.run("SELECT id, label FROM big_table WHERE id < 0")
    assert [c.name for c in result.columns()] == ["id", "label"] and result.fetch(10) == []
    result.close()
    conn.run("CREATE TEMPORARY TABLE affected (id INT)").close()
    insert = conn.run("INSERT INTO affected VALUES (1), (2)")
    assert insert.columns() is None and insert.affected_rows() == 2
    insert.close()


def test_warnings_become_one_message(conn):
    result = conn.run("SELECT CAST('abc' AS SIGNED) AS n")
    assert result.fetch(5) == [[0]]
    assert [m.text for m in result.take_messages()] == ["1 warning: run SHOW WARNINGS to see it"]
    assert result.take_messages() == []
    result.close()


def test_call_returns_every_result_set(conn):
    result = conn.run("CALL two_results(3)")
    assert result.fetch(10) == [[1, "row 1"], [2, "row 2"], [3, "row 3"]]
    assert result.next_result() and result.fetch(10) == [[100000]]
    assert result.next_result() and result.columns() is None and result.affected_rows() == 0
    assert not result.next_result()
    result.close()
    assert conn.run("SELECT 1").fetch(1) == [[1]]  # no "Commands out of sync"


def test_procedure_failing_after_a_result_set_leaves_the_connection_usable(conn):  # Review Focus 4
    result = conn.run("CALL fail_after_select()")
    assert result.fetch(5) == [[1]]
    with pytest.raises(StatementError, match="boom after the first result") as info:
        result.next_result()
    assert info.value.code == "1644"
    result.close()
    assert conn.run("SELECT 2").fetch(1) == [[2]]


def test_closing_a_fully_sent_result_does_not_poison_the_next_statement(conn):  # Review Focus 4
    result = conn.run("SELECT id FROM big_table WHERE id <= 3")
    assert result.fetch(1) == [[1]]
    result.close()
    result.close()  # idempotent
    for _ in range(3):
        assert conn.run("SELECT 7").fetch(1) == [[7]]


def test_cancel_with_nothing_running_is_harmless(conn):  # Review Focus 4
    conn.cancel()
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_one_statement_per_run(conn):
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1; SELECT 2")
    assert info.value.code == "1064"
    assert conn.run("SELECT 2").fetch(1) == [[2]]


def test_text_mentioning_delimiter_is_sent_verbatim(conn):  # Review Focus 1
    result = conn.run("SELECT 1 AS `delimiter` /* delimiter $$ */ -- delimiter ;\n")
    assert [c.name for c in result.columns()] == ["delimiter"] and result.fetch(1) == [[1]]
    result.close()
    text = conn.run("SELECT 'DELIMITER //\nx' AS s")
    assert text.fetch(1) == [["DELIMITER //\nx"]]
    text.close()


def test_errors_carry_errno_sqlstate_and_line(conn):
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1,\nFROM t")  # "… near 'FROM t' at line 2"
    assert info.value.code == "1064" and (info.value.position.line, info.value.position.column) == (2, 1)
    with pytest.raises(StatementError) as missing:
        conn.run("SELECT * FROM nope")
    assert missing.value.code == "1146" and "SQLSTATE: 42S02" in missing.value.message


def test_cancel_a_running_statement(conn):
    threading.Timer(0.5, conn.cancel).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        conn.run("SELECT SLEEP(30)")
    assert time.monotonic() - started < 2.5
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_cancel_mid_stream_then_reuse(conn):
    result = conn.run(ENDLESS)
    assert len(result.fetch(1000)) == 1000
    started = time.monotonic()
    conn.cancel()
    with pytest.raises(StatementCancelled):
        drain(result)
    assert time.monotonic() - started < 3
    result.close()
    assert conn.run("SELECT 1").fetch(1) == [[1]]


def test_closing_a_partly_read_stream_inside_a_transaction_keeps_the_transaction(conn):
    conn.set_autocommit(False)
    conn.run("CREATE TEMPORARY TABLE tx_rows (id INT)").close()
    conn.run("INSERT INTO tx_rows VALUES (1)").close()
    assert conn.in_transaction()
    result = conn.run(ENDLESS)
    assert len(result.fetch(500)) == 500
    result.close()
    assert conn.in_transaction()
    conn.run("INSERT INTO tx_rows VALUES (2)").close()
    conn.commit()
    assert not conn.in_transaction()
    assert conn.run("SELECT COUNT(*) FROM tx_rows").fetch(1) == [[2]]


def test_manual_commit_mode_and_transaction_state(conn):
    assert conn.autocommit()
    conn.set_autocommit(False)
    assert not conn.autocommit()
    conn.run("SELECT 1").close()
    assert not conn.in_transaction()  # a plain read leaves no transaction behind (protocol flag)
    conn.run("CREATE TEMPORARY TABLE st_rows (id INT)").close()
    conn.run("INSERT INTO st_rows VALUES (1)").close()
    assert conn.in_transaction()
    with pytest.raises(StatementError) as info:
        conn.set_autocommit(True)
    assert info.value.code == "transaction_open"
    conn.rollback()
    assert not conn.in_transaction()
    conn.run("INSERT INTO st_rows VALUES (2)").close()
    conn.run("CREATE TABLE IF NOT EXISTS st_ddl_probe (id INT)").close()  # DDL commits implicitly
    assert not conn.in_transaction()
    conn.run("DROP TABLE IF EXISTS st_ddl_probe").close()
    conn.run("SET autocommit = 1").close()  # typed by the user: recalibrated once no result is open
    assert conn.autocommit()


def test_read_only_profile_is_read_only_in_the_database():
    conn = open_mysql(read_only=True)
    try:
        with pytest.raises(StatementError) as info:
            conn.run("INSERT INTO job_log (message) VALUES ('ro')")
        assert info.value.code == "1792"  # ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION
        conn.set_autocommit(False)
        with pytest.raises(StatementError) as manual:
            conn.run("UPDATE job_log SET message = 'x' WHERE id = 1")
        assert manual.value.code == "1792"
    finally:
        conn.close()


def test_a_killed_connection_is_reported_as_lost(conn):
    result = conn.run("SELECT CONNECTION_ID()")
    [[thread]] = result.fetch(1)
    result.close()
    killer = mysql.connector.connect(**mysql_driver_kwargs())
    killer.cmd_query(f"KILL {thread}")
    killer.close()
    with pytest.raises(StatementError) as info:
        conn.run("SELECT 1").fetch(1)
    assert info.value.code == "connection_lost"


def test_tls_modes():
    def cipher(connection):
        result = connection.run("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        [[_, value]] = result.fetch(5)
        result.close()
        return value

    secure, plain = open_mysql(ssl=SslConfig(mode="require")), open_mysql(ssl=SslConfig(mode="disable"))
    try:
        assert cipher(secure) and cipher(plain) == ""
    finally:
        secure.close()
        plain.close()
    with pytest.raises(ConnectionFailed, match="(?i)certificate"):  # the dev server's CA is self-signed
        open_mysql(ssl=SslConfig(mode="verify-ca"))


def test_browse_select_on_a_backtick_name_runs(conn):  # Review Focus 3
    path = [PathSegment(kind="schema", name="dbaguy"), PathSegment(kind="folder", name="tables"),
            PathSegment(kind="table", name="tick`table")]
    sql = build_browse_select(Dialect.MYSQL, path, "`a``b` = 1", [SortKey(column="a`b", descending=True)])
    assert conn.run(sql).fetch(5) == [[1, "one"]]


def test_registry_and_url_import():
    registry = default_registry()
    assert registry.get("mysql").info().dialect == Dialect.MYSQL
    parsed = registry.parse_url("jdbc:mysql://dbaguy@127.0.0.1:33067/dbaguy?useSSL=false")
    assert (parsed.profile.engine, parsed.profile.port, parsed.profile.ssl.mode) == ("mysql", 33067, "disable")


@requires_mysql_bastion
def test_mysql_through_the_ssh_bastion_including_cancel(tmp_path):  # Review Focus 5
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    tunnels = SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)
    service = ConnectionService(AdapterRegistry([MysqlAdapter()]), profiles, tunnels)
    profile = make_mysql_profile(
        host="mysql", port=3306,
        ssh=SshConfig(enabled=True, host="127.0.0.1", port=BASTION_PORT, user="dev", auth="password"),
    )
    lease = service.open(profile, None, ResolvedSecrets(password=MYSQL_PASSWORD, ssh_password="dev"))
    try:
        connection = lease.connection
        assert connection.server_info().version.startswith("MySQL 8.4")
        threading.Timer(0.5, connection.cancel).start()
        started = time.monotonic()
        with pytest.raises(StatementCancelled):
            connection.run("SELECT SLEEP(30)")
        assert time.monotonic() - started < 3
        assert connection.run("SELECT 1").fetch(1) == [[1]]
    finally:
        lease.close()
    assert tunnels.active_count() == 0
```

`backend/tests/test_mysql_sessions.py` (worker level, mirroring `test_postgres_sessions.py`):

```python
import time

import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.domain.execution import CommitCommand, ExecuteCommand, RollbackCommand, SetAutocommitCommand
from dbaguy_backend.sessions.worker import SessionWorker
from tests.fake_db import FakeLease
from tests.support import MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, make_mysql_profile, requires_mysql
from tests.test_sessions import Recorder, finished, paused

pytestmark = requires_mysql
LARGE_SELECT = "SELECT a.id FROM big_table a CROSS JOIN big_table b"


@pytest.fixture
def mysql_worker():
    lease = FakeLease(MysqlAdapter().open(make_mysql_profile(), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None))
    worker = SessionWorker("mysql", lambda: lease)
    worker.start()
    recorder = Recorder()
    worker.set_emitter(recorder)
    yield worker, recorder
    worker.close()


def run(worker, rec, rid, sql, page_size=500):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql, page_size=page_size))
    done = rec.wait_for(finished(rid), timeout=30)
    assert not rec.of(rid, "error"), rec.of(rid, "error")[0].message
    return done


def command(worker, rec, cmd):
    worker.submit(cmd)
    done = rec.wait_for(finished(cmd.request_id), timeout=30)
    assert not rec.of(cmd.request_id, "error")
    return done


def test_statement_timeout_on_mysql(mysql_worker):
    worker, rec = mysql_worker
    worker.submit(ExecuteCommand(request_id="t", sql="SELECT SLEEP(5)", timeout_s=0.3))
    done = rec.wait_for(finished("t"), timeout=30)
    assert done.timed_out and done.cancelled and done.elapsed_ms < 3000
    assert rec.of("t", "message")[-1].text == "Timed out after 0.3 s"
    assert not run(worker, rec, "u", "SELECT 1").cancelled


def test_cancel_inside_a_manual_transaction_keeps_the_transaction(mysql_worker):
    worker, rec = mysql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "c", "CREATE TEMPORARY TABLE keep_rows (id INT)")
    assert run(worker, rec, "i", "INSERT INTO keep_rows VALUES (1)").in_transaction
    worker.submit(ExecuteCommand(request_id="s", sql="SELECT SLEEP(30)"))
    rec.wait_for(lambda e: e.type == "started" and e.request_id == "s", timeout=10)
    time.sleep(0.3)
    started = time.monotonic()
    worker.cancel("s")
    done = rec.wait_for(finished("s"), timeout=10)
    assert done.cancelled and time.monotonic() - started < 2
    assert done.in_transaction  # KILL QUERY ends the statement, not the transaction
    assert not command(worker, rec, CommitCommand(request_id="k")).in_transaction
    run(worker, rec, "n", "SELECT COUNT(*) FROM keep_rows")
    assert rec.of("n", "rows")[0].rows == [[1]]


def test_paused_large_select_inside_a_transaction_does_not_end_it(mysql_worker):
    worker, rec = mysql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "t", "CREATE TEMPORARY TABLE keep_page (id INT)")
    run(worker, rec, "i", "INSERT INTO keep_page VALUES (1)")
    worker.submit(ExecuteCommand(request_id="big", sql=LARGE_SELECT, page_size=500))
    assert rec.wait_for(paused("big", 500), timeout=30).has_more
    assert not command(worker, rec, CommitCommand(request_id="c")).in_transaction  # discards the paused result first
    run(worker, rec, "n", "SELECT COUNT(*) FROM keep_page")
    assert rec.of("n", "rows")[0].rows == [[1]]


def test_manual_mode_reports_the_transaction_and_rolls_back(mysql_worker):
    worker, rec = mysql_worker
    command(worker, rec, SetAutocommitCommand(request_id="m", enabled=False))
    run(worker, rec, "t", "CREATE TEMPORARY TABLE badge (id INT)")
    assert run(worker, rec, "u", "INSERT INTO badge VALUES (1)").in_transaction
    assert not command(worker, rec, RollbackCommand(request_id="r")).in_transaction
    run(worker, rec, "n", "SELECT COUNT(*) FROM badge")
    assert rec.of("n", "rows")[0].rows == [[0]]
```

In `tests/test_api.py::test_engines`, the set becomes `{"postgres", "mysql", "sqlite"}`. Add: `assert (engines["mysql"]["per_database_connection"], engines["mysql"]["cancel_aborts_transaction"], engines["mysql"]["server_read_only"]) == (False, False, True)`.

- [ ] **Step 3: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_mysql_types.py tests/test_mysql_adapter.py tests/test_mysql_sessions.py tests/test_api.py -q`
Expected: `ImportError` for `types` and `adapter`; `test_engines` fails on the set.

- [ ] **Step 4: Implement `adapters/mysql/types.py`** (the text-first conversion table, spec §3.5):

```python
BINARY_CHARSET = 63  # MySQL's `binary` character set: BLOB/BINARY/VARBINARY hold bytes, not text


def _int(raw: bytes) -> int:
    return int(raw)


def _float(raw: bytes) -> float:
    return float(raw)


def _text(raw: bytes) -> str:
    return bytes(raw).decode("utf-8", errors="replace")  # the connection charset is utf8mb4; never raise on bad bytes


def _bytes(raw: bytes) -> bytes:
    return bytes(raw)


def _bit(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


@dataclass(frozen=True, slots=True)
class ColumnKind:
    name: str
    category: ColumnCategory
    convert: Callable[[bytes], Any]


_INTEGERS = {FieldType.TINY: "tinyint", FieldType.SHORT: "smallint", FieldType.INT24: "mediumint",
             FieldType.LONG: "int", FieldType.LONGLONG: "bigint"}
_FIXED: dict[int, ColumnKind] = {
    **{t: ColumnKind(n, ColumnCategory.NUMBER, _int) for t, n in _INTEGERS.items()},
    FieldType.FLOAT: ColumnKind("float", ColumnCategory.NUMBER, _float),
    FieldType.DOUBLE: ColumnKind("double", ColumnCategory.NUMBER, _float),
    FieldType.DECIMAL: ColumnKind("decimal", ColumnCategory.NUMBER, _text),  # exact digits: text, like PG numeric
    FieldType.NEWDECIMAL: ColumnKind("decimal", ColumnCategory.NUMBER, _text),
    FieldType.BIT: ColumnKind("bit", ColumnCategory.NUMBER, _bit),
    FieldType.DATE: ColumnKind("date", ColumnCategory.DATETIME, _text),  # text keeps 0000-00-00
    FieldType.NEWDATE: ColumnKind("date", ColumnCategory.DATETIME, _text),
    FieldType.DATETIME: ColumnKind("datetime", ColumnCategory.DATETIME, _text),
    FieldType.TIMESTAMP: ColumnKind("timestamp", ColumnCategory.DATETIME, _text),
    FieldType.TIME: ColumnKind("time", ColumnCategory.DATETIME, _text),  # TIME can be negative or over 24 h
    FieldType.YEAR: ColumnKind("year", ColumnCategory.DATETIME, _text),
    FieldType.JSON: ColumnKind("json", ColumnCategory.JSON, _text),
    FieldType.GEOMETRY: ColumnKind("geometry", ColumnCategory.BINARY, _bytes),
    FieldType.VECTOR: ColumnKind("vector", ColumnCategory.BINARY, _bytes),
    FieldType.NULL: ColumnKind("null", ColumnCategory.OTHER, _text),
}
_STRINGS: dict[int, tuple[str, str]] = {  # (text name, binary name)
    FieldType.VARCHAR: ("varchar", "varbinary"), FieldType.VAR_STRING: ("varchar", "varbinary"),
    FieldType.STRING: ("char", "binary"), FieldType.TINY_BLOB: ("text", "blob"), FieldType.MEDIUM_BLOB: ("text", "blob"),
    FieldType.LONG_BLOB: ("text", "blob"), FieldType.BLOB: ("text", "blob"),
}


def column_kind(description: tuple) -> ColumnKind:
    """description: a mysql-connector cursor description entry (name, type, …, flags at [7], charset at [8])."""
    field_type, flags, charset = description[1], description[7], description[8]
    if field_type in _STRINGS:
        text_name, binary_name = _STRINGS[field_type]
        if charset == BINARY_CHARSET:
            return ColumnKind(binary_name, ColumnCategory.BINARY, _bytes)
        if flags & FieldFlag.ENUM:
            return ColumnKind("enum", ColumnCategory.TEXT, _text)
        if flags & FieldFlag.SET:
            return ColumnKind("set", ColumnCategory.TEXT, _text)
        return ColumnKind(text_name, ColumnCategory.TEXT, _text)
    kind = _FIXED.get(field_type) or ColumnKind(f"type {field_type}", ColumnCategory.OTHER, _text)
    if field_type in _INTEGERS and flags & FieldFlag.UNSIGNED:
        return ColumnKind(f"{kind.name} unsigned", kind.category, kind.convert)
    return kind
```

`column_meta(d)` → `ColumnMeta(name=d[0], type_name=kind.name, category=kind.category)`. `row_converter(descriptions)` precomputes the converters and returns `lambda row: [None if v is None else c(v) for c, v in zip(converters, row)]`.

- [ ] **Step 5: Implement `adapters/mysql/adapter.py`.** `first_keyword` here is `sql.lexer.first_keyword` (upper-cased, dialect-aware), not the PG-only helper in `sql_text`. Driver imports: `import mysql.connector`, `from mysql.connector.cursor import MySQLCursorRaw`, `from mysql.connector.connection import MySQLConnection`. These parts go in as written (cursor, results, cancel, close):

```python
_CONNECTION_LOST_ERRNOS = frozenset({1053, 2006, 2013, 2055, 4031})  # shutdown, gone away, lost, lost at, idle kick
_QUERY_INTERRUPTED = 1317
_AT_LINE = re.compile(r" at line (\d+)\s*$")
_MENTIONS_AUTOCOMMIT = re.compile(r"autocommit", re.IGNORECASE)
_MAY_SET_AUTOCOMMIT = frozenset({"CALL", "EXECUTE"})  # a procedure or prepared statement may switch it too
_READ_ONLY_SESSION = "SET SESSION TRANSACTION READ ONLY"


class _VerbatimCursor(MySQLCursorRaw):
    """Sends a statement exactly as written. `MySQLCursor.execute()` first runs any text containing `delimiter `
    through the connector's DELIMITER-script rewriter (even inside a comment or as an alias), which changes user SQL."""

    def execute_verbatim(self, sql: str) -> None:
        self._connection.handle_unread_result()
        self._reset_result()
        statement = sql.encode(self._connection.python_charset)
        self._executed = statement
        self._handle_result(self._connection.cmd_query(statement))


class MysqlResult(StatementResult):
    def __init__(self, owner: "MysqlConnection", cursor: _VerbatimCursor) -> None:
        self._owner, self._cursor = owner, cursor
        self._closed = False
        self._failed = False  # after a driver error, never ask for another result set: its flags may be stale
        self._load_columns()

    def _load_columns(self) -> None:
        description = self._cursor.description
        self._columns = [column_meta(d) for d in description] if description else None
        self._convert = row_converter(description) if description else None
        self._warned = False

    def _translate(self, exc: mysql.connector.Error) -> StatementError:
        self._failed = True
        return self._owner.translate_error(exc)

    def columns(self) -> list[ColumnMeta] | None:
        return self._columns

    def fetch(self, n: int) -> list[list[Any]]:
        if self._columns is None or self._closed:
            return []
        try:
            rows = self._cursor.fetchmany(n)
        except mysql.connector.Error as exc:
            raise self._translate(exc) from exc
        if self._owner.killed():  # rows that reached us after KILL QUERY: the statement was cancelled
            self._failed = True
            raise StatementCancelled()
        return [self._convert(row) for row in rows]

    def affected_rows(self) -> int | None:
        rowcount = self._cursor.rowcount
        return rowcount if self._columns is None and rowcount >= 0 else None

    def next_result(self) -> bool:
        if self._closed or self._failed:
            return False
        try:
            more = bool(self._cursor.nextset())
        except mysql.connector.Error as exc:
            raise self._translate(exc) from exc
        if self._owner.killed():
            self._failed = True
            raise StatementCancelled()
        if more:
            self._load_columns()
        return more

    def take_messages(self) -> list[ServerMessage]:
        """No round trip: the warning count arrives with the end of each result set (SHOW WARNINGS is left to the user)."""
        count = self._cursor.warning_count
        if self._warned or not count or self._owner.unread():
            return []
        self._warned = True
        text = f"{count} warning{'s' if count != 1 else ''}: run SHOW WARNINGS to see {'them' if count != 1 else 'it'}"
        return [ServerMessage(severity=MessageSeverity.WARNING, text=text)]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner.finish(self._cursor, more_results=not self._failed)
```

In `MysqlConnection`:

```python
    def kill_query(self, why: str) -> bool:
        """KILL QUERY <our thread> from a short side connection (same endpoint, account and TLS settings). Ends only
        the running statement: the session, its temporary tables and its transaction stay. Safe from any thread."""
        started = time.monotonic()
        try:  # a fresh conn_attrs per connection: the driver adds its own entries to the dict it is given
            killer = mysql.connector.connect(**self._kill_params, conn_attrs={"program_name": "DbaGuy cancel"})
            try:
                killer.cmd_query(f"KILL QUERY {int(self._thread_id)}")
            finally:
                killer.close()
        except Exception as exc:  # mysql.connector.Error, OSError, …: never let a cancel raise
            logger.warning("MySQL {} failed: KILL QUERY {} not sent ({}: {})", why, self._thread_id, type(exc).__name__, exc)
            return False
        logger.info("MySQL {}: KILL QUERY {} in {:.0f} ms", why, self._thread_id, (time.monotonic() - started) * 1000)
        return True

    def cancel(self) -> None:
        self._killed.set()  # first: SLEEP() may return its 1 before the KILL's own reply reaches us
        if not self.kill_query("cancel"):
            self._killed.clear()  # nothing was interrupted: report what the statement really does

    def killed(self) -> bool:
        return self._killed.is_set()

    def unread(self) -> bool:
        return bool(self._conn.unread_result)

    def finish(self, cursor: _VerbatimCursor, more_results: bool) -> None:
        """End a result: stop the statement if rows are still coming, then read and drop what is left, including later
        result sets of a CALL (otherwise: "Commands out of sync"). The transaction is untouched. If the KILL cannot be
        sent, everything left is read, which is slow for a huge result but keeps the transaction."""
        try:
            if self._conn.unread_result:
                self.kill_query("close of a partly read result")
            while True:
                if self._conn.unread_result:
                    self._conn.consume_results()
                if not more_results or not cursor.nextset():
                    break
        except mysql.connector.Error as exc:
            if exc.errno != _QUERY_INTERRUPTED:  # 1317 is the KILL we just sent
                logger.warning("Discarding the rest of a MySQL result failed: {}", exc)
        finally:
            with suppress(mysql.connector.Error):
                cursor.close()

    def translate_error(self, exc: mysql.connector.Error) -> StatementError:
        if self._killed.is_set() or exc.errno == _QUERY_INTERRUPTED:
            return StatementCancelled()
        text = exc.msg or str(exc)
        # errno -1 OperationalError: "MySQL Connection not available" (the socket is already gone)
        if exc.errno in _CONNECTION_LOST_ERRNOS or (isinstance(exc, mysql.connector.errors.OperationalError) and exc.errno == -1):
            return StatementError(f"Connection to the server was lost: {text}", code="connection_lost")
        message = text + (f"\nSQLSTATE: {exc.sqlstate}" if exc.sqlstate else "")
        line = _AT_LINE.search(text)
        return StatementError(
            message, code=str(exc.errno) if exc.errno > 0 else None,
            position=ErrorPosition(line=int(line.group(1)), column=1) if line else None,
        )

    def run(self, sql: str) -> StatementResult:
        self._killed.clear()
        if _MENTIONS_AUTOCOMMIT.search(sql) or first_keyword(sql, Dialect.MYSQL) in _MAY_SET_AUTOCOMMIT:
            self._autocommit_stale = True
        cursor = _VerbatimCursor(self._conn)  # not conn.cursor(): that sends a PING first, one round trip per statement
        try:
            cursor.execute_verbatim(sql)
        except mysql.connector.Error as exc:
            with suppress(mysql.connector.Error):
                cursor.close()
            raise self.translate_error(exc) from exc
        result = MysqlResult(self, cursor)
        if self._killed.is_set():  # it ended normally after KILL QUERY (SLEEP() returns 1): still a cancel
            result.close()
            raise StatementCancelled()
        return result
```

The rest of `MysqlConnection` follows these rules:
- **`__init__`** takes `(conn, kill_params, read_only: bool, database: str | None)` and keeps `self._thread_id = conn.connection_id`, `self._autocommit = True`, `self._autocommit_stale = False` and `self._killed = threading.Event()`.
- **`autocommit()`:** when `_autocommit_stale` and nothing is unread, run `catalog.first_row(self._conn, "SELECT @@session.autocommit")`, update and clear the flag. On a driver error, keep the last value and log DEBUG. Otherwise return the cached value, with no round trip.
- **`set_autocommit(enabled)`:** no-op when `self.autocommit() == enabled`. Raise `transaction_open_error()` when `self._conn.in_transaction`. Otherwise `cmd_query(f"SET autocommit = {1 if enabled else 0}")` (translate errors), update the cache, and log INFO `"Auto-commit {} on MySQL thread {}"`.
- **`in_transaction()`** returns `bool(self._conn.in_transaction)`: the server status flag from the last packet, no round trip. **`transaction_failed()`** is inherited (False).
- **`commit()` / `rollback()`:** `self._conn.commit()` / `rollback()`, with translated errors.
- **`server_info()`:** `catalog.first_row(conn, "SELECT VERSION(), DATABASE(), CURRENT_USER()")`, refreshes `_database`, and gives `version=f"{'MariaDB' if 'mariadb' in v.lower() else 'MySQL'} {v}"`. **`current_database()`** returns `_database` (no round trip).
- **`list_children` / `object_detail` / `schema_snapshot`** delegate to Task 8 with `self._conn`.
- **`close()`:** `self._conn.close()`; on a `mysql.connector.Error`, `self._conn.shutdown()`.

`MysqlAdapter.open(profile, endpoint, password, database)`:
1. `ValidationFailed("Not a MySQL server profile")` unless it is a `ServerProfile` with an endpoint.
2. `params = connect_params(...)`.
3. `mysql.connector.connect(**params)`. `mysql.connector.Error` → `ConnectionFailed(str(exc).strip())`. `AttributeError`/`ValueError`/`TypeError` (an option the driver does not know or cannot take) → `ConnectionFailed(f"Invalid MySQL connection option: {exc}")`.
4. In a try block that `shutdown()`s the connection on any failure:
   - if `requires_tls(profile.ssl) and not conn.is_secure`, raise `ConnectionFailed(f"The server does not offer TLS, which ssl mode '{profile.ssl.mode}' requires")`;
   - if `profile.read_only`, `cmd_query(_READ_ONLY_SESSION)` (a driver error → `ConnectionFailed(f"Could not make the connection read-only: {exc}")`);
   - then `_enable_keepalive(conn)`.
5. Log INFO `"Connected to MySQL {}:{} db={} tls={}{} thread={}{}"`: the host is `endpoint.tls_server_name or endpoint.host`; `tls` is the mode plus `" (active)"` when `conn.is_secure`; the thread id; `" read_only=True"` when set.
6. Return `MysqlConnection(conn, kill_params(params), profile.read_only, params.get("database"))`.

```python
def _enable_keepalive(conn: MySQLConnection) -> None:
    """TCP keepalive (30 s idle, 10 s interval, 3 probes) as for PostgreSQL; the driver has no option for it, so it is
    set on its socket, best effort."""
    sock = getattr(getattr(conn, "_socket", None), "sock", None)
    if sock is None:
        logger.debug("MySQL socket not reachable; TCP keepalive not set")
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPALIVE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        if (option := getattr(socket, name, None)) is not None:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
```

`parse_url(url)` returns `parse_mysql_url(url)`. Register the adapter in `container.default_registry()` as listed under Interfaces.

- [ ] **Step 6: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest -q`
Expected: all green; no MySQL test is skipped (containers up).

- [ ] **Step 7: Measure streaming against PostgreSQL** (spec §2: switch to the C extension only past 3× PG). This is a throwaway check and is not committed:

```bash
cd backend && .venv/bin/python - <<'EOF'
import time
from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from tests.support import (MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, PG_HOST, PG_PASSWORD, PG_PORT,
                           make_mysql_profile, make_pg_profile)
for name, adapter, profile, endpoint, password in [
    ("postgres", PostgresAdapter(), make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD),
    ("mysql", MysqlAdapter(), make_mysql_profile(), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD),
]:
    conn = adapter.open(profile, endpoint, password, None)
    for _ in range(2):  # the second pass is the one that counts (warm caches)
        started = time.perf_counter()
        result = conn.run("SELECT id, label, value FROM big_table ORDER BY id")
        total = 0
        while rows := result.fetch(500):
            total += len(rows)
        result.close()
    print(name, total, f"{time.perf_counter() - started:.2f} s")
    conn.close()
EOF
```

Expected: two lines with 100000 rows each. Put both timings in the commit body. If MySQL is over 3× PG, **stop and report BLOCKED** to the controller with the numbers; do not switch drivers in this task, because the C extension changes the cursor classes and the packaging.

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/adapters/mysql backend/dbaguy_backend/container.py backend/tests
git commit -m "feat(mysql): MySQL adapter — verbatim streaming, type table, KILL QUERY cancel and close, transactions, read-only session" \
  -m "Streaming 100k rows: PostgreSQL <x> s, MySQL <y> s (pure driver)."
```

---

### Task 10: Frontend: capability gating, value filter endpoint, MySQL in the connection dialog and explorer

**Files:**
- Modify: `src/features/connections/hooks.ts`, `src/features/connections/ConnectionDialog.tsx`
- Modify: `src/features/query/ConnectionPicker.tsx`
- Modify: `src/features/session/SessionController.ts`, `src/features/session/SessionGuards.tsx`, `src/features/session/transactions.ts`
- Modify: `src/features/grid/ExportDialog.tsx`
- Modify: `src/features/table-data/filterValue.ts`, `src/features/table-data/TableDataTab.tsx`
- Modify: `src/features/explorer/kinds.tsx`

**Interfaces:**
- Consumes:
  - `EngineInfo.per_database_connection` / `cancel_aborts_transaction` (Task 5);
  - `POST /sql/value-condition` with `{dialect, column, value, category}` → `{condition: string | null}` (Task 3);
  - node kinds `event`, `foreign_key` and folders `events`, `foreign_keys` (Task 5);
  - the registered `mysql` engine (Task 9).
- Produces:
  - `useEngineInfo(profileId: string | null): EngineInfo | null` (hooks.ts); `useDialect` is reimplemented as `useEngineInfo(id)?.dialect ?? null` (DRY).
  - `SessionController.profileId` becomes `readonly` public (was `private readonly`).
  - `valueCondition(dialect: Dialect, column: ColumnMeta, value: unknown): Promise<string | null>` (filterValue.ts). `quoteIdent` and `filterForValue` are deleted.

Rules:
- **Database picker:** in `ConnectionPicker`, the `DatabasePicker` shows when `useEngineInfo(profileId)?.per_database_connection` is true (PG yes; MySQL and SQLite no). Update the component's doc comment to say "for engines with one connection per database".
- **Export-cancel warning** shows only where it is true:
  - `ExportDialog`: `const cancelAbortsTransaction = inTransaction && (useEngineInfo(controller.profileId)?.cancel_aborts_transaction ?? true)`. Use it for the footer warning, for `askCancel` (confirm only when true; otherwise cancel at once) and for the confirm message.
  - `SessionGuards`: the export step's `inTransaction` becomes `inTransaction && cancelAborts`, where `cancelAborts` is read from `useEngineInfo(controller?.profileId ?? null)` and kept in the existing `latest` ref.
  - `transactions.ts`: the doc comment of `EXPORT_CANCEL_ABORTS_TRANSACTION` says it applies to engines whose `cancel_aborts_transaction` is true (PostgreSQL).
- **Filter by this value:** the `TableDataTab` cell-menu item is enabled whenever `column && rows && row < rows.length && dialect && !exporting`. `onSelect` calls `valueCondition(dialect, column, rows[row][col])`:
  - a string → `applyFilter(addCondition(state.filter, condition))`;
  - `null` → `toast("info", "This value cannot be matched exactly: the grid holds only its beginning")`;
  - a rejection → `toast("error", errorMessage(error))`.
- `valueCondition` uses `api()`/`unwrap` like `splitText` and sends `{dialect, column: column.name, value, category: column.category}`.
- **Explorer kinds:** `event` gets `CalendarClock` and the label "Event"; `foreign_key` gets `Link2` and the label "Foreign Key". The folders `events` → `CalendarClock` / "Events" and `foreign_keys` → `Link2` / "Foreign Keys".
- **ConnectionDialog:**
  - URL placeholder per selected engine via `const URL_EXAMPLES: Partial<Record<Engine, string>> = { postgres: "postgres://user@host:5432/db", mysql: "mysql://user@host:3306/db or jdbc:mysql://host:3306/db", sqlite: "sqlite:///path/to.db" }`, falling back to the postgres example.
  - SSL mode option labels per engine via `const SSL_MODE_LABELS: Partial<Record<Engine, Record<SslConfig["mode"], string>>> = { mysql: { disable: "disable (DISABLED)", prefer: "prefer (PREFERRED)", require: "require (REQUIRED)", "verify-ca": "verify-ca (VERIFY_CA)", "verify-full": "verify-full (VERIFY_IDENTITY)" } }`. Other engines show the raw mode.
  - For MySQL, the Mode row hint reads "verify-full cannot be used through an SSH tunnel". The stored values do not change.
  - No read-only note in 03a (03b adds it for `server_read_only: false`).
- No new key handlers. The `"postgres"` fallbacks in `completion.ts`, `format.ts` and `copy.ts` apply only without a connection; leave them. Colours come only from tokens.

- [ ] **Step 1: Implement the rules above.**
- [ ] **Step 2: Verify the build.**

Run: `npm run typecheck && npm run build`
Expected: both pass.

- [ ] **Step 3: Manual check in the browser.** Run `scripts/dev.sh --web` and open http://localhost:1420, with the dev containers up.
  1. New Connection → Engine MySQL. The URL placeholder shows the MySQL example. Import `jdbc:mysql://dbaguy@127.0.0.1:33067/dbaguy?useSSL=false`: host, port 33067, database, user and SSL `disable` fill in. Password `dbaguy`, untick "Save password in Keychain", Test → "Connected to MySQL 8.4.x", Save. The SSL tab shows the MySQL names.
  2. The explorer shows `dbaguy` and `analytics` first, with the system schemas greyed. `dbaguy` → Tables / Views / Functions / Procedures / Triggers / Events (with icons). `orders` → Columns / Indexes / Foreign Keys / Triggers. Double-click `orders_customer_idx` and `orders_customer_fk`: generated DDL. Double-click `purge_job_log`: the event DDL. Open `tick`table` and `Order Items` as tables: rows load.
  3. Table tab on `types_demo`: the zero date shows `0000-00-00`, big numbers are exact, blobs show hex. Right-click → "Filter by this value":
     - on `note` (`it's \ back`), the filter becomes ``note = 'it''s \\ back'`` and returns row 1;
     - on `flag`, `flag = TRUE` returns row 1;
     - on a blob cell, `data = X'deadbeef'` returns row 1.
  4. A query tab bound to MySQL has no database picker (a PG tab still has one).
     - Paste the contents of `dev/mysql/init/01-routines.sql` with each name changed to `…_copy`, the trigger on `job_log` (two triggers cannot be `BEFORE INSERT ON orders` at once), and the `USE` line removed.
     - With the cursor inside the procedure body, ⌘↵ highlights only that `CREATE PROCEDURE …` (no `DELIMITER` lines) and runs it.
     - ⌘⇧↵ runs every statement. The Messages pane shows each run, and the explorer's Procedures folder shows `two_results_copy` after a refresh.
  5. `SELECT SLEEP(30)` → Cancel stops within about 1 s; `SELECT 1` then works. `SELECT * FROM big_table` pages with Load more.
     - Copy rows as INSERT: backtick names, `X'…'` bytes.
     - Export `SELECT a.id FROM big_table a CROSS JOIN (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5) x` (500,000 rows) to CSV: the file has 500,001 lines.
  6. Turn auto-commit off and `INSERT INTO job_log (message) VALUES ('t')`: the badge lights. Start an export and press Cancel: no "aborts the transaction" warning on MySQL. Repeat on PG: the warning is still there. Rollback clears the badge.
  7. ⌘⇧F formats with MySQL syntax (backticks kept). `SELECT * FROM big` completes `big_table`; `` `Order Items` `` is quoted on apply.
  8. Make a read-only copy of the profile: `INSERT …` is blocked with the read-only reason; `USE analytics` and `SET @x = 1` run.

- [ ] **Step 4: Commit**

```bash
git add src
git commit -m "feat(app): MySQL in the UI — capability-driven database picker and export-cancel warning, backend value filter, MySQL dialog labels, event and FK kinds"
```

---

### Task 11: Packaging with MySQL, bundle verification, CLAUDE.md

**Files:**
- Modify: `backend/dbaguy-backend.spec`
- Modify: `CLAUDE.md`
- Create (scratchpad only, not committed): a WebSocket probe script

**Interfaces:**
- Consumes: everything above.
- Produces: an app bundle whose frozen sidecar connects to MySQL with TLS, classifies MySQL through sqlglot, lists the tree and streams a query. The bundle contains no `_mysql_connector` C extension.

- [ ] **Step 1: Update the PyInstaller spec** — add to `backend/dbaguy-backend.spec`:

```python
# mysql-connector runs pure Python in DbaGuy (use_pure=True): leave its C extension and the parts that need other
# packages (Django, asyncio API, OpenTelemetry, Kerberos/LDAP/OCI/OpenID/WebAuthn plugins) out of the bundle.
_MYSQL_LEFT_OUT = (
    "mysql.connector.connection_cext", "mysql.connector.cursor_cext", "mysql.connector.aio", "mysql.connector.django",
    "mysql.connector.opentelemetry.instrumentation", "mysql.connector.plugins.authentication_kerberos_client",
    "mysql.connector.plugins.authentication_ldap_sasl_client", "mysql.connector.plugins.authentication_oci_client",
    "mysql.connector.plugins.authentication_openid_connect_client",
    "mysql.connector.plugins.authentication_webauthn_client",
)
```

Append to `hiddenimports`:
- `"sqlglot.dialects.mysql"`, `"sqlglot.parsers.mysql"`, `"sqlglot.generators.mysql"`, `"sqlglot.typing.mysql"`;
- `*collect_submodules("mysql.connector", filter=lambda name: not name.startswith(_MYSQL_LEFT_OUT))` (this keeps the locales, which are loaded with importlib, and the native/caching_sha2/sha256/clear-password auth plugins).

Set `excludes=["tkinter", "pytest", "PyInstaller", "_mysql_connector", *_MYSQL_LEFT_OUT]`.

- [ ] **Step 2: Build the sidecar and check its contents**

Run: `npm run build:backend && find src-tauri/resources/backend -name "_mysql_connector*" | wc -l && ls src-tauri/resources/backend/_internal | grep -c "^mysql$"`
Expected: `0`, then `1`.

- [ ] **Step 3: Probe the frozen sidecar against MySQL** (with the dev containers up). In shell 1:

```bash
cd src-tauri/resources/backend
DBAGUY_TOKEN=t DBAGUY_PORT=0 DBAGUY_LOG_TO_FILE=false DBAGUY_DATA_DIR="$(mktemp -d)" ./dbaguy-backend   # prints {"event":"ready","port":N}
```

In shell 2 (replace N):
1. `curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json" -X POST http://127.0.0.1:N/profiles/test -d '{"profile":{"engine":"mysql","name":"m","host":"127.0.0.1","port":33067,"database":"dbaguy","username":"dbaguy","ssl":{"mode":"require"}},"secrets":{"password":"dbaguy"}}'` → `"ok":true` with `MySQL 8.4` (pure driver, TLS and caching_sha2 in the bundle).
2. `curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json" -X POST http://127.0.0.1:N/profiles -d '{"profile":{"engine":"mysql","name":"m","host":"127.0.0.1","port":33067,"database":"dbaguy","username":"dbaguy","save_password":false,"ssl":{"mode":"disable"}},"secrets":{"password":"dbaguy"}}'` → note `profile.id` as P.
3. `curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json" -X POST http://127.0.0.1:N/sql/analyze -d '{"profile_id":"P","statements":["ALTER TABLE t DROP COLUMN a","SELECT * FROM t INTO OUTFILE \"/tmp/x\""]}'` → the first has `"dangerous_reasons":["alter_drop"]` (sqlglot's MySQL dialect is bundled), the second `"kind":"write"`.
4. `curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json" -X POST http://127.0.0.1:N/metadata/children -d '{"profile_id":"P","path":[]}'` → includes `"dbaguy"`.
5. `curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json" -X POST http://127.0.0.1:N/sessions -d '{"profile_id":"P"}'` → note `session_id` as S. Then save this as `<scratchpad>/ws_probe.py` and run `backend/.venv/bin/python <scratchpad>/ws_probe.py N t S`:

```python
import json
import sys

from websockets.sync.client import connect

port, token, session = sys.argv[1:4]
with connect(f"ws://127.0.0.1:{port}/sessions/{session}/ws?token={token}") as ws:
    ws.send(json.dumps({"type": "execute", "request_id": "q", "sql": "SELECT id, label FROM big_table ORDER BY id LIMIT 3"}))
    while True:
        event = json.loads(ws.recv())
        print(event["type"], event.get("rows") or "")
        if event["type"] == "finished":
            break
```

Expected: a `rows` event with `[[1, "row 1"], [2, "row 2"], [3, "row 3"]]`, then `finished`. Stop the sidecar with Ctrl-C.

- [ ] **Step 4: Build the app, check signatures and re-measure startup**

Run: `npm run build:app && codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok" && scripts/measure-startup.sh`
Expected: "signature ok", a cold-start time, an RSS total, and "backend exited with the app". Record the numbers for CLAUDE.md.

- [ ] **Step 5: Update `CLAUDE.md`** (keep its language and style):
  - **開發指令 → 測試 DB line:** add `MySQL 127.0.0.1:33067（dbaguy/dbaguy，root 同密碼，另有 analytics database）`. The bastion can reach `postgres:5432` and `mysql:3306`.
  - **Repo 結構 → adapters:** mention `adapters/mysql/` (settings, catalog/introspection/schema, types, adapter).
  - **架構重點:** add a MySQL bullet:
    - user SQL goes through `_VerbatimCursor` (never `cursor.execute()`, whose DELIMITER rewriter changes text) with `MULTI_STATEMENTS` off;
    - cancel and early close send `KILL QUERY <thread>` from a short side connection, keeping the transaction, and close drains later result sets;
    - `in_transaction` comes from the protocol flag, and `autocommit` is cached and recalibrated only after statements that may change it;
    - read-only means `SET SESSION TRANSACTION READ ONLY` plus the allow-list;
    - `DELIMITER` lines are consumed by the splitter;
    - the type table is in `adapters/mysql/types.py`.
  - **已知限制:**
    - the lexer assumes MySQL's default sql_mode (`ANSI_QUOTES` / `NO_BACKSLASH_ESCAPES` are not detected);
    - a cancel that lands just as a statement finishes is reported as cancelled;
    - a MySQL snapshot over 10,000 relations loads only the current database;
    - replace the startup and RSS numbers with the new measurement (noting that `enqueue=True` is gone and mysql-connector was added).
  - **進度 ③:** `進行中：03a 方言基礎 + MySQL 完成；03b SQL Server 待做`.

- [ ] **Step 6: Report what only a real server can verify.** For the user's final test list (spec §4), list in the task handoff:
  - TLS `verify-ca` against the company CA, and `verify-full` without a tunnel;
  - Cancel and early close through the company network and bastion;
  - Tree and snapshot time on the largest company database;
  - The read-only profile against a real account.

- [ ] **Step 7: Run the full suites again and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green.

```bash
git add backend/dbaguy-backend.spec CLAUDE.md
git commit -m "build: bundle the pure mysql-connector and sqlglot's MySQL dialect; verify the sidecar against MySQL; document 03a"
```

---

## Carries from sub-project ② (followup notes)

| Carry | Where |
|---|---|
| 02a: `quote_ident` hard-codes `"`; the always-quote expression is duplicated in `sql_text` | Task 3 (`_IDENT_QUOTES`, `force_quote_ident`; `quote_ident_sqlite`/`quote_ident_mysql` reuse it) |
| 02a: `$N` params gated on `dollar_quotes` | Task 1 (`dollar_params`) |
| 02a: `block_bodies` magic strings; tracker keywords hard-coded | Tasks 1–2 (`BodyRule` enum, one tracker per rule) |
| 02a: test gap "every Dialect present in every table"; backtick doubling | Task 3 tripwire test; MySQL quoting tests |
| 02a: `sql_text` re-exports `PG_RESERVED`, blanket `noqa` | Task 3 (explicit imports) |
| 02a: lexical fallback weaker than sqlglot | Task 4 (OUTFILE/DUMPFILE, `LOCK IN SHARE MODE`, `LOAD`/`HANDLER`, `EXPLAIN FORMAT`) — the rest stays deferred |
| 02a: SchemaCache key not normalised for non-per-database engines | Deliberately deferred (Ruling 29) |
| 02b / 02c: `filterForValue` is PG/SQLite only | Task 3 (`/sql/value-condition`) + Task 10 |
| 02c: sql-formatter mapping and splitting need MySQL | Task 1 (`LANGUAGES.mysql`, CodeMirror `MySQL`) + Task 2 |
| 02c: cap sqlglot input for huge INSERTs (⑥) | Not ③ — untouched |

## Rulings recorded while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| 1 | `set_message_listener` and its worker hook stay in 03b. | Spec §5 lists "message listener" under 03b, and only SQL Server uses it. Spec §5 binds over the coordinator's example task list. | 03b adds one default method and one worker change (small, FakeConnection-safe). |
| 2 | Only `server_read_only` is pre-added for 03b. `Dialect.TSQL`, `StatementSpan.repeat` and the `synonym` kind wait. | Adding `Dialect.TSQL` forces entries in every dialect table, and in the TS `Record`, at once, so it is not cheaper now. `repeat` needs API and run-loop wiring. | None: the tripwire test in Task 3 points 03b at every table. |
| 3 | `/*! … */` and `/*+ … */` are both `EXEC_COMMENT` tokens (spec), but only `/*!` forces `UNKNOWN`. | Optimizer hints cannot change what a statement does; making them UNKNOWN would block hinted SELECTs on read-only profiles. | A hint that alters behaviour would be classified READ (MySQL has none that write). |
| 4 | A table filter containing an executable comment is rejected. | The filter guard turns comments into spaces; for `/*!` that silently changes meaning, and keeping it would run hidden code. | Users must remove the comment from a filter. |
| 5 | The MySQL string literal doubles `'` and `\` (`'it''s \\ x'`), never `\'`. | The spec's `'it\\'s'` example is ambiguous as written. Doubling quotes is valid in every sql_mode, and doubling backslashes reads back exactly in the default mode. | Under `NO_BACKSLASH_ESCAPES`, a copied literal holds two backslashes per original one. |
| 6 | `DELIMITER` is a command only as the first word on its line before a statement starts. The terminator is the first non-space run; the rest of the line is ignored. | Matches the mysql client and spec §3.3, and keeps a column or alias named `delimiter` working. | A `DELIMITER` written mid-statement is sent to the server (syntax error, nothing runs). |
| 7 | Compound bodies open only on the first BEGIN of `CREATE [OR REPLACE] [DEFINER=…] [SQL SECURITY …] PROCEDURE/FUNCTION/TRIGGER/EVENT`, never right after a dot. | Spec §3.3; the dot guard covers `NEW.begin` in triggers. | A body-less routine using an unqualified column `begin` merges with following statements; the server rejects the merged text. |
| 8 | User SQL goes through a `MySQLCursorRaw` subclass that calls `cmd_query` directly. | `cursor.execute()` rewrites any text matching `delimiter\s+` outside quotes, comments included (found in the 26.7.0 source). | Depends on protected cursor members (`_handle_result`, `_reset_result`); a driver upgrade must re-check them (pinned `<27`). |
| 9 | MySQL errors: code = errno as text, message = server text + `SQLSTATE: x` line, position = `(N, 1)` from `at line N`. | Spec §3.5: errno + SQLSTATE + message; the errno is what MySQL users search for. | The chip shows `1064` where PG shows a SQLSTATE; purely cosmetic. |
| 10 | A statement counts as cancelled once KILL was sent. A failed KILL clears the flag. | Spec §2: `SLEEP` returns 1 rather than an error after KILL. | A statement finishing in the same millisecond is reported as cancelled although it completed. |
| 11 | `close()` of an unread result always sends KILL QUERY, then drains. If the KILL connection fails, it drains everything. | Spec §3.5; the transaction must survive. | Closing a huge result when the server refuses the side connection reads it in full (slow, logged as a WARNING). |
| 12 | `autocommit()` is cached and recalibrated with `SELECT @@session.autocommit` only after statements mentioning "autocommit" or starting with CALL/EXECUTE, and only when nothing is unread. | Spec §3.5 asks for no round trips while paused; the driver's `autocommit` property is a round trip, and the status flag is not exposed. | A procedure that switches autocommit, called via an unusual path, could leave the toolbar toggle stale until the next such statement. |
| 13 | MySQL warnings show as one message, "N warnings: run SHOW WARNINGS…", when a result set ends. | Non-strict truncation is silent data loss otherwise, and the count costs no round trip. | One extra message line for statements with warnings. |
| 14 | `TINYINT(1)` shows as a number, `BIT` as an int, `YEAR` as text (datetime category), ENUM/SET as text, GEOMETRY/VECTOR as bytes. | Text-first, lossless, matching spec §3.5's table; MySQL has no real boolean type. | Booleans show as `0/1`, not ✓/✗. |
| 15 | Profile options: reserved keys dropped (incl. `init_command`, `allow_local_infile`), values coerced (`true/false` → bool, digits → int). URL import keeps only keys mysql-connector knows. | The driver takes typed values, and `init_command` could undo READ ONLY. JDBC URLs carry Java-only parameters that would fail the connection. | An option that needs a list (e.g. `tls_versions`) cannot be given. |
| 16 | `verify-full` through an SSH tunnel fails before connecting. `require` is checked with `is_secure` after connecting. The CA is used only for `verify-*`. | mysql-connector verifies against the host it connects to (127.0.0.1); spec §3.5 says fail closed. The driver silently falls back to plain TCP for require. | Tunnelled users must pick `verify-ca`. |
| 17 | TCP keepalive is set on the driver's private socket (30/10/3), best effort. | Feature-priorities §7; the driver has no option for it. | A driver refactor could make it a no-op (logged at DEBUG). |
| 18 | Index and FK leaves get generated DDL. New kind `foreign_key` and folder `foreign_keys`. | Spec §3.5 lists a Foreign keys folder, and every leaf opens an object tab, but `SHOW CREATE` has no index/FK form. | Two more kinds for 03b to reuse or ignore. |
| 19 | The snapshot has `database=None` and covers all non-system databases. Over 10,000 relations (counted first) it covers only `DATABASE()`, or nothing without one. Lazy per-schema loading stays deferred. | Spec §3.5; `database=None` avoids "dbaguy · dbaguy" in quick open; MySQL 5.7 is out of scope. | Huge servers complete only the current database. |
| 20 | `cancel_aborts_transaction` is True only for PG; SQLite keeps False. | The flag drives the export-cancel warning, and an export is a SELECT, which SQLite's interrupt does not roll back. | SQLite users lose a warning that never applied to exports. |
| 21 | MySQL clause keywords drop RETURNING. `USE/FORCE/IGNORE/STRAIGHT_JOIN/PARTITION` stop alias reading for every dialect. | MySQL has no RETURNING; index hints were read as aliases. | A PG alias literally named `use` or `force` is no longer recognised. |
| 22 | `value_condition` reuses the copy/export cell-literal renderer. Complete bytes cells become filterable (`X'…'`). The menu item is always enabled, and a `null` answer shows a toast. | DRY with formatting; the grid cannot know whether a bytes preview is complete without duplicating that logic. | One click to learn that a cut value cannot be filtered. |
| 23 | The dependency is pinned `mysql-connector-python>=26.7.0,<27`. Poetry installs the platform wheel; the bundle excludes the C extension. | Mirrors the psycopg pin style; spec §2 chose pure mode. | The dev venv carries the unused C extension (~20 MB). |
| 24 | Streaming speed is measured in Task 9; over 3× PG, the implementer stops and reports instead of switching. | The C extension changes the cursor classes (`_VerbatimCursor`) and packaging, which needs a plan revision. | A pause in execution if pure mode is too slow. |
| 25 | The lexer assumes MySQL's default sql_mode. | Detecting `ANSI_QUOTES`/`NO_BACKSLASH_ESCAPES` per session costs a round trip and makes the pure `sql/` layer session-aware. | On such servers the splitter and guards can disagree with the server about strings (documented in CLAUDE.md). |
| 26 | Seed objects use DEFINER `dbaguy`@`%`; the event is scheduled for 2030. | The dev account can then read every definition, and the event never deletes anything. | None (dev only). |
| 27 | `EXPLAIN [ANALYZE] FORMAT=…` is unwrapped like PG's EXPLAIN options. | Otherwise `EXPLAIN ANALYZE FORMAT=TREE DELETE …` was UNKNOWN and skipped the DELETE confirmation on the standard level. | None. |
| 28 | `LOAD` and `HANDLER` are WRITE only in MySQL (dialect overlay). The OUTFILE/DUMPFILE rule is dialect-agnostic. | PG's `LOAD 'lib'` keeps its meaning; INTO OUTFILE exists only in MySQL anyway. | None. |
| 29 | The SchemaCache key normalisation (02a minor) is not done. | MySQL query and table tabs never send a database (no picker), so one key is used. | Duplicate snapshot builds if some path later sends a database for MySQL. |
| 30 | `server_info` labels MariaDB servers "MariaDB …". | Spec: MariaDB works where compatible; the label avoids "MySQL 10.11". | None. |
| 31 | The SSL tab shows MySQL names next to the shared modes; no per-engine profile fields. | `SslConfig` stays one model (spec §3.5 maps modes); one dialog for every engine. | Users read two names per mode. |
| 32 | Cursors are constructed directly (`_VerbatimCursor(conn)`, `MySQLCursorBuffered(conn)`), never through `conn.cursor()`. | `conn.cursor()` sends a PING first: a round trip per statement, which doubles latency over a tunnel (found in the 26.7.0 source). | Same protected-API dependency as Ruling 8. |

## Spec issues found while planning

- §3.2 shows the MySQL literal as `'it\\'s'`, which as written closes the string after an escaped backslash; resolved by Ruling 5.
- §3.4 makes every `EXEC_COMMENT` statement UNKNOWN, including `/*+ … */` hints, which would block harmless hinted SELECTs on read-only profiles; narrowed by Ruling 3.
- §3.5 says `require` = "must be TLS, unverified", which the driver cannot express (it falls back to plain); enforced after connect (Ruling 16).
- §3.5 lists a "Foreign keys" folder while §3.5 and §3.8 only add `event`/`EVENTS`; `foreign_key`/`FOREIGN_KEYS` were added (Ruling 18).
- §5 puts the message listener in 03b, while §3.1 describes it with the other contract changes; §5 was followed (Ruling 1).
- The feature-priorities §6 advice (lazy per-schema snapshots for MySQL 5.7) conflicts with spec §3.5's eager snapshot; the spec wins because MySQL < 8 is out of scope (Ruling 19).
