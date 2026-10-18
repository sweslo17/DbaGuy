"""Statements inside a T-SQL batch. T-SQL needs no `;` between statements, so a batch is cut before every word that
starts a statement, unless that word continues the statement before it (GRANT SELECT, INSERT … TO u; UPDATE t SET …;
ON DELETE SET NULL in a foreign key; UNION SELECT; OFFSET … FETCH; CASE … ELSE … END; …). The merges are kept narrow
so a following DROP, TRUNCATE or unfiltered DELETE/UPDATE stays its own piece and keeps its danger reason.

The guarantee is "fail closed", not "a wrong cut can only make it stricter": the classifier judges every piece, a
statement it does not know is UNKNOWN, and a word floor over the whole batch raises the kind of a piece that hides a
write word (spec §3.4, R-03b-P2, R-03b-P3). Pure; never raises."""

from .lexer import Token, TokenKind

STATEMENT_STARTS = frozenset("""SELECT INSERT UPDATE DELETE MERGE DECLARE SET IF ELSE WHILE BEGIN END PRINT RAISERROR
    THROW EXEC EXECUTE WAITFOR RETURN BREAK CONTINUE GOTO CREATE ALTER DROP TRUNCATE USE GRANT REVOKE DENY BACKUP
    RESTORE DBCC KILL BULK COMMIT ROLLBACK SAVE OPEN FETCH CLOSE DEALLOCATE CHECKPOINT RECONFIGURE SHUTDOWN SETUSER
    REVERT WRITETEXT UPDATETEXT SEND RECEIVE MOVE GET ENABLE DISABLE""".split())
# Not reserved words, so elsewhere they are names (`SELECT throw INTO t2 FROM t` reads a column and creates t2): they
# start a statement only before their own syntax. THROW and RECEIVE: see _starts.
_STARTS_ONLY_BEFORE: dict[str, tuple[str, ...]] = {
    "SEND": ("ON", "CONVERSATION"), "MOVE": ("CONVERSATION",), "GET": ("CONVERSATION", "GROUP"),
    "ENABLE": ("TRIGGER",), "DISABLE": ("TRIGGER",),
}
# RECEIVE has no word of its own after it (RECEIVE TOP (1) * | col, … FROM queue). SQL Server takes it only where a
# statement may begin without a `;` before it (probed): at the start, after a label, after the condition of IF or
# WHILE, after ELSE, and right after BEGIN, BEGIN TRY or BEGIN CATCH (R-03b-T3c). Anywhere else it is a name.
_CONTROL_HEADS = frozenset({"IF", "ELSE", "WHILE"})
_BLOCK_OPENERS = (["BEGIN"], ["BEGIN", "TRY"], ["BEGIN", "CATCH"])
# A start word right after one of these words continues the statement. All of them are reserved words (an alias named
# `after` once joined the DELETE after it to a SELECT and lost its danger reason), so AFTER INSERT|UPDATE|DELETE is
# not here: a trigger is one module statement anyway, and a security policy's block predicate is cut (stricter).
_CONTINUES_AFTER: dict[str, frozenset[str]] = {
    "SELECT": frozenset({"UNION", "ALL", "EXCEPT", "INTERSECT", "FOR"}),  # set operators; DECLARE … CURSOR FOR SELECT
    "INSERT": frozenset({"THEN", "FOR", "OF", "BULK"}),  # MERGE actions, cursors, triggers, BULK INSERT
    "UPDATE": frozenset({"THEN", "FOR", "OF"}),
    "DELETE": frozenset({"THEN", "FOR", "OF"}),
    "MERGE": frozenset({"INNER", "OUTER", "LEFT", "RIGHT", "FULL", "CROSS"}),  # INNER MERGE JOIN (a join hint)
    "FETCH": frozenset({"ROW", "ROWS"}),  # OFFSET n ROWS FETCH NEXT …
    "GRANT": frozenset({"WITH"}),  # … WITH GRANT OPTION
}
# Only in a foreign key of a CREATE / ALTER, after REFERENCES (R-03b-P3): ON DELETE|UPDATE CASCADE, ON DELETE SET NULL.
# Elsewhere an ON ends `SET NOCOUNT ON` or `WITH CHECK_POLICY = ON`, and the UPDATE or DELETE after it is a statement
# of its own.
_CONTINUES_IN_FOREIGN_KEY: dict[str, frozenset[str]] = {
    "INSERT": frozenset({"ON"}), "UPDATE": frozenset({"ON"}), "DELETE": frozenset({"ON"}),
    "SET": frozenset({"UPDATE", "DELETE"}),
}
_DDL_HEADS = frozenset({"CREATE", "ALTER"})
# In a statement that begins with the key, the first of these words (at parenthesis depth 0) continues it. INSERT lists
# VALUES too, which starts nothing: a SELECT after `INSERT … VALUES (…)` is a statement of its own. ALTER does not list
# SET: `ALTER DATABASE d SET …` is cut in two, both DDL or stricter, so no SET can carry a later DELETE into an ALTER.
_CONTINUES_ONCE_IN: dict[str, frozenset[str]] = {
    "WITH": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"}),  # the CTE's statement
    "INSERT": frozenset({"SELECT", "EXEC", "EXECUTE", "VALUES"}),  # INSERT … SELECT, INSERT … EXEC
    "ALTER": frozenset({"ALTER", "DROP"}),  # ALTER TABLE t ALTER COLUMN | DROP COLUMN | DROP CONSTRAINT
}
# After ALTER, an ALTER or DROP followed by one of these starts a statement of its own (ALTER TABLE t ADD c int
# DROP TABLE u is two statements; R-03b-P3): the first word of every object a DROP statement names, but COLUMN (ALTER
# TABLE … DROP COLUMN; a DROP COLUMN MASTER|ENCRYPTION KEY merged into an ALTER still has the alter_drop reason).
_OBJECT_WORDS = frozenset("""AGGREGATE APPLICATION ASSEMBLY ASYMMETRIC AVAILABILITY BROKER CERTIFICATE CONTRACT
    COUNTER CREDENTIAL CRYPTOGRAPHIC DATABASE DEFAULT ENDPOINT EVENT EXTERNAL FULLTEXT FUNCTION INDEX LOGIN MASTER
    MESSAGE PARTITION PROC PROCEDURE QUEUE REMOTE RESOURCE ROLE ROUTE RULE SCHEMA SEARCH SECURITY SENSITIVITY SEQUENCE
    SERVER SERVICE SIGNATURE STATISTICS SYMMETRIC SYNONYM TABLE TRIGGER TYPE USER VIEW WORKLOAD XML""".split())
