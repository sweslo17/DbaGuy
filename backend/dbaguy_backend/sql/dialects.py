import json
import math
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
    # How a number literal ends; None: the lexer's own (digits, a fraction, an exponent with its digits). T-SQL's
    # differs, and a token boundary SQL Server draws elsewhere than DbaGuy hides a keyword (Task 9 fix round 3)
    number: re.Pattern[str] | None = None
    # T-SQL: `$1.5` is one money literal (no exponent: `$1e` is `$1` and a name), `$action` / `$IDENTITY` one name
    dollar_money_and_names: bool = False


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
        # As SQL Server 2022 reads them (probed, Task 9 fix round 3): `0x` and any run of hex digits, none included, is
        # one binary literal (`0x1TRUNCATE` is 0x1 then TRUNCATE; `0xg` is 0x then the name g); an exponent marker
        # belongs to its number with or without a sign and digits (`1e`, `1.e`, `.5e`, `1e+`, `1E-`: `1eTRUNCATE` is
        # 1e then TRUNCATE, `1else` 1e then lse). There are no suffixes: `1d`, `1f`, `1m`, `0b1`, `1_0` are a number
        # and a name, which the lexer's own rule already says.
        number=re.compile(r"0[xX][0-9a-fA-F]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d*)?"),
        dollar_money_and_names=True,
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
# T-SQL reads a backslash right before a line break inside a string literal as a line continuation and drops it (SQL
# Server 2022, N'…' and '…' alike; every character after a backslash was tried, U+0001–U+FFFF with the N and 1–127
# without, and only LF and CR do anything): LF and CRLF go with it (`N'a\<LF>b'` is `ab`), a lone CR stays and the
# backslash goes (`N'a\<CR>b'` is `a<CR>b`). Only the backslash right next to the break counts (`\\<LF>` loses one
# backslash and the break), and a backslash last in the literal is kept. So the literal ends after that backslash and
# the text goes on in the next one: `CONCAT(<an empty MAX value>, N'a\', N'<LF>b')`. The MAX value keeps the result from
# being capped at 4000 characters (8000 without the N), as in the NUL form above. The pair is (Unicode, not): the empty
# value has the literal's own type, so a non-Unicode literal stays varchar and keeps its index seek where ⑤ relies on
# that (A8: on a SQL collation N'…' turns a varchar key's seek into a scan, and `VARCHAR(MAX)` does not; plan checked).
# CONCAT, not a chain of `+`: a chain is as deep as it is long and the server gives up at 1,000–2,000 breaks in one
# value (8631, stack limit); CONCAT takes 254 arguments and is nested for more, so the limit becomes the expression
# size of 65,535 constants (8632), about 32,000 breaks in one value (checked). Both are errors, never a changed value.
# The other dialects read a backslash and a line break as they are (MySQL has the backslash doubled already): None.
_LINE_CONTINUATION_GUARD: dict[Dialect, tuple[str, str] | None] = {
    Dialect.POSTGRES: None, Dialect.SQLITE: None, Dialect.MYSQL: None, Dialect.SQLPP: None,
    Dialect.TSQL: ("CAST(N'' AS NVARCHAR(MAX))", "CAST('' AS VARCHAR(MAX))"),
}
_AFTER_BACKSLASH_BEFORE_BREAK = re.compile(r"(?<=\\)(?=[\r\n])")  # the place between them
_CONCAT_ARGUMENTS = 253  # CONCAT takes 254 arguments: the empty MAX value and these
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
# A space before a time's offset (`12:34:56 +08:00`, as SSMS and CAST write a datetimeoffset): with the `T`, SQL Server
# refuses it (241, ⑥ A15), so the two are joined
_SPACE_BEFORE_OFFSET = re.compile(r"(?<=\d)\s+(?=[+-]\d{2}:\d{2}$)", re.ASCII)
# What SQLite's REAL writes for the values a number literal cannot (⑥ R-06-11, L3): 9e999 is read as infinity, while
# 'inf' would be stored as text; NaN is stored as NULL anyway. The keys are Python's text of the float (repr).
# PostgreSQL reads the quoted text ('inf', 'nan'); MySQL and SQL Server floats hold none of them (⑥ S3).
_NON_FINITE: dict[Dialect, dict[str, str]] = {Dialect.SQLITE: {"inf": "9e999", "-inf": "-9e999", "nan": "NULL"}}
# −0.0: a number literal without an exponent is an exact numeric, whose zero has no sign. MySQL reads one with an
# exponent as a DOUBLE (06a Task 4); PostgreSQL's float input reads the quoted text (_QUOTED_NEGATIVE_ZERO)
_NEGATIVE_ZERO: dict[Dialect, str] = {Dialect.MYSQL: "-0.0e0"}
_QUOTED_NEGATIVE_ZERO: frozenset[Dialect] = frozenset({Dialect.POSTGRES})
# Writing an identity column's own values (⑥ D-06-5): PostgreSQL's GENERATED ALWAYS needs OVERRIDING SYSTEM VALUE in
# the INSERT (P1); SQL Server's IDENTITY needs IDENTITY_INSERT on, one table at a time (A S2)
_OVERRIDING: dict[Dialect, str] = {Dialect.POSTGRES: "OVERRIDING SYSTEM VALUE"}
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
    character never stays raw (`_NUL_IN_LITERAL`): an escape, or a concatenation that keeps the text one expression;
    nor does a T-SQL backslash right before a line break (`_LINE_CONTINUATION_GUARD`), which the server would read as a
    line continuation: such a text is a CONCAT of literals, so it is an expression, not a constant.
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
    if (empty := _LINE_CONTINUATION_GUARD[dialect]) is None or "\\" not in value:  # nearly every value: no scan
        return _quoted(dialect, value, national)
    pieces = [_quoted(dialect, part, national) for part in _AFTER_BACKSLASH_BEFORE_BREAK.split(value)]
    return pieces[0] if len(pieces) == 1 else _concatenated(empty[0] if national else empty[1], pieces)


