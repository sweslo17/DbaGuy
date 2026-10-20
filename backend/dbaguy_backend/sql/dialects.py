import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..domain.dialect import Dialect  # the key of every rule table in sql/; imported from here by the sql modules


class BodyRule(StrEnum):
    """Which statements hold a body whose inner `;` does not end the statement (see splitter)."""

    BEGIN_ATOMIC = "begin_atomic"  # PostgreSQL: CREATE [OR REPLACE] FUNCTION|PROCEDURE … BEGIN ATOMIC … END
    TRIGGER = "trigger"  # SQLite: CREATE [TEMP|TEMPORARY] TRIGGER … BEGIN … END
    COMPOUND = "compound"  # MySQL: CREATE … PROCEDURE|FUNCTION|TRIGGER|EVENT … BEGIN … END, nested blocks
    # T-SQL: a batch that CREATEs/ALTERs a procedure, function, trigger or view; the rest of the batch is its body
    # (has_body). ";" never splits T-SQL (GO lines do: splitter)
    TSQL_MODULE = "tsql_module"
    # SQL++: no statement holds a body (JS UDF bodies are strings, inline ones {expr}); ";" always ends one
    NONE = "none"


class ClientCommand(StrEnum):
    """A line the client consumes itself: the server never sees it."""

    DELIMITER = "delimiter"  # MySQL: `DELIMITER $$` changes the statement terminator
    GO = "go"  # T-SQL (sqlcmd, SSMS): a "GO [n]" line ends a batch and runs it n times


@dataclass(frozen=True)
class LexRules:
    nested_block_comments: bool
    line_comment_end: re.Pattern[str]  # ends a `--` or `#` comment
    dash_comment_needs_space: bool  # MySQL: `--` starts a comment only before whitespace, a control char or the end
    hash_comments: bool  # MySQL: `#` starts a line comment
    executable_comments: bool  # MySQL: `/*! … */`, `/*+ … */` and MariaDB `/*M! … */` are code (EXEC_COMMENT)
    dollar_quotes: bool  # PostgreSQL $tag$…$tag$ strings
    dollar_params: bool  # PostgreSQL $1 parameters
    escape_string_prefixes: frozenset[str]  # PostgreSQL E'…': backslash escapes in that string only
    backslash_escapes: bool  # MySQL (default sql_mode): backslash escapes in every string
    string_quotes: frozenset[str]  # characters that open a string
    identifier_quotes: tuple[tuple[str, str], ...]  # (open, close)
    variable_prefixes: frozenset[str]  # start a variable word: MySQL "@" (@x, @@x); T-SQL "@" and "#" (#temp, ##global)
    body_rule: BodyRule
    client_command: ClientCommand | None = None
    national_strings: bool = False  # T-SQL: N'…' is one string
    # T-SQL: every Unicode space ends a word, as in SQL Server (probed on 2022: str.isspace() and U+200B), so
    # `DELETE\u00a0FROM t` is a DELETE. Elsewhere a non-ASCII character continues a name, as in those servers.
    unicode_spaces: bool = False
    identifier_backslash_escapes: bool = False  # SQL++: `\`` and `\\` inside back-ticks
    named_param_prefixes: frozenset[str] = frozenset()  # SQL++ "$" and "@": `$name`, `@name`, `$1` are one PARAM token
    question_params: bool = False  # SQL++: `?` is a positional PARAM