# MERGE ends only at `;` (T-SQL requires one). GRANT / REVOKE / DENY run on only until their grantee (TO / FROM):
# their privilege lists name statement words, but nothing ends them, so the next statement after the grantee is cut.
_UNTIL_SEMICOLON = frozenset({"MERGE"})
_UNTIL_GRANTEE = frozenset({"GRANT", "REVOKE", "DENY"})


def _words(tokens: list[Token]) -> list[str]:
    """One entry per token: the upper-cased word, "" for anything else and for a word right after a dot, which is
    always a name (`t.throw`, `x.send`)."""
    return [
        t.upper if t.kind == TokenKind.WORD and not (k and tokens[k - 1].kind == TokenKind.DOT) else ""
        for k, t in enumerate(tokens)
    ]


def _starts(words: list[str], tokens: list[Token], i: int, begin: int) -> bool:
    """Whether the word at `i` can start a statement there; the statement so far begins at `begin`."""
    word = words[i]
    if word not in STATEMENT_STARTS:
        return False
    following = tokens[i + 1] if i + 1 < len(tokens) else None
    if word == "THROW":  # THROW, THROW 50000, …, THROW @n, …; a bare THROW before `;`, END CATCH or the end
        after = words[i + 1] if following is not None else ""
        return (
            following is None or following.kind in (TokenKind.SEMI, TokenKind.NUMBER) or after.startswith("@")
            or after == "END"
        )
    if word == "RECEIVE":
        label = i >= 2 and tokens[i - 1].text == ":" and tokens[i - 2].kind == TokenKind.WORD  # lbl: (not ::)
        return label or words[begin] in _CONTROL_HEADS or words[begin:i] in _BLOCK_OPENERS
    if word in _STARTS_ONLY_BEFORE:
        return tuple(words[i + 1:i + 1 + len(_STARTS_ONLY_BEFORE[word])]) == _STARTS_ONLY_BEFORE[word]
    return not (word == "MERGE" and words[i + 1:i + 2] == ["RANGE"])  # ALTER PARTITION FUNCTION … MERGE RANGE (…)


def _continues(head: str, before: str, seen: set[str], words: list[str], tokens: list[Token], i: int) -> bool:
    """Whether the start word at `i` continues the statement whose first word is `head`; `before`: the word right
    before it ("" for anything else); `seen`: the words at depth 0 since `head`."""
    word = words[i]
    following = words[i + 1] if i + 1 < len(tokens) else ""
    if tokens[i - 1].kind == TokenKind.COMMA:
        return True  # a comma never ends a statement
    if head in _UNTIL_SEMICOLON or before in _CONTINUES_AFTER.get(word, ()):
        return True
    if head in _UNTIL_GRANTEE and not seen & {"TO", "FROM"}:
        return True
    if head in _DDL_HEADS and "REFERENCES" in seen and before in _CONTINUES_IN_FOREIGN_KEY.get(word, ()):
        return True
    if word == "SET" and "SET" not in seen and (head == "UPDATE" or (head == "WITH" and "UPDATE" in seen)):
        return True  # UPDATE t SET …, also WITH c AS (…) UPDATE c SET …
    group = _CONTINUES_ONCE_IN.get(head, frozenset())
    if word in group and not group & seen and not (word in ("ALTER", "DROP") and following in _OBJECT_WORDS):
        return True
    # DROP TABLE IF EXISTS t: an IF that is followed by EXISTS and a name, not by EXISTS (subquery)
    return (
        word == "IF" and i + 2 < len(tokens) and words[i + 1] == "EXISTS"
        and tokens[i + 2].kind != TokenKind.LPAREN
    )


def statements(tokens: list[Token]) -> list[list[Token]]:
    """`tokens`: the significant tokens of one batch. Cut at `;` and before a statement start at parenthesis depth 0
    outside CASE … END."""
    words = _words(tokens)
    out: list[list[Token]] = []
    current: list[Token] = []
    begin = 0  # where `current` begins in `tokens`
    head = ""
    seen: set[str] = set()  # words at depth 0 in `current`, its first word excepted
    depth = cases = 0
    for i, token in enumerate(tokens):
        if token.kind == TokenKind.SEMI and depth == 0:
            if current:
                out.append(current)
            current, seen = [], set()
            continue
        word = words[i]
        if (
            current and depth == 0 and cases == 0 and _starts(words, tokens, i, begin)
            and not _continues(head, words[i - 1], seen, words, tokens, i)
        ):
            out.append(current)
            current, seen = [], set()
        if not current:
            begin, head = i, word
        current.append(token)
        if token.kind == TokenKind.LPAREN:
            depth += 1
        elif token.kind == TokenKind.RPAREN:
            depth = max(depth - 1, 0)
        elif word == "CASE":
            cases += 1
        elif word == "END" and cases:
            cases -= 1
        if depth == 0 and word and len(current) > 1:
            seen.add(word)
    if current:
        out.append(current)
    return out
