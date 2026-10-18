import json
import os

import pytest
from loguru import logger
from pydantic import ValidationError

from dbaguy_backend.domain.errors import AlreadyExists, NotFound, StorageError, ValidationFailed
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.settings import AppSettings
from dbaguy_backend.domain.workspace import WorkspaceState, WorkspaceTab
from dbaguy_backend.services import scripts_service
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


@pytest.mark.parametrize("name", ["x" * 251 + ".txt", "a" + "é" * 125 + ".txt"], ids=["255 bytes", "multibyte"])
def test_atomic_text_file_can_write_any_target_name_the_file_system_accepts(tmp_path, name):
    target = tmp_path / name
    with atomic_text_file(target) as fp:  # the temp file name is longer than the target's: it must be cut short
        fp.write("x")
    assert [p.name for p in tmp_path.iterdir()] == [name] and target.read_text() == "x"


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


def test_scripts_dir_cannot_be_an_existing_file(tmp_path):
    (tmp_path / "file").write_text("x")
    service = SettingsService(tmp_path / "settings.json", tmp_path / "scripts")
    with pytest.raises(ValidationFailed, match="not a folder"):
        service.update(AppSettings(scripts_dir=str(tmp_path / "file")))
    assert service.get().scripts_dir is None and not (tmp_path / "settings.json").exists()  # nothing was saved
    (tmp_path / "folder").mkdir()
    service.update(AppSettings(scripts_dir=str(tmp_path / "folder")))  # an existing folder is fine
    service.update(AppSettings(scripts_dir=str(tmp_path / "not-yet")))  # and so is one that will be created


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


def test_workspace_keeps_object_tabs(tmp_path):
    state = WorkspaceState.model_validate({"tabs": [{
        "id": "o1", "kind": "object", "title": "orders",
        "object_path": [{"kind": "schema", "name": "public"}, {"kind": "table", "name": "orders"}],
    }]})
    WorkspaceService(tmp_path / "workspace.json").put(state)
    assert WorkspaceService(tmp_path / "workspace.json").get() == state


def test_an_invalid_tab_is_dropped_alone_with_a_warning_naming_its_fields(tmp_path, logged_warnings):
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps({"tabs": [
        {"id": "t1", "kind": "query", "buffer": "SELECT 1"},
        {"id": "t2", "kind": "chart", "buffer": "secret-buffer", "cursor": -5},
        {"id": "t3", "kind": "table", "future": True},
    ], "active_tab_id": "t1"}))
    state = WorkspaceService(path).get()
    assert [tab.id for tab in state.tabs] == ["t1", "t3"] and state.active_tab_id == "t1"
    assert list(tmp_path.glob("workspace.json.corrupt-*")) == []  # the file itself is fine
    assert len(logged_warnings) == 1
    assert "1" in logged_warnings[0] and "kind" in logged_warnings[0] and "cursor" in logged_warnings[0]
    assert "secret-buffer" not in logged_warnings[0] and "chart" not in logged_warnings[0]


def test_a_tab_with_bad_drafts_or_context_keeps_its_buffer_and_loses_only_those(tmp_path, logged_warnings):
    path = tmp_path / "workspace.json"
    bad_names = {"$1000": "1", "$x y": "1", "$0": "1", "$1abc": "1", "$\u00e9": "1", "$line\nbreak": "1"}
    path.write_text(json.dumps({"tabs": [
        {"id": "t1", "kind": "query", "buffer": "SELECT $x", "query_context": "all",
         "params": {"$ok": "1", **bad_names, "$big": "x" * (1 << 20) + "y"}},
        {"id": "t2", "kind": "query", "buffer": "kept", "params": {"$a": "1"},
         "query_context": {"bucket": "b", "scope": "s"}},
        {"id": "t3", "kind": "query", "buffer": "b3", "params": ["not", "a", "dict"]},
        {"id": "t4", "kind": "query", "buffer": "b4", "params": {f"$p{i}": "1" for i in range(450)},
         "query_context": {"bucket": ""}},
        {"id": "t5", "kind": "chart", "buffer": "dropped as before"},
    ]}))
    state = WorkspaceService(path).get()
    t1, t2, t3, t4 = state.tabs
    assert [t.id for t in state.tabs] == ["t1", "t2", "t3", "t4"]
    assert (t1.buffer, t1.params, t1.query_context) == ("SELECT $x", {"$ok": "1"}, None)
    assert (t2.params, t2.query_context.bucket) == ({"$a": "1"}, "b")  # a good tab is untouched
    assert (t3.buffer, t3.params) == ("b3", None)
    assert (t4.buffer, len(t4.params), list(t4.params)[0], t4.query_context) == ("b4", 400, "$p0", None)
    assert list(tmp_path.glob("workspace.json.corrupt-*")) == []
    repaired = [line for line in logged_warnings if line.startswith("Repaired")]
    assert repaired == [
        "Repaired tab 0 of the saved workspace: removed 7 parameter draft(s) and reset the query context",
        "Repaired tab 2 of the saved workspace: removed 1 parameter draft(s)",
        "Repaired tab 3 of the saved workspace: removed 50 parameter draft(s) and reset the query context",
    ]
    everything = "".join(logged_warnings)  # counts and indexes only: no name, no draft, no context value
    for secret in ("$x y", "$1000", "line", "break", "$ok", "all", "xxxx"):
        assert secret not in everything
    WorkspaceService(path).put(state)  # the next save writes what was kept
    assert [t["id"] for t in json.loads(path.read_text())["tabs"]] == ["t1", "t2", "t3", "t4"]


