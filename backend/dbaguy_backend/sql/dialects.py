import re
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
        body_rule=BodyRule.TSQL_MODULE, client_command=ClientCommand.GO, national_strings=True,
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

TSQL_RESERVED = frozenset(  # SQL Server reserved keywords: an extra one only costs a pair of brackets
    """add all alter and any as asc authorization backup begin between break browse bulk by cascade case check
    checkpoint close clustered coalesce collate column commit compute constraint contains containstable continue
    convert create cross current current_date current_time current_timestamp current_user cursor database dbcc
    deallocate declare default delete deny desc disk distinct distributed double drop dump else end errlvl escape
    except exec execute exists exit external fetch file fillfactor for foreign freetext freetexttable from full function
    goto grant group having holdlock identity identity_insert identitycol if in index inner insert intersect into is
    join key kill left like lineno load merge national nocheck nonclustered not null nullif of off offsets on open
    opendatasource openquery openrowset openxml option or order outer over percent pivot plan precision primary print
    proc procedure public raiserror read readtext reconfigure references replication restore restrict return revert
    revoke right rollback rowcount rowguidcol rule save schema securityaudit select semantickeyphrasetable
    semanticsimilaritydetailstable semanticsimilaritytable session_user set setuser shutdown some statistics
    system_user table tablesample textsize then to top tran transaction trigger truncate try_convert tsequal union
    unique unpivot update updatetext use user values varying view waitfor when where while with within writetext""".split()
)

_SIMPLE_IDENT = {
    Dialect.POSTGRES: re.compile(r"[a-z_][a-z0-9_$]*"),  # unquoted PG identifiers fold to lower case
    Dialect.SQLITE: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),  # SQLite identifiers are case-insensitive
    Dialect.MYSQL: re.compile(r"[A-Za-z_][A-Za-z0-9_$]*"),  # unquoted MySQL names keep their case
    Dialect.TSQL: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),
}
_RESERVED = {
    Dialect.POSTGRES: PG_RESERVED, Dialect.SQLITE: SQLITE_RESERVED, Dialect.MYSQL: MYSQL_RESERVED,
    Dialect.TSQL: TSQL_RESERVED,
}
_IDENT_QUOTES: dict[Dialect, tuple[str, str]] = {
    Dialect.POSTGRES: ('"', '"'),
    Dialect.SQLITE: ('"', '"'),
    Dialect.MYSQL: ("`", "`"),  # `"` opens a string in MySQL's default sql_mode
    Dialect.TSQL: ("[", "]"),
}
_BOOL_LITERALS: dict[Dialect, tuple[str, str]] = {
    Dialect.POSTGRES: ("TRUE", "FALSE"), Dialect.SQLITE: ("1", "0"), Dialect.MYSQL: ("TRUE", "FALSE"),
    Dialect.TSQL: ("1", "0"),
}
_BYTES_LITERALS: dict[Dialect, str] = {  # {} is the hex
    Dialect.POSTGRES: "'\\x{}'::bytea", Dialect.SQLITE: "X'{}'", Dialect.MYSQL: "X'{}'", Dialect.TSQL: "0x{}",
}
_JSON_LITERALS: dict[Dialect, str] = {  # {} is a string literal holding the JSON text
    Dialect.POSTGRES: "{}", Dialect.SQLITE: "{}",
    # A JSON column compares a plain string as a JSON string scalar, so `doc = '{"a": 1}'` would match nothing.
    Dialect.MYSQL: "CAST({} AS JSON)",
    Dialect.TSQL: "{}",  # JSON is NVARCHAR text in SQL Server
}
# MySQL keeps dates with a zero year, month or day (`0000-00-00`, `2026-00-15`) that its default sql_mode refuses as
# literals (1525), so such a value is compared with the column's text instead; other dialects have no zero dates.
_ZERO_DATE_AS_TEXT: dict[Dialect, str | None] = {
    Dialect.POSTGRES: None, Dialect.SQLITE: None, Dialect.MYSQL: "CAST({} AS CHAR)", Dialect.TSQL: None,
}
_ZERO_DATE = re.compile(r"(?:0000-\d\d-\d\d|\d{4}-00-\d\d|\d{4}-\d\d-00)(?:$|[ T])", re.ASCII)
_STRING_PREFIXES: dict[Dialect, str] = {  # T-SQL N'…': Unicode, so text outside the column's code page survives
    Dialect.POSTGRES: "", Dialect.SQLITE: "", Dialect.MYSQL: "", Dialect.TSQL: "N",
}
_ISO_DATETIME: dict[Dialect, bool] = {  # T-SQL: write date and time with a "T", the form no login language misreads
    Dialect.POSTGRES: False, Dialect.SQLITE: False, Dialect.MYSQL: False, Dialect.TSQL: True,
}
_DATE_SPACE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
NUMERIC_LITERAL = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)  # text that can be written as a bare number


def needs_quote(dialect: Dialect, name: str) -> bool:
    return not _SIMPLE_IDENT[dialect].fullmatch(name) or name.lower() in _RESERVED[dialect]


def force_quote_ident(dialect: Dialect, name: str) -> str:
    """Always quoted, the closing quote doubled inside: for names interpolated into introspection SQL."""
    opening, closing = _IDENT_QUOTES[dialect]
    return opening + name.replace(closing, closing * 2) + closing


def quote_ident(dialect: Dialect, name: str) -> str:
    return force_quote_ident(dialect, name) if needs_quote(dialect, name) else name


def qualified(dialect: Dialect, schema: str | None, name: str) -> str:
    return f"{quote_ident(dialect, schema)}.{quote_ident(dialect, name)}" if schema else quote_ident(dialect, name)


def quote_literal(dialect: Dialect, value: str) -> str:
    """A string literal that the dialect reads back as exactly `value`: quotes are doubled, and where backslash
    escapes exist (MySQL's default sql_mode), backslashes are doubled too. T-SQL literals carry the N prefix."""
    if RULES[dialect].backslash_escapes:
        value = value.replace("\\", "\\\\")
    return _STRING_PREFIXES[dialect] + "'" + value.replace("'", "''") + "'"


def datetime_literal(dialect: Dialect, text: str) -> str:
    """A date/time value given as the grid's text. SQL Server reads `2026-10-18 12:34:56` by the login's DATEFORMAT
    (a dmy language such as British fails with error 242) but `2026-10-18T12:34:56` the same everywhere."""
    if _ISO_DATETIME[dialect] and (found := _DATE_SPACE_TIME.match(text)):
        text = f"{found.group(1)}T{found.group(2)}{text[found.end():]}"
    return quote_literal(dialect, text)


def bool_literal(dialect: Dialect, value: bool) -> str:
    return _BOOL_LITERALS[dialect][0 if value else 1]


def bytes_literal(dialect: Dialect, hex_digits: str) -> str:
    return _BYTES_LITERALS[dialect].format(hex_digits)


def json_literal(dialect: Dialect, text: str) -> str:
    """A JSON document given as its text, written so that the dialect compares it as JSON."""
    return _JSON_LITERALS[dialect].format(quote_literal(dialect, text))


def zero_date_as_text(dialect: Dialect, column_sql: str, value: str) -> str | None:
    """`column_sql` as text when `value` is a date with a zero part the dialect cannot write as a literal; None when
    the plain comparison works (and keeps using the column's index)."""
    template = _ZERO_DATE_AS_TEXT[dialect]
    return template.format(column_sql) if template is not None and _ZERO_DATE.match(value) else None