RULES: dict[Dialect, LexRules] = {
    Dialect.POSTGRES: LexRules(
        nested_block_comments=True, line_comment_end=re.compile(r"[\n\r]"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=True, dollar_params=True,
        escape_string_prefixes=frozenset({"e", "E"}), backslash_escapes=False, string_quotes=frozenset("'"),
        identifier_quotes=(('"', '"'),), variable_prefixes=frozenset(), body_rule=BodyRule.BEGIN_ATOMIC,
    ),
    Dialect.SQLITE: LexRules(
        nested_block_comments=False, line_comment_end=re.compile(r"\n"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=False, string_quotes=frozenset("'"),
        identifier_quotes=(('"', '"'), ("[", "]"), ("`", "`")), variable_prefixes=frozenset(),
        body_rule=BodyRule.TRIGGER,
    ),
    Dialect.MYSQL: LexRules(
        nested_block_comments=False, line_comment_end=re.compile(r"\n"), dash_comment_needs_space=True,
        hash_comments=True, executable_comments=True, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=True, string_quotes=frozenset("'\""),
        identifier_quotes=(("`", "`"),), variable_prefixes=frozenset("@"), body_rule=BodyRule.COMPOUND,
        client_command=ClientCommand.DELIMITER,
    ),
    Dialect.TSQL: LexRules(
        nested_block_comments=True, line_comment_end=re.compile(r"[\n\r]"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=False, string_quotes=frozenset("'"),
        # `"` names an identifier: DbaGuy connects with QUOTED_IDENTIFIER ON (Task 6, Ruling 3)
        identifier_quotes=(("[", "]"), ('"', '"')), variable_prefixes=frozenset("@#"),
        body_rule=BodyRule.TSQL_MODULE, client_command=ClientCommand.GO, national_strings=True, unicode_spaces=True,
    ),
    Dialect.SQLPP: LexRules(  # Couchbase SQL++ 7.6 (survey §8.1; spec §3.3.1)
        nested_block_comments=False, line_comment_end=re.compile(r"[\n\r]"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=True, string_quotes=frozenset("'\""),  # "x" is a string
        identifier_quotes=(("`", "`"),), variable_prefixes=frozenset(), body_rule=BodyRule.NONE,
        unicode_spaces=False,  # A7 (Task 1): the server refuses U+00A0, U+2003, U+3000, U+200B and U+FEFF as spaces
        identifier_backslash_escapes=True, named_param_prefixes=frozenset("$@"), question_params=True,
    ),
}

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

# SQL Server reserved keywords: an extra one only costs a pair of brackets. `go` is not one, but a name that lands
# alone on a line would act as the batch separator, so it is always bracketed.
TSQL_RESERVED = frozenset(
    """add all alter and any as asc authorization backup begin between break browse bulk by cascade case check
    checkpoint close clustered coalesce collate column commit compute constraint contains containstable continue
    convert create cross current current_date current_time current_timestamp current_user cursor database dbcc
    deallocate declare default delete deny desc disk distinct distributed double drop dump else end errlvl escape
    except exec execute exists exit external fetch file fillfactor for foreign freetext freetexttable from full function
    go goto grant group having holdlock identity identity_insert identitycol if in index inner insert intersect into is
    join key kill left like lineno load merge national nocheck nonclustered not null nullif of off offsets on open
    opendatasource openquery openrowset openxml option or order outer over percent pivot plan precision primary print
    proc procedure public raiserror read readtext reconfigure references replication restore restrict return revert
    revoke right rollback rowcount rowguidcol rule save schema securityaudit select semantickeyphrasetable
    semanticsimilaritydetailstable semanticsimilaritytable session_user set setuser shutdown some statistics
    system_user table tablesample textsize then to top tran transaction trigger truncate try_convert tsequal union
    unique unpivot update updatetext use user values varying view waitfor when where while with within writetext""".split()
)

SQLPP_RESERVED = frozenset(  # Couchbase SQL++ 7.6 reserved words (225), case-insensitive
    """_index_condition _index_key advise all alter analyze and any array as asc at begin between binary boolean break
    bucket build by cache call case cast cluster collate collection commit committed connect continue correlated cover
    create current cycle database dataset datastore declare decrement default delete derived desc describe distinct do
    drop each element else end escape every except exclude execute exists explain false fetch filter first flatten
    flatten_keys flush following for force from fts function golang grant group groups gsi hash having if ignore ilike
    in include increment index infer inline inner insert intersect into is isolation javascript join key keys keyspace
    known language last lateral left let letting level like limit lsm map mapping matched materialized maxvalue merge
    minvalue missing namespace nest next nextval nl no not nth_value null nulls number object offset on option options
    or order others outer over parse partition password path pool preceding prepare prev previous prevval primary
    private privilege probe procedure public range raw read realm recursive reduce rename replace respect restart
    restrict return returning revoke right role rollback row rows satisfies savepoint schema scope select self sequence
    set show some start statistics string system then ties to tran transaction trigger true truncate unbounded under
    union unique unknown unnest unset update upsert use user users using validate value valued values vector via view
    when where while window with within work xor""".split()
)

_SIMPLE_IDENT = {
    Dialect.POSTGRES: re.compile(r"[a-z_][a-z0-9_$]*"),  # unquoted PG identifiers fold to lower case
    Dialect.SQLITE: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),  # SQLite identifiers are case-insensitive
    Dialect.MYSQL: re.compile(r"[A-Za-z_][A-Za-z0-9_$]*"),  # unquoted MySQL names keep their case
    Dialect.TSQL: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),
    Dialect.SQLPP: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),  # ASCII only (survey §8.1); names keep their case
}
_RESERVED = {
    Dialect.POSTGRES: PG_RESERVED, Dialect.SQLITE: SQLITE_RESERVED, Dialect.MYSQL: MYSQL_RESERVED,
    Dialect.TSQL: TSQL_RESERVED, Dialect.SQLPP: SQLPP_RESERVED,
}
_IDENT_QUOTES: dict[Dialect, tuple[str, str]] = {
    Dialect.POSTGRES: ('"', '"'),
    Dialect.SQLITE: ('"', '"'),
    Dialect.MYSQL: ("`", "`"),  # `"` opens a string in MySQL's default sql_mode
    Dialect.TSQL: ("[", "]"),
    Dialect.SQLPP: ("`", "`"),
}
_BOOL_LITERALS: dict[Dialect, tuple[str, str]] = {
    Dialect.POSTGRES: ("TRUE", "FALSE"), Dialect.SQLITE: ("1", "0"), Dialect.MYSQL: ("TRUE", "FALSE"),
    Dialect.TSQL: ("1", "0"), Dialect.SQLPP: ("TRUE", "FALSE"),
}
_BYTES_LITERALS: dict[Dialect, str | None] = {  # {} is the hex; None: the dialect has no binary literal
    Dialect.POSTGRES: "'\\x{}'::bytea", Dialect.SQLITE: "X'{}'", Dialect.MYSQL: "X'{}'", Dialect.TSQL: "0x{}",
    Dialect.SQLPP: None,
}
_JSON_LITERALS: dict[Dialect, str | None] = {  # {} is a string literal holding the JSON text; None: the text itself
    Dialect.POSTGRES: "{}", Dialect.SQLITE: "{}",
    # A JSON column compares a plain string as a JSON string scalar, so `doc = '{"a": 1}'` would match nothing.
    Dialect.MYSQL: "CAST({} AS JSON)",
    Dialect.TSQL: "{}",  # JSON is NVARCHAR text in SQL Server
    Dialect.SQLPP: None,  # JSON is SQL++ syntax: a document is written as itself
}
# MySQL keeps dates with a zero year, month or day (`0000-00-00`, `2026-00-15`) that its default sql_mode refuses as
# literals (1525), so such a value is compared with the column's text instead; other dialects have no zero dates.
_ZERO_DATE_AS_TEXT: dict[Dialect, str | None] = {
    Dialect.POSTGRES: None, Dialect.SQLITE: None, Dialect.MYSQL: "CAST({} AS CHAR)", Dialect.TSQL: None,
    Dialect.SQLPP: None,
}
_ZERO_DATE = re.compile(r"(?:0000-\d\d-\d\d|\d{4}-00-\d\d|\d{4}-\d\d-00)(?:$|[ T])", re.ASCII)
_STRING_PREFIXES: dict[Dialect, str] = {  # T-SQL N'…': Unicode, so text outside the column's code page survives
    Dialect.POSTGRES: "", Dialect.SQLITE: "", Dialect.MYSQL: "", Dialect.TSQL: "N", Dialect.SQLPP: "",
}
_ISO_DATETIME: dict[Dialect, bool] = {  # T-SQL: write date and time with a "T", the form no login language misreads
    Dialect.POSTGRES: False, Dialect.SQLITE: False, Dialect.MYSQL: False, Dialect.TSQL: True, Dialect.SQLPP: False,
}
# What stands for a NUL character inside a quoted literal, so the SQL never carries a raw NUL: drivers cut a C string
# there (pymssql: error 105), and the worker refuses such text for every engine (invalid_text). T-SQL: MAX keeps the
# concatenation from being capped at 4000 characters, which would silently truncate a long value. MySQL: the `\0`
# escape of the default sql_mode (which the lexer assumes too). PostgreSQL: None, its text cannot hold a NUL at all.
# SQL++: None, a JSON string writes it as `\u0000`.
_NUL_IN_LITERAL: dict[Dialect, str | None] = {
    Dialect.POSTGRES: None, Dialect.SQLITE: "' || char(0) || '", Dialect.MYSQL: "\\0",
    Dialect.TSQL: "' + CAST(NCHAR(0) AS NVARCHAR(MAX)) + N'", Dialect.SQLPP: None,
}
# SQL++ string literals are JSON strings (double quotes, JSON escapes; D-04-15); the other dialects quote with ''.
_JSON_STRINGS: dict[Dialect, bool] = {
    Dialect.POSTGRES: False, Dialect.SQLITE: False, Dialect.MYSQL: False, Dialect.TSQL: False, Dialect.SQLPP: True,
}
_IN_LIST_BRACKETS: dict[Dialect, tuple[str, str]] = {  # SQL++: `x IN [1, 2]` (an array; A19)
    Dialect.POSTGRES: ("(", ")"), Dialect.SQLITE: ("(", ")"), Dialect.MYSQL: ("(", ")"), Dialect.TSQL: ("(", ")"),
    Dialect.SQLPP: ("[", "]"),
}
# No sqlglot dialect exists for these: they are classified by keyword rules only (sqlglot_rules has no entry).
KEYWORD_ONLY_DIALECTS: frozenset[Dialect] = frozenset({Dialect.SQLPP})
# Unquoted identifiers keep their case: a formatter may not change it (format_guard).
CASE_SENSITIVE_IDENTIFIERS: frozenset[Dialect] = frozenset({Dialect.SQLPP})
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DATE_SPACE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", re.ASCII)
NUMERIC_LITERAL = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)  # text that can be written as a bare number


