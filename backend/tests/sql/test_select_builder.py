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


def test_blank_filter_means_no_where_and_count_ignores_order():
    assert build_browse_select(PG, PG_PATH, "  ;  ", []) == 'SELECT * FROM sales."Order Items"'
    assert build_browse_select(PG, PG_PATH, None, [SortKey(column="id")], count=True) == (
        'SELECT count(*) FROM sales."Order Items"'
    )


def test_partition_and_trailing_comment_in_filter():
    partition = path(("schema", "public"), ("table", "events"), ("folder", "partitions"), ("partition", "events_2026"))
    assert relation_from_path(partition) == ("public", "events_2026")
    sql = build_browse_select(PG, PG_PATH, "x = 1 /* unterminated", [SortKey(column="id")])
    assert sql == 'SELECT * FROM sales."Order Items"\nWHERE x = 1\nORDER BY id ASC'