def _quoted(dialect: Dialect, text: str, national: bool) -> str:
    """One quoted literal: the N prefix where it is wanted, quotes doubled, a NUL kept out."""
    literal = (_STRING_PREFIXES[dialect] if national else "") + "'" + text.replace("'", "''") + "'"
    nul = _NUL_IN_LITERAL[dialect]
    return literal if nul is None else literal.replace("\x00", nul)


def _concatenated(empty_max: str, pieces: list[str]) -> str:
    """T-SQL expressions as one: `CONCAT(<empty MAX value>, a, b, …)`, nested when there are more than CONCAT takes.
    Every level starts with the MAX value, so no level is capped."""
    while True:
        pieces = [f"CONCAT({empty_max}, {', '.join(pieces[i:i + _CONCAT_ARGUMENTS])})"
                  for i in range(0, len(pieces), _CONCAT_ARGUMENTS)]
        if len(pieces) == 1:
            return pieces[0]


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
            text = _SPACE_BEFORE_OFFSET.sub("", f"{found.group(1)}T{found.group(2)}{text[found.end():]}")
    return quote_literal(dialect, text, backslash_escapes=backslash_escapes)


def non_finite_literal(dialect: Dialect, text: str) -> str | None:
    """How the dialect writes Python's `inf`, `-inf` or `nan` as a number; None: as quoted text (PostgreSQL reads
    'inf' and 'nan' itself; MySQL and SQL Server cannot hold them)."""
    return _NON_FINITE.get(dialect, {}).get(text)


def float_literal(dialect: Dialect, value: float) -> str | None:
    """A float as a number literal that reads back as the same value: Python's repr, the shortest text that does
    (`0.1`, `1e+16`); SQLite's infinities (non_finite_literal); −0.0 with its sign where a plain `-0.0` loses it.
    None: the value is written as quoted text instead (PostgreSQL's 'inf', 'nan' and '-0.0', which its float input
    reads)."""
    text = repr(value)
    if not math.isfinite(value):
        return non_finite_literal(dialect, text)
    if value == 0 and math.copysign(1.0, value) < 0:
        return None if dialect in _QUOTED_NEGATIVE_ZERO else _NEGATIVE_ZERO.get(dialect, text)
    return text


def overriding_identity(dialect: Dialect) -> str | None:
    """What an INSERT that writes a GENERATED ALWAYS identity's values says after its column list."""
    return _OVERRIDING.get(dialect)


def identity_insert(dialect: Dialect, table_sql: str) -> tuple[str, str] | None:
    """The statements around writing an IDENTITY column's values (SQL Server); None where no switch exists."""
    if dialect is not Dialect.TSQL:
        return None
    return f"SET IDENTITY_INSERT {table_sql} ON", f"SET IDENTITY_INSERT {table_sql} OFF"


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


# The heads the explain tool puts in front of an agent's statement (spec ⑧ D-08-10), each as words the lexer reads one
# by one (`=` is a token of its own), the first the one used: MySQL's tree format is 8.0.16's, so an older server or
# MariaDB gets the plain head. T-SQL has none: its plan comes from SET SHOWPLAN_TEXT around the statement.
EXPLAIN_HEADS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("EXPLAIN",),
    Dialect.MYSQL: ("EXPLAIN FORMAT = TREE", "EXPLAIN"),
    Dialect.SQLITE: ("EXPLAIN QUERY PLAN",),
    Dialect.TSQL: (),
    Dialect.SQLPP: ("EXPLAIN",),
}


@dataclass(frozen=True)
class SessionAssumption:
    """A setting the lexer (and so the agent gate) reads the dialect's statements by: the frame checks it on every
    call, as the server last reported it (`DatabaseConnection.reported_setting`, no round trip), and refuses the call
    and closes the session unless it is `value` (pre-flight M1, as ⑤'s G3; review M-2)."""

    setting: str
    value: str