@pytest.mark.parametrize("content", ["{not json", "[]", '{"tabs": "nope"}', '{"version": "two", "tabs": []}'])
def test_a_workspace_file_unreadable_as_a_whole_is_moved_aside(tmp_path, content):
    path = tmp_path / "workspace.json"
    path.write_text(content)
    assert WorkspaceService(path).get() == WorkspaceState()
    assert len(list(tmp_path.glob("workspace.json.corrupt-*"))) == 1


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


def test_dot_prefixed_names_cannot_be_used_not_even_to_reach_a_git_folder(scripts):
    service, root = scripts
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("c")
    (root / ".hidden.sql").write_text("h")
    service.write("docs/a.sql", "a")
    (root / "via-link").symlink_to(root / ".git", target_is_directory=True)  # a plain name that leads to a dot folder
    hidden = [".git", ".git/config", ".git/hooks/x.sql", ".hidden.sql", "docs/.x.sql", "docs/.d/y.sql",
              "via-link", "via-link/hooks/x.sql"]
    for path in hidden:
        for action in (service.read, service.delete, service.create_folder):
            with pytest.raises(ValidationFailed):
                action(path)
        with pytest.raises(ValidationFailed):
            service.write(path, "x")
        with pytest.raises(ValidationFailed):
            service.rename(path, "moved.sql")  # as the source
        with pytest.raises(ValidationFailed):
            service.rename("docs/a.sql", path)  # as the target
    with pytest.raises(ValidationFailed):
        service.rename(".git", ".git-old")  # a folder: it has no .sql rule to stop it
    with pytest.raises(ValidationFailed):
        service.rename("docs", ".git/hooks/docs")
    with pytest.raises(ValidationFailed):
        service.rename("docs", "via-link/docs")
    assert sorted(p.name for p in root.iterdir()) == [".git", ".hidden.sql", "docs", "via-link"]
    assert sorted(p.name for p in (root / ".git").iterdir()) == ["config", "hooks"]
    assert list((root / ".git" / "hooks").iterdir()) == [] and (root / "docs" / "a.sql").read_text() == "a"
    assert (root / ".git" / "config").read_text() == "c" and (root / ".hidden.sql").read_text() == "h"


def test_a_file_in_the_way_is_a_missing_path_or_a_conflict_never_a_server_error(scripts):
    service, root = scripts
    (root / "file.sql").write_text("x")
    (root / "folder").mkdir()
    service.write("a.sql", "a")
    for missing in (lambda: service.read("file.sql/x.sql"), lambda: service.delete("file.sql/x.sql"),
                    lambda: service.rename("file.sql/x.sql", "y.sql")):
        with pytest.raises(NotFound):
            missing()
    for conflict in (lambda: service.write("file.sql/x.sql", "x"), lambda: service.create_folder("file.sql/sub"),
                     lambda: service.create_folder("file.sql"), lambda: service.rename("folder", "file.sql/f"),
                     lambda: service.rename("a.sql", "file.sql/b.sql")):
        with pytest.raises(AlreadyExists):
            conflict()
    assert sorted(p.name for p in root.iterdir()) == ["a.sql", "file.sql", "folder"]  # nothing created, no temp file
    assert (root / "file.sql").read_text() == "x"


def test_names_longer_than_the_file_system_allows_are_rejected_not_crashes(scripts):
    service, root = scripts
    service.write("a.sql", "a")
    too_long_file, too_long_folder = "x" * 252 + ".sql", "d" * 256  # 256 bytes
    for path in (too_long_file, f"{too_long_folder}/a.sql"):
        for action in (service.read, service.delete, service.create_folder):
            with pytest.raises(ValidationFailed, match="too long"):
                action(path)
        with pytest.raises(ValidationFailed, match="too long"):
            service.write(path, "x")
        with pytest.raises(ValidationFailed, match="too long"):
            service.rename("a.sql", path)
        with pytest.raises(ValidationFailed, match="too long"):
            service.rename(path, "b.sql")
    assert [p.name for p in root.iterdir()] == ["a.sql"]


