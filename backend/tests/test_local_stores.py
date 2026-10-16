import json

import pytest

from dbaguy_backend.domain.errors import AlreadyExists, NotFound, StorageError, ValidationFailed
from dbaguy_backend.domain.settings import AppSettings
from dbaguy_backend.domain.workspace import WorkspaceState
from dbaguy_backend.services.scripts_service import ScriptsService
from dbaguy_backend.services.settings_service import SettingsService
from dbaguy_backend.services.workspace_service import WorkspaceService
from dbaguy_backend.storage.atomic_file import atomic_text_file
from dbaguy_backend.storage.json_files import JsonDocument, write_text_atomic


def test_atomic_text_file_replaces_the_target_in_one_step(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old")
    with atomic_text_file(target) as fp:
        fp.write("new\r\nline")
        assert target.read_text() == "old"  # nothing visible until the block ends
    assert target.read_bytes() == b"new\r\nline"  # written as given: no newline translation
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_text_file_failure_keeps_the_old_target_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old")
    with pytest.raises(RuntimeError), atomic_text_file(target) as fp:
        fp.write("half")
        raise RuntimeError("boom")
    assert target.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_text_file_never_touches_a_foreign_part_file(tmp_path):
    target = tmp_path / "out.txt"
    foreign = tmp_path / "out.txt.part"
    foreign.write_text("someone else's")
    with atomic_text_file(target) as fp:
        fp.write("new")
    with pytest.raises(RuntimeError), atomic_text_file(target) as fp:
        raise RuntimeError("boom")
    assert foreign.read_text() == "someone else's" and target.read_text() == "new"


def test_atomic_text_file_writers_do_not_share_a_temp_file(tmp_path):
    target = tmp_path / "out.txt"
    with atomic_text_file(target) as first, atomic_text_file(target) as second:
        first.write("first")
        second.write("second")
        assert len(list(tmp_path.iterdir())) == 2
    assert len(list(tmp_path.iterdir())) == 1 and target.read_text() in {"first", "second"}


def test_write_text_atomic_creates_parents_and_reports_failures_as_the_given_error(tmp_path):
    write_text_atomic(tmp_path / "a" / "b.txt", "x", StorageError)
    assert (tmp_path / "a" / "b.txt").read_text() == "x"
    with pytest.raises(StorageError, match="Cannot save"):
        write_text_atomic(tmp_path / "a", "x", StorageError)  # a folder is in the way
    with pytest.raises(StorageError, match="Cannot save"):
        write_text_atomic(tmp_path / "lone.txt", "\ud800", StorageError)  # not encodable
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a"]


def test_json_document_keeps_unknown_fields_even_when_saved_before_it_was_loaded(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"future": {"x": 1}, "scripts_dir": "/old"}))
    JsonDocument(path, AppSettings).save(AppSettings(scripts_dir="/new"))
    assert json.loads(path.read_text()) == {**AppSettings(scripts_dir="/new").model_dump(mode="json"), "future": {"x": 1}}


def test_json_document_reads_once_and_load_reads_again(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"scripts_dir": "/one"}))
    doc = JsonDocument(path, AppSettings)
    assert doc.get().scripts_dir == "/one"
    path.write_text(json.dumps({"scripts_dir": "/two"}))
    assert doc.get().scripts_dir == "/one" and doc.load().scripts_dir == "/two" and doc.get().scripts_dir == "/two"
    doc.save(AppSettings(scripts_dir="/three"))
    assert doc.get().scripts_dir == "/three" and json.loads(path.read_text())["scripts_dir"] == "/three"


def test_settings_defaults_and_unknown_fields_survive(tmp_path):
    path = tmp_path / "settings.json"
    assert SettingsService(path, tmp_path / "scripts").get() == AppSettings()
    path.write_text(json.dumps({"editor": {"font_size": 15, "future": 1}, "future_section": {"x": 2}}))
    service = SettingsService(path, tmp_path / "scripts")
    current = service.get()
    assert current.editor.font_size == 15 and current.run.page_size == 500
    service.update(current.model_copy(update={"run": current.run.model_copy(update={"page_size": 1000})}))
    saved = json.loads(path.read_text())
    assert saved["run"]["page_size"] == 1000 and saved["future_section"] == {"x": 2} and saved["editor"]["future"] == 1


