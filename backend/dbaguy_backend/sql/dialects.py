import re
from dataclasses import dataclass
from enum import StrEnum

from ..domain.dialect import Dialect  # the key of every rule table in sql/; imported from here by the sql modules


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

_SIMPLE_IDENT = {
    Dialect.POSTGRES: re.compile(r"[a-z_][a-z0-9_$]*"),  # unquoted PG identifiers fold to lower case
    Dialect.SQLITE: re.compile(r"[A-Za-z_][A-Za-z0-9_]*"),  # SQLite identifiers are case-insensitive
    Dialect.MYSQL: re.compile(r"[A-Za-z_][A-Za-z0-9_$]*"),  # unquoted MySQL names keep their case
}
_RESERVED = {Dialect.POSTGRES: PG_RESERVED, Dialect.SQLITE: SQLITE_RESERVED, Dialect.MYSQL: MYSQL_RESERVED}
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
_JSON_LITERALS: dict[Dialect, str] = {  # {} is a string literal holding the JSON text
    Dialect.POSTGRES: "{}", Dialect.SQLITE: "{}",
    # A JSON column compares a plain string as a JSON string scalar, so `doc = '{"a": 1}'` would match nothing.
    Dialect.MYSQL: "CAST({} AS JSON)",
}
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
    escapes exist (MySQL's default sql_mode), backslashes are doubled too."""
    if RULES[dialect].backslash_escapes:
        value = value.replace("\\", "\\\\")
    return "'" + value.replace("'", "''") + "'"


def bool_literal(dialect: Dialect, value: bool) -> str:
    return _BOOL_LITERALS[dialect][0 if value else 1]


def bytes_literal(dialect: Dialect, hex_digits: str) -> str:
    return _BYTES_LITERALS[dialect].format(hex_digits)


def json_literal(dialect: Dialect, text: str) -> str:
    """A JSON document given as its text, written so that the dialect compares it as JSON."""
    return _JSON_LITERALS[dialect].format(quote_literal(dialect, text))