def is_reserved(dialect: Dialect, word: str) -> bool:
    return word.lower() in _RESERVED[dialect]


def needs_quote(dialect: Dialect, name: str) -> bool:
    return not _SIMPLE_IDENT[dialect].fullmatch(name) or is_reserved(dialect, name)


def force_quote_ident(dialect: Dialect, name: str) -> str:
    """Always quoted, the closing quote doubled inside (SQL++ also doubles backslashes, which escape in back-ticks):
    for names interpolated into introspection SQL."""
    opening, closing = _IDENT_QUOTES[dialect]
    if RULES[dialect].identifier_backslash_escapes:
        name = name.replace("\\", "\\\\")
    return opening + name.replace(closing, closing * 2) + closing


def quote_ident(dialect: Dialect, name: str) -> str:
    return force_quote_ident(dialect, name) if needs_quote(dialect, name) else name


def qualified(dialect: Dialect, schema: str | None, name: str) -> str:
    return f"{quote_ident(dialect, schema)}.{quote_ident(dialect, name)}" if schema else quote_ident(dialect, name)


def keyspace_path(bucket: str, scope: str | None = None, collection: str | None = None) -> str:
    """A SQL++ keyspace path, `bucket[.scope[.collection]]`, each part quoted when needed."""
    return ".".join(quote_ident(Dialect.SQLPP, part) for part in (bucket, scope, collection) if part is not None)