@pytest.mark.parametrize("content", ["{not json", '{"editor": {"font_size": 999}}', "[]"])
def test_unreadable_settings_are_moved_aside(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    assert SettingsService(path, tmp_path / "scripts").get() == AppSettings()
    assert len(list(tmp_path.glob("settings.json.corrupt-*"))) == 1


def test_a_corrupt_file_is_reported_without_its_contents(tmp_path):
    from loguru import logger

    path = tmp_path / "settings.json"
    path.write_text('{"editor": {"font_size": 999, "secret": "do-not-log-me"}}')
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="ERROR")
    try:
        SettingsService(path, tmp_path / "scripts").get()
    finally:
        logger.remove(sink)
    assert len(messages) == 1 and "editor.font_size" in messages[0] and "999" not in messages[0]
    assert "do-not-log-me" not in messages[0]


def test_scripts_root(tmp_path):
    service = SettingsService(tmp_path / "settings.json", tmp_path / "scripts")
    assert service.scripts_root() == tmp_path / "scripts" and (tmp_path / "scripts").is_dir()
    with pytest.raises(ValidationFailed):
        service.update(AppSettings(scripts_dir="relative/dir"))
    service.update(AppSettings(scripts_dir=str(tmp_path / "repo")))
    assert service.scripts_root() == tmp_path / "repo"


