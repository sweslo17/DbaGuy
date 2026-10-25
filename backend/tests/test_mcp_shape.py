"""What an MCP result may hold and how big its next page is (spec ⑧ §3.6, CR-08-7). Pure."""

from dbaguy_backend.agent.shape import PAGE_ROWS_MAX, RESPONSE_BYTES, ResultBudget, cap_cell, compact, note

COLUMNS = [{"name": "v", "type": "text"}]


def test_cells_past_the_limit_become_previews_that_keep_their_size():
    assert cap_cell("abc", 5) == ("abc", 3)
    assert cap_cell("abcdefgh", 5) == ({"$type": "text", "preview": "abcde", "size": 8, "truncated": True}, 8)
    grid_cut = {"$type": "text", "preview": "x" * 65536, "size": 100_000_000, "truncated": True}
    assert cap_cell(grid_cut, 10) == ({"$type": "text", "preview": "x" * 10, "size": 100_000_000,
                                       "truncated": True}, 100_000_000)
    assert cap_cell({"$type": "json", "text": '{"a": 12345}'}, 4) == (
        {"$type": "text", "preview": '{"a"', "size": 12, "truncated": True}, 12)
    blob = {"$type": "bytes", "size": 5_000_000, "preview": "00ff"}
    assert cap_cell(blob, 10) == (blob, 5_000_000)
    assert cap_cell(9007199254740993, 10) == (9007199254740993, 16)
    assert cap_cell(None, 10) == (None, 4)


def test_rows_are_kept_while_they_fit_and_one_that_does_not_ends_the_result():
    budget = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=1000)
    row = ["y" * 1000]
    budget.take([row] * 100)
    kept = len(budget.rows)
    assert 30 < kept < 40 and budget.dropped == 100 - kept and budget.full
    assert len(compact({"columns": COLUMNS, "rows": budget.rows}).encode()) < RESPONSE_BYTES


def test_max_rows_ends_it_too():
    budget = ResultBudget(COLUMNS, max_rows=3, max_cell_chars=1000)
    budget.take([["a"]] * 2)
    assert not budget.full
    budget.take([["b"], ["c"]])
    assert budget.full and len(budget.rows) == 3 and budget.dropped == 1


def test_the_next_page_asks_for_what_the_room_left_holds():
    """Only a count of rows to ask for: the worker ends a page by its size (ruling T6-I1), so a large row read before
    does not shrink the next request, and a small one does not let a page of large rows through."""
    budget = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=10)
    budget.take([["z"]])
    assert budget.next_page() == PAGE_ROWS_MAX  # small rows: the page cap
    budget.take([["m" * 1_000_000], [{"$type": "text", "preview": "h", "size": 100_000_000, "truncated": True}]])
    assert budget.next_page() == PAGE_ROWS_MAX  # sent as 10-character previews: their raw size asks for nothing
    nearly = ResultBudget(COLUMNS, max_rows=5, max_cell_chars=10)
    nearly.take([["a"]] * 3)
    assert nearly.next_page() == 2  # no more than the rows still wanted
    spent = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=1000)
    spent.take([["q" * 1000]] * 30)
    assert spent.next_page() == spent.remaining // spent.largest_encoded


def test_a_note_is_cut_with_an_ellipsis():
    assert note("short") == "short"
    assert note("x" * 600) == "x" * 499 + "…"


def test_a_few_huge_values_end_the_reading():
    budget = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=1000)
    huge = {"$type": "text", "preview": "x" * 65536, "size": 100_000_000, "truncated": True}
    budget.take([[huge]])
    assert budget.read_capped and budget.full and len(budget.rows) == 1  # one 100 MB value: that row alone


def test_sessions_and_adapters_never_import_the_mcp_layer():
    """Final review M7 (spec §3.1): `agent/` builds on `sessions/`, `adapters/` and `domain/`, never the other way."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "dbaguy_backend"
    importing = re.compile(r"^\s*(from\s+\.+agent\b|from\s+dbaguy_backend\.agent\b|import\s+dbaguy_backend\.agent\b)",
                           re.MULTILINE)
    offenders = [str(path.relative_to(root)) for folder in ("sessions", "adapters", "services", "domain", "sql")
                 for path in (root / folder).rglob("*.py") if importing.search(path.read_text())]
    assert offenders == []


def test_a_later_wider_result_keeps_its_cells_when_no_column_was_dropped():
    """T8 re-review 1: rows are cut to the kept columns only when columns were left out (an explain reads every result
    set, and a later one may be wider than the first)."""
    budget = ResultBudget([{"name": "a", "type": None}], 10, 1_000)
    budget.take([["x", "y", "z"]])
    assert budget.rows == [["x", "y", "z"]] and budget.columns_dropped == 0


def test_the_budget_counts_bytes_not_characters():
    """Final review I4: a Chinese cell takes three bytes a character, and the rows kept stop at the bytes."""
    budget = ResultBudget([{"name": "note", "type": "text"}], 1_000, 1_000)
    budget.take([["資料庫" * 100] for _ in range(1_000)])  # 300 characters, 900 bytes each
    taken = len(compact(budget.rows).encode())
    assert budget.dropped and RESPONSE_BYTES - 2_000 < taken < RESPONSE_BYTES and len(compact(budget.rows)) < 14_000