def in_list(dialect: Dialect, literals: Iterable[str]) -> str:
    """Literals (already written for the dialect) as the right-hand side of IN: `(1, 2)`, in SQL++ `[1, 2]`."""
    opening, closing = _IN_LIST_BRACKETS[dialect]
    return opening + ", ".join(literals) + closing


def quote_literal(
    dialect: Dialect, value: str, *, national: bool = True, backslash_escapes: bool | None = None
) -> str:
    """A string literal that the dialect reads back as exactly `value`: quotes are doubled, and where backslash
    escapes exist (MySQL's default sql_mode), backslashes are doubled too. T-SQL literals carry the N prefix. A NUL
    character never stays raw (`_NUL_IN_LITERAL`): an escape, or a concatenation that keeps the text one expression.
    SQL++: the value as a JSON string (`json.dumps`), which is also how the server reads a "…" literal (D-04-15).

    `national` False leaves T-SQL's N out (ASCII text for a non-Unicode column keeps its index seek, ⑤ D-05-2, A8).
    `backslash_escapes` overrides the dialect's rule (False: a MySQL session with NO_BACKSLASH_ESCAPES, ⑤ D-05-13),
    where MySQL's `\\0` cannot write a NUL: ValueError then."""
    if _JSON_STRINGS[dialect]:
        return json.dumps(value, ensure_ascii=False)
    escapes = RULES[dialect].backslash_escapes if backslash_escapes is None else backslash_escapes
    if "\x00" in value and dialect is Dialect.MYSQL and not escapes:
        raise ValueError("A NUL character cannot be written in a MySQL literal without backslash escapes")
    if escapes:
        value = value.replace("\\", "\\\\")
    literal = (_STRING_PREFIXES[dialect] if national else "") + "'" + value.replace("'", "''") + "'"
    nul = _NUL_IN_LITERAL[dialect]
    return literal if nul is None else literal.replace("\x00", nul)


