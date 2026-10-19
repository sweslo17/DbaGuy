"""The guard of `scripts/scratch_tables.py sql|hold` (05b Task 6, pre-flight L2): one plain statement on catalogue tables
only. Pure: no database. The manual checks write only `zz_05b_*` tables, so a typo or a stray `;` must not reach a seeded
table."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scratch_tables.py"
_spec = importlib.util.spec_from_file_location("scratch_tables", SCRIPT)
scratch_tables = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scratch_tables)


def violation(engine: str, statement: str) -> str | None:
    return scratch_tables.scratch_violation(statement, scratch_tables.ENGINES[engine])


ACCEPTED = [
    ("pg", "UPDATE public.zz_05b_items SET label = 'theirs' WHERE id = 30"),
    ("pg", "update PUBLIC.ZZ_05B_ITEMS set label = 'x'"),
    ("pg", "UPDATE public.zz_05b_items SET label = 'x' WHERE id = 1;"),  # one trailing `;`
    ("pg", "UPDATE public.zz_05b_items\n  SET label = 'it''s'\n  WHERE id = 1"),
    ("pg", "UPDATE zz_05b_items SET label = 'a;b -- c /* d */' WHERE id = 1"),  # nothing special inside a literal
    ("pg", "DELETE FROM public.zz_05b_items WHERE id = 2"),
    ("pg", "INSERT INTO public.zz_05b_items(label) VALUES ('x')"),
    ("pg", "INSERT INTO public.zz_05b_nokey DEFAULT VALUES"),
    ("pg", "INSERT INTO public.zz_05b_nokey (a, b) SELECT a, b FROM public.zz_05b_nokey"),
    ("pg", "SELECT id FROM public.zz_05b_items WHERE id < 5"),
    ("pg", "UPDATE public.zz_05b_items SET label = 'x' WHERE id IN (SELECT a FROM public.zz_05b_nokey)"),
    ("mysql", "DELETE FROM zz_05b_myisam WHERE id = 2"),
    ("mssql", "UPDATE dbo.zz_05b_items SET label = N'x' WHERE id = 3"),
    ("sqlite", "SELECT id FROM zz_05b_items"),
]

REFUSED = [
    # a seeded table that only mentions the prefix in a literal
    ("pg", "UPDATE public.orders SET note = 'zz_05b_x'"),
    ("pg", "SELECT * FROM public.orders WHERE note = 'zz_05b_'"),
    # two statements, with `;` or (T-SQL) without it
    ("pg", "UPDATE public.zz_05b_items SET label = 'a'; UPDATE public.orders SET note = 'b'"),
    ("pg", "UPDATE public.zz_05b_items SET a = 1; UPDATE public.zz_05b_items SET b = 2"),
    ("pg", "UPDATE public.zz_05b_items SET a = 1 WHERE id = 1 ; ;"),
    ("mssql", "UPDATE dbo.zz_05b_items SET a = 1 UPDATE dbo.orders SET b = 2"),
    ("mssql", "UPDATE dbo.zz_05b_items SET a = 1 DELETE FROM dbo.orders"),
    ("mssql", "UPDATE dbo.zz_05b_items SET a = 1 DROP TABLE dbo.orders"),
    # a name that only starts with the prefix, or ends in a catalogue name
    ("pg", "UPDATE public.zz_05b_other SET a = 1"),
    ("pg", "UPDATE zz_05b_ SET a = 1"),
    ("pg", "UPDATE public.zz_05b_items_copy SET a = 1"),
    ("pg", "UPDATE zz_05b_itemsX SET a = 1"),
    ("pg", "DELETE FROM public.xzz_05b_items"),
    ("pg", "SELECT * FROM public.zz_05b_secret"),
    # another table in the statement
    ("pg", "DELETE FROM public.orders WHERE id IN (SELECT a FROM public.zz_05b_nokey)"),
    ("pg", "INSERT INTO public.orders SELECT * FROM public.zz_05b_items"),
    ("pg", "UPDATE public.zz_05b_items SET a = 1 FROM public.orders"),
    ("pg", "SELECT * FROM public.zz_05b_items i JOIN public.orders o ON true"),
    ("pg", "INSERT INTO public.zz_05b_items, public.orders VALUES (1)"),
    ("mssql", "SELECT * INTO dbo.evil FROM dbo.zz_05b_items"),
    ("mysql", "UPDATE zz_05b_items, orders SET orders.a = 1"),
    ("mysql", "UPDATE zz_05b_items a, orders o SET o.a = 1"),
    ("mysql", "UPDATE zz_05b_items JOIN orders ON 1 SET orders.a = 1"),
    # not a data statement, or not one plain statement
    ("pg", "DROP TABLE public.zz_05b_items"),
    ("pg", "TRUNCATE TABLE public.zz_05b_items"),
    ("pg", "UPDATE public.zz_05b_items SET label = 'x' WHERE 1 = 1 DROP TABLE public.orders"),
    ("pg", "WITH x AS (SELECT 1) UPDATE zz_05b_items SET a = 1"),
    ("pg", "SELECT 1"),
    ("pg", ""),
    ("pg", "UPDATE zz_05b_items SET note = 'x' -- trailing comment"),
    ("pg", "UPDATE zz_05b_items SET note = 'x' /* c */"),
    ("pg", "UPDATE zz_05b_items SET note = 'abc"),
    ("pg", "UPDATE zz_05b_items SET note = 'a\\' WHERE 1 = 1"),
    ("pg", 'UPDATE "public"."zz_05b_items" SET note = \'x\''),
    ("mssql", "UPDATE [zz_05b_items] SET note = N'x'"),
    # another engine's schema
    ("pg", "UPDATE dbo.zz_05b_items SET a = 1"),
    ("mssql", "UPDATE public.zz_05b_items SET a = 1"),
]


@pytest.mark.parametrize(("engine", "statement"), ACCEPTED)
def test_a_plain_statement_on_a_catalogue_table_is_accepted(engine, statement):
    assert violation(engine, statement) is None


@pytest.mark.parametrize(("engine", "statement"), REFUSED)
def test_anything_else_is_refused_with_a_reason(engine, statement):
    assert violation(engine, statement)


def test_every_catalogue_object_is_a_scratch_name():
    for engine in scratch_tables.ENGINES.values():
        for _, name in engine.objects:
            assert name.rsplit(".", 1)[-1].startswith(scratch_tables.PREFIX)
            assert name.lower() in engine.tables()