_MYSQL_VERSION = re.compile(r"MySQL (\d+)\.(\d+)\.(\d+)")
_MARIADB = re.compile(r"MariaDB \d")


def _mysql_version(server_version: str) -> tuple[int, int, int] | None:
    """MySQL's version from ServerInfo.version (`MySQL 8.4.11`); None for anything else (MariaDB, or a fork whose
    VERSION() is not three numbers: Aurora's `8.0.mysql_aurora.3.05.2`)."""
    found = _MYSQL_VERSION.match(server_version)
    return None if found is None else (int(found[1]), int(found[2]), int(found[3]))


@dataclass(frozen=True)
class ServerRequirement:
    """The servers an MCP agent's frame runs on, judged from ServerInfo.version when its session opens, with no round
    trip (ruling T6-1): MySQL's frame needs `max_execution_time` and RELEASE_ALL_LOCKS(), which MariaDB and MySQL before
    5.7.8 do not have. Only what parses as one of them is refused; any other text (Aurora, an unknown fork) is let
    through."""

    minimum: tuple[int, int, int]
    refused_products: re.Pattern[str]

    def refusal(self, server_version: str) -> str | None:
        """Why `server_version` is refused, or None."""
        found = _mysql_version(server_version)
        if self.refused_products.match(server_version) or (found is not None and found < self.minimum):
            least = ".".join(str(part) for part in self.minimum)
            return f"MCP reads need MySQL {least} or later (this server is {server_version})"
        return None


@dataclass(frozen=True)
class ReadFrame:
    """DbaGuy's own statements around an MCP agent's (spec ⑧ D-08-3), sent on the agent's session: `begin` before it
    (`{timeout_ms}`: the call's timeout plus SERVER_TIMEOUT_MARGIN_S, the server's own limit as a backstop), the
    adapter's rollback() after it when `rolls_back`, then `end`, which releases what a function may have locked for the
    session (a ROLLBACK keeps session locks, H1-e, M5). `explain_around`: what an explain sends before and after the
    statement instead of a head (T-SQL's SHOWPLAN: the statement is not run, T7). `assumes`: a setting the lexer needs
    of the session, checked on every call. `requires`: the servers the frame runs on, checked when a session opens."""

    begin: tuple[str, ...] = ()
    end: tuple[str, ...] = ()
    rolls_back: bool = False
    explain_around: tuple[str, str] | None = None
    assumes: SessionAssumption | None = None
    requires: ServerRequirement | None = None


SERVER_TIMEOUT_MARGIN_S = 5
_READ_FRAMES: dict[Dialect, ReadFrame] = {
    # BEGIN READ ONLY on a session that is read-only by default too (P1, P2); statement_timeout: P10. Off, a backslash
    # in '…' escapes, which RULES[POSTGRES] reads as standard (the server is on by default; a role or database may not)
    Dialect.POSTGRES: ReadFrame(("BEGIN READ ONLY", "SET LOCAL statement_timeout = {timeout_ms}"),
                                ("SELECT pg_advisory_unlock_all()",), rolls_back=True,
                                assumes=SessionAssumption("standard_conforming_strings", "on")),
    # an explicit READ ONLY transaction each time: a function can turn the session's default off (M3, M3e)
    Dialect.MYSQL: ReadFrame(("SET SESSION max_execution_time = {timeout_ms}", "START TRANSACTION READ ONLY"),
                             ("SELECT RELEASE_ALL_LOCKS()",), rolls_back=True,
                             requires=ServerRequirement((5, 7, 8), _MARIADB)),
    # no read-only session at all: what slips through is rolled back (T6). One batch, no extra round trip: the options
    # the lexer assumes (QUOTED_IDENTIFIER: `"…"` is a name) set again, since a SET is not rolled back (review M-3)
    Dialect.TSQL: ReadFrame(("SET QUOTED_IDENTIFIER ON; SET ANSI_NULLS ON; BEGIN TRANSACTION",), rolls_back=True,
                            explain_around=("SET SHOWPLAN_TEXT ON", "SET SHOWPLAN_TEXT OFF")),
    Dialect.SQLITE: ReadFrame(),  # mode=ro, query_only and the authorizer (D-08-4); no transaction to hold locks
    Dialect.SQLPP: ReadFrame(),  # readonly=true on every request (C1); no transactions
}
_MYSQL_TREE = (8, 0, 16)


def read_frame(dialect: Dialect) -> ReadFrame:
    return _READ_FRAMES[dialect]


def explain_head(dialect: Dialect, server_version: str) -> str | None:
    """What the explain tool puts before an agent's statement (D-08-10); None for T-SQL (SHOWPLAN). MySQL's tree format
    needs 8.0.16 (MariaDB and older servers get the plain EXPLAIN)."""
    heads = EXPLAIN_HEADS[dialect]
    if not heads:
        return None
    if dialect == Dialect.MYSQL:
        found = _mysql_version(server_version)
        if found is None or found < _MYSQL_TREE:
            return heads[-1]
    return heads[0]


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