def datetime_literal(dialect: Dialect, text: str, *, backslash_escapes: bool | None = None) -> str:
    """A date/time value given as the grid's text. SQL Server's datetime and smalldatetime read `2026-10-05` and
    `2026-10-05 12:34:56` by the login's DATEFORMAT (a dmy language such as British stores 2026-05-10, or fails with
    error 242) but `20261005` and `2026-10-05T12:34:56` the same everywhere, so T-SQL text is stripped, a date alone is
    written without its dashes and a date and time are joined by a `T`. A time the server cannot read that way (a
    one-digit hour) still goes with the `T`: the server refuses it rather than misreading it."""
    if _ISO_DATETIME[dialect]:
        text = text.strip()
        if _DATE_ONLY.fullmatch(text):
            text = text.replace("-", "")
        elif found := _DATE_SPACE_TIME.match(text):
            text = f"{found.group(1)}T{found.group(2)}{text[found.end():]}"
    return quote_literal(dialect, text, backslash_escapes=backslash_escapes)


def bool_literal(dialect: Dialect, value: bool) -> str:
    return _BOOL_LITERALS[dialect][0 if value else 1]


def bytes_literal(dialect: Dialect, hex_digits: str) -> str:
    """Binary data given as hex digits. Raises ValueError for a dialect without a binary literal (SQL++)."""
    template = _BYTES_LITERALS[dialect]
    if template is None:
        raise ValueError(f"{dialect} has no binary literal")
    return template.format(hex_digits)


def json_literal(dialect: Dialect, text: str, *, national: bool = True, backslash_escapes: bool | None = None) -> str:
    """A JSON document given as its text, written so that the dialect compares it as JSON (SQL++: the text itself,
    JSON being SQL++ syntax)."""
    template = _JSON_LITERALS[dialect]
    if template is None:
        return text
    return template.format(quote_literal(dialect, text, national=national, backslash_escapes=backslash_escapes))