def test_scripts_dir_expands_the_home_folder_and_rejects_unknown_users(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    service = SettingsService(tmp_path / "settings.json", tmp_path / "scripts")
    service.update(AppSettings(scripts_dir="~/sql"))
    assert service.scripts_root() == tmp_path / "sql"
    with pytest.raises(ValidationFailed):
        service.update(AppSettings(scripts_dir="~no-such-user-dbaguy/sql"))
    with pytest.raises(ValidationFailed):
        service.update(AppSettings(scripts_dir=""))


def test_a_scripts_folder_that_cannot_be_created_is_a_storage_error(tmp_path):
    (tmp_path / "file").write_text("x")
    service = SettingsService(tmp_path / "settings.json", tmp_path / "file" / "scripts")
    with pytest.raises(StorageError, match="scripts folder"):
        service.scripts_root()


def test_workspace_round_trip(tmp_path):
    service = WorkspaceService(tmp_path / "workspace.json")
    assert service.get() == WorkspaceState()
    state = WorkspaceState.model_validate({
        "tabs": [{"id": "t1", "kind": "query", "title": "Q", "buffer": "SELECT '😀'", "cursor": 11,
                  "selection": {"anchor": 0, "head": 6}}],
        "active_tab_id": "t1",
    })
    service.put(state)
    assert WorkspaceService(tmp_path / "workspace.json").get() == state


@pytest.mark.parametrize("tab", [{"id": "", "kind": "query"}, {"id": "x" * 101, "kind": "query"},
                                 {"id": "t", "kind": "other"}, {"id": "t", "kind": "query", "cursor": -1},
                                 {"id": "t", "kind": "query", "unknown": 1}])
def test_workspace_tabs_are_validated(tab):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkspaceState.model_validate({"tabs": [tab]})


@pytest.fixture
def scripts(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    return ScriptsService(lambda: root), root


def test_scripts_crud_and_tree(scripts):
    service, root = scripts
    service.create_folder("reports")
    service.write("reports/daily.sql", "SELECT 1;")
    service.write("a.sql", "SELECT 2;")
    (root / "notes.txt").write_text("x")
    (root / ".hidden.sql").write_text("x")
    tree = service.tree()
    assert tree.root == str(root)
    assert [(n.name, n.kind) for n in tree.items] == [("reports", "folder"), ("a.sql", "file")]
    assert tree.items[0].children[0].path == "reports/daily.sql"
    assert service.read("reports/daily.sql").content == "SELECT 1;"
    service.rename("a.sql", "reports/b.sql")
    with pytest.raises(AlreadyExists):
        service.rename("reports/b.sql", "reports/daily.sql")
    with pytest.raises(ValidationFailed):
        service.delete("reports")  # not empty
    service.delete("reports/b.sql")
    service.delete("reports/daily.sql")
    service.delete("reports")
    assert service.tree().items == []
    with pytest.raises(NotFound):
        service.read("gone.sql")


@pytest.mark.parametrize("path", ["../x.sql", "/etc/passwd", "a/../../x.sql", "", "a\\b.sql", "nul\0.sql"])
def test_paths_outside_the_root_are_rejected(scripts, path):
    service, _ = scripts
    with pytest.raises(ValidationFailed):
        service.read(path)
    with pytest.raises(ValidationFailed):
        service.write(path, "x")


@pytest.mark.parametrize("path", [".", "./", "a/..", "a/../b.sql"])
def test_the_root_itself_and_dot_dot_components_are_rejected(scripts, path):
    service, _ = scripts
    for action in (service.read, service.delete, service.create_folder):
        with pytest.raises(ValidationFailed):
            action(path)


def test_every_operation_goes_through_the_guard(scripts, tmp_path):
    service, root = scripts
    (tmp_path / "outside.sql").write_text("s")
    service.write("in.sql", "x")
    for bad in ("../outside.sql", "/etc/hosts"):
        with pytest.raises(ValidationFailed):
            service.delete(bad)
        with pytest.raises(ValidationFailed):
            service.create_folder(bad)
        with pytest.raises(ValidationFailed):
            service.rename(bad, "x.sql")
        with pytest.raises(ValidationFailed):
            service.rename("in.sql", bad)
    assert (tmp_path / "outside.sql").read_text() == "s" and (root / "in.sql").read_text() == "x"


def test_symlink_escapes_are_rejected(scripts, tmp_path):
    service, root = scripts
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.sql").write_text("s")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "file-link.sql").symlink_to(outside / "secret.sql")
    with pytest.raises(ValidationFailed):
        service.read("link/secret.sql")
    with pytest.raises(ValidationFailed):
        service.write("link/new.sql", "x")
    with pytest.raises(ValidationFailed):
        service.write("file-link.sql", "overwritten")  # a link to a file outside the root
    with pytest.raises(ValidationFailed):
        service.delete("file-link.sql")
    with pytest.raises(ValidationFailed):
        service.rename("file-link.sql", "moved.sql")
    with pytest.raises(ValidationFailed):
        service.create_folder("link/sub")
    assert not (outside / "new.sql").exists() and (outside / "secret.sql").read_text() == "s"
    assert service.tree().items == []  # symlinks are never followed


def test_a_symlink_to_another_kind_of_file_inside_the_root_cannot_be_used_to_overwrite_it(scripts):
    service, root = scripts
    (root / "notes.txt").write_text("keep")
    (root / "alias.sql").symlink_to(root / "notes.txt")
    with pytest.raises(ValidationFailed):
        service.write("alias.sql", "overwritten")
    with pytest.raises(ValidationFailed):
        service.read("alias.sql")
    assert (root / "notes.txt").read_text() == "keep"


def test_only_sql_files_are_written(scripts):
    service, _ = scripts
    with pytest.raises(ValidationFailed):
        service.write("x.txt", "x")
    service.write("x.sql", "x")
    service.write("Y.SQL", "y")  # case-insensitive
    with pytest.raises(ValidationFailed):
        service.rename("x.sql", "x.txt")


def test_other_files_in_the_folder_are_neither_read_nor_deleted_nor_moved(scripts):
    service, root = scripts
    (root / "README.md").write_text("docs")
    (root / "sub").mkdir()
    (root / "sub" / "config").write_text("c")
    for action in (service.read, service.delete, lambda p: service.rename(p, "moved.sql")):
        with pytest.raises(ValidationFailed):
            action("README.md")
        with pytest.raises(ValidationFailed):
            action("sub/config")
    assert (root / "README.md").read_text() == "docs" and (root / "sub" / "config").read_text() == "c"


def test_write_limits_and_conflicts(scripts):
    service, root = scripts
    service.write("big.sql", "x" * (10 * 1024 * 1024))  # exactly the limit
    with pytest.raises(ValidationFailed, match="10 MB"):
        service.write("big.sql", "x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(ValidationFailed, match="10 MB"):
        service.write("big.sql", "é" * (5 * 1024 * 1024 + 1))  # the limit counts bytes, not characters
    assert (root / "big.sql").stat().st_size == 10 * 1024 * 1024  # a rejected write changes nothing
    with pytest.raises(ValidationFailed):
        service.write("lone.sql", "\ud800")
    (root / "folder.sql").mkdir()
    with pytest.raises(ValidationFailed):
        service.write("folder.sql", "x")
    (root / "file.sql").write_text("x")
    with pytest.raises(StorageError):
        service.write("file.sql/inside.sql", "x")  # a file where a parent folder should be


def test_a_script_is_stored_and_returned_exactly_as_written(scripts):
    service, root = scripts
    text = "SELECT 'é😀';\r\n-- 中文\r\n"
    written = service.write("deep/er/x.sql", text)
    assert written.path == "deep/er/x.sql" and written.content == text
    assert (root / "deep" / "er" / "x.sql").read_bytes() == text.encode("utf-8")  # CRLF kept, parents created
    read = service.read("deep/er/x.sql")
    assert read.content == text and read.modified_at.tzinfo is not None
    assert [p.name for p in (root / "deep" / "er").iterdir()] == ["x.sql"]  # no temp file left behind
    (root / "bin.sql").write_bytes(b"\xff\xfe")
    with pytest.raises(ValidationFailed, match="UTF-8"):
        service.read("bin.sql")
    (root / "huge.sql").write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(ValidationFailed, match="10 MB"):
        service.read("huge.sql")
    with pytest.raises(ValidationFailed):
        service.read("deep")  # a folder


def test_rename_rules(scripts):
    service, root = scripts
    service.write("a.sql", "A")
    service.create_folder("f")
    service.write("f/x.sql", "X")
    with pytest.raises(NotFound):
        service.rename("missing.sql", "m.sql")
    with pytest.raises(AlreadyExists):
        service.rename("a.sql", "a.sql")
    service.rename("a.sql", "new/dir/b.sql")  # parents of the target are created
    assert (root / "new" / "dir" / "b.sql").read_text() == "A" and not (root / "a.sql").exists()
    with pytest.raises(AlreadyExists):
        service.rename("f", "new")
    with pytest.raises(ValidationFailed):
        service.rename("f", "f/inside")
    service.rename("f", "g")  # a folder keeps its contents; its name needs no suffix
    assert (root / "g" / "x.sql").read_text() == "X"


def test_a_case_only_rename_is_not_a_conflict_on_a_case_insensitive_file_system(scripts):
    service, root = scripts
    service.write("Report.sql", "x")
    if not (root / "report.SQL").exists():
        pytest.skip("case-sensitive file system")
    service.rename("Report.sql", "report.sql")
    assert [p.name for p in root.iterdir()] == ["report.sql"]


def test_create_folder_and_delete_conflicts(scripts):
    service, root = scripts
    service.create_folder("a/b/c")
    assert (root / "a" / "b" / "c").is_dir()
    with pytest.raises(AlreadyExists):
        service.create_folder("a/b")
    service.write("f.sql", "x")
    with pytest.raises(AlreadyExists):
        service.create_folder("f.sql")
    (root / "a" / "b" / "c" / ".DS_Store").write_text("x")  # not shown in the tree, but not ours to delete
    with pytest.raises(ValidationFailed, match="not empty"):
        service.delete("a/b/c")
    with pytest.raises(NotFound):
        service.delete("missing.sql")
    with pytest.raises(NotFound):
        service.delete("missing")


def test_tree_order_and_limits(scripts, monkeypatch):
    from loguru import logger

    from dbaguy_backend.services import scripts_service

    service, root = scripts
    for name in ("b.sql", "A.sql", "c.SQL", "zdir/n.sql", "Adir/n.sql"):
        service.write(name, "x")
    assert [n.name for n in service.tree().items] == ["Adir", "zdir", "A.sql", "b.sql", "c.SQL"]

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        monkeypatch.setattr(scripts_service, "_MAX_NODES", 3)
        listed = service.tree().items  # depth first, folders first, then by name: Adir, Adir/n.sql, zdir
        assert [n.name for n in listed] == ["Adir", "zdir"] and [c.name for c in listed[0].children] == ["n.sql"]
        assert listed[1].children == []
        assert len(messages) == 1

        monkeypatch.setattr(scripts_service, "_MAX_NODES", 10_000)
        monkeypatch.setattr(scripts_service, "_MAX_DEPTH", 2)
        service.write("l1/l2/l3/deep.sql", "x")
        messages.clear()
        l1 = next(n for n in service.tree().items if n.name == "l1")
        assert [n.name for n in l1.children] == ["l2"] and l1.children[0].children == []
        assert len(messages) == 1
    finally:
        logger.remove(sink)


def test_an_unreadable_scripts_folder_is_a_storage_error(tmp_path):
    service = ScriptsService(lambda: tmp_path / "no-such-folder")
    with pytest.raises(StorageError):
        service.tree()
