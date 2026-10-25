"""The names a view's or a trigger's text uses, and the routines it calls (06a Task 6 review T1, T4): what a dump looks
for when an object they need is left out."""

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.scope import names_used, routines_called


def test_the_names_a_text_uses_are_its_words_and_quoted_names_never_its_strings():
    text = ("CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN INSERT INTO \"docs fts\" (rowid, [b]) "
            "VALUES (new.id, 'docs_x'); END")
    used = names_used(text, Dialect.SQLITE)
    assert {"docs_ai", "docs", "docs fts", "rowid", "b", "new", "id"} <= used
    assert "docs_x" not in used and "'docs_x'" not in used
    assert names_used('SELECT "a""b" FROM `c`', Dialect.SQLITE) >= {'a"b', "c"}


def test_the_routines_a_body_calls():
    body = ("BEGIN CALL p_log(NEW.id); CALL `p two`; SET NEW.v = `f x`(1) + NEW.total + db.g(2) + COUNT(*); "
            "SET @s = 'h(1)'; END")
    called = routines_called(body, Dialect.MYSQL)
    assert {"p_log", "p two", "f x"} <= called
    assert "total" not in called and "g" not in called and "h" not in called  # a column, a qualified name, a string
    assert routines_called("", Dialect.MYSQL) == set()