def zero_date_as_text(dialect: Dialect, column_sql: str, value: str) -> str | None:
    """`column_sql` as text when `value` is a date with a zero part the dialect cannot write as a literal; None when
    the plain comparison works (and keeps using the column's index)."""
    template = _ZERO_DATE_AS_TEXT[dialect]
    return template.format(column_sql) if template is not None and _ZERO_DATE.match(value) else None


@dataclass(frozen=True)
class TransactionFrame:
    """The texts around a grid commit (⑤ D-05-4): sent through run() like its statements, so they are in the preview."""

    begin: str
    commit: str


_TRANSACTION_FRAMES: dict[Dialect, TransactionFrame | None] = {
    Dialect.POSTGRES: TransactionFrame("BEGIN", "COMMIT"),
    Dialect.MYSQL: TransactionFrame("START TRANSACTION", "COMMIT"),
    # NOCOUNT OFF: on a server whose `user options` turn NOCOUNT on, no statement would report its rows (A2)
    Dialect.TSQL: TransactionFrame("SET NOCOUNT OFF;\nBEGIN TRANSACTION", "COMMIT TRANSACTION"),
    # IMMEDIATE: the write lock first, so a database another connection is writing fails before the first statement
    Dialect.SQLITE: TransactionFrame("BEGIN IMMEDIATE", "COMMIT"),
    Dialect.SQLPP: None,  # no grid editing (R-05-12)
}
_ALL_DEFAULTS: dict[Dialect, str | None] = {  # an INSERT of a row whose every column takes its default
    Dialect.POSTGRES: "DEFAULT VALUES", Dialect.SQLITE: "DEFAULT VALUES", Dialect.TSQL: "DEFAULT VALUES",
    Dialect.MYSQL: "() VALUES ()", Dialect.SQLPP: None,
}
# The verbs of a grid commit's UPDATE and INSERT. SQLite applies a constraint's own conflict clause from CREATE TABLE
# (`UNIQUE … ON CONFLICT REPLACE` deletes the other row that holds the value, IGNORE skips the statement) unless the
# statement names one, and still reports one row changed; `OR ABORT` overrides it, and ABORT is SQLite's default, so
# every other table behaves exactly as without it (05a final review I1). DELETE has no conflict clause.
_UPDATE_VERBS: dict[Dialect, str] = {Dialect.SQLITE: "UPDATE OR ABORT"}
_INSERT_VERBS: dict[Dialect, str] = {Dialect.SQLITE: "INSERT OR ABORT INTO"}


def transaction_frame(dialect: Dialect) -> TransactionFrame:
    frame = _TRANSACTION_FRAMES[dialect]
    if frame is None:
        raise ValueError(f"{dialect} has no grid editing")
    return frame


def update_head(dialect: Dialect, table_sql: str) -> str:
    """A grid commit's `UPDATE <table>` (SQLite: `UPDATE OR ABORT <table>`)."""
    return f"{_UPDATE_VERBS.get(dialect, 'UPDATE')} {table_sql}"


def insert_head(dialect: Dialect, table_sql: str) -> str:
    """A grid commit's `INSERT INTO <table>` (SQLite: `INSERT OR ABORT INTO <table>`)."""
    return f"{_INSERT_VERBS.get(dialect, 'INSERT INTO')} {table_sql}"


def insert_defaults(dialect: Dialect, table_sql: str) -> str:
    rest = _ALL_DEFAULTS[dialect]
    if rest is None:
        raise ValueError(f"{dialect} has no grid editing")
    return f"{insert_head(dialect, table_sql)} {rest}"


def script_text(dialect: Dialect, statements: Sequence[str]) -> str:
    """Statements as one text to copy or keep in History: `;` after each, or a `GO` line after each T-SQL batch."""
    if RULES[dialect].client_command is ClientCommand.GO:
        return "".join(f"{statement}\nGO\n" for statement in statements).rstrip("\n")
    return "\n".join(f"{statement};" for statement in statements)