@pytest.mark.parametrize("name", ["x" * 251 + ".sql", "a" + "é" * 125 + ".sql"], ids=["255 bytes", "multibyte"])
def test_a_script_whose_name_is_as_long_as_allowed_can_be_saved(scripts, name):
    service, root = scripts
    service.write(name, "ok")  # the temp file's name is longer than this one's
    assert service.read(name).content == "ok" and [p.name for p in root.iterdir()] == [name]


def test_only_regular_files_are_used(scripts):
    service, root = scripts
    os.mkfifo(root / "pipe.sql")  # reading it would block for ever
    for action in (service.read, service.delete, lambda p: service.write(p, "x"), lambda p: service.rename(p, "r.sql")):
        with pytest.raises(ValidationFailed, match="regular file"):
            action("pipe.sql")
    (root / "loop.sql").symlink_to(root / "loop.sql")  # a link that points at itself
    (root / "broken.sql").symlink_to(root / "nowhere.sql")
    for name in ("loop.sql", "broken.sql"):
        for action in (service.read, service.delete, lambda p: service.rename(p, "r.sql")):
            with pytest.raises(NotFound):
                action(name)
    assert (root / "pipe.sql").exists() and (root / "loop.sql").is_symlink() and (root / "broken.sql").is_symlink()


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
    with pytest.raises(AlreadyExists):
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


def test_tree_order(scripts):
    service, _ = scripts
    for name in ("b.sql", "A.sql", "c.SQL", "zdir/n.sql", "Adir/n.sql"):
        service.write(name, "x")
    assert [n.name for n in service.tree().items] == ["Adir", "zdir", "A.sql", "b.sql", "c.SQL"]


@pytest.fixture
def logged_warnings():
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink)


def count_nodes(nodes) -> int:
    return sum(1 + count_nodes(n.children) for n in nodes)


def test_the_node_limit_lists_shallow_entries_before_deep_ones(scripts, monkeypatch, logged_warnings):
    service, root = scripts
    for i in range(20):
        (root / "A-modules" / f"pkg{i:02}" / "deep").mkdir(parents=True)  # a big folder that sorts first
    service.write("queries/q.sql", "x")
    service.write("top.sql", "x")
    monkeypatch.setattr(scripts_service, "_MAX_NODES", 10)
    items = service.tree().items
    assert [n.name for n in items] == ["A-modules", "queries", "top.sql"]  # nothing top-level is lost
    assert count_nodes(items) == 10 and len(logged_warnings) == 1
    monkeypatch.setattr(scripts_service, "_MAX_NODES", 10_000)
    logged_warnings.clear()
    assert count_nodes(service.tree().items) == 44 and logged_warnings == []


def test_the_node_limit_cuts_a_single_folder_by_name(scripts, monkeypatch, logged_warnings):
    service, _ = scripts
    for name in ("b.sql", "A.sql", "c.SQL", "zdir/n.sql", "Adir/n.sql"):
        service.write(name, "x")
    monkeypatch.setattr(scripts_service, "_MAX_NODES", 3)
    items = service.tree().items  # folders first, then by name
    assert [n.name for n in items] == ["Adir", "zdir", "A.sql"] and len(logged_warnings) == 1


def test_the_depth_limit_warns_only_when_something_is_left_out(scripts, monkeypatch, logged_warnings):
    service, root = scripts
    monkeypatch.setattr(scripts_service, "_MAX_DEPTH", 2)
    service.create_folder("l1/l2")  # a folder at the deepest level that holds nothing listable: nothing is hidden
    (root / "l1" / "l2" / ".hidden.sql").write_text("x")
    (root / "l1" / "l2" / "notes.txt").write_text("x")
    l1 = service.tree().items[0]
    assert [n.name for n in l1.children] == ["l2"] and l1.children[0].children == [] and logged_warnings == []
    service.write("l1/l2/deep.sql", "x")
    l1 = service.tree().items[0]
    assert l1.children[0].children == [] and len(logged_warnings) == 1  # now there is something below the limit


def test_an_unreadable_scripts_folder_is_a_storage_error(tmp_path):
    service = ScriptsService(lambda: tmp_path / "no-such-folder")
    with pytest.raises(StorageError):
        service.tree()


def test_workspace_keeps_the_tab_context_and_params(tmp_path):  # Review Focus 5
    service = WorkspaceService(tmp_path / "workspace.json")
    state = WorkspaceState(tabs=[
        WorkspaceTab(id="a", kind="query", query_context=QueryContext(bucket="my.bucket", scope="app"),
                     params={"$n": "9007199254740993", "$1": '"x"', "$x": "not json yet"}),
        WorkspaceTab(id="b", kind="query", query_context="none"),
        WorkspaceTab(id="c", kind="query"),
    ])
    service.put(state)
    assert service.get() == state
    with pytest.raises(ValidationError):
        WorkspaceTab(id="d", kind="query", params={"n": "1"})  # a draft is keyed by `$name` or `$n`
    with pytest.raises(ValidationError):
        WorkspaceTab(id="d", kind="query", query_context="all")
