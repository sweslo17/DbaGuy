import errno
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from loguru import logger

from ..domain.errors import AlreadyExists, DomainError, NotFound, StorageError, ValidationFailed
from ..domain.scripts import ScriptFile, ScriptNode, ScriptTree
from ..storage.json_files import write_text_atomic

_MAX_SCRIPT_BYTES = 10 * 1024 * 1024
_MAX_DEPTH = 10
_MAX_NODES = 10_000
_SQL_SUFFIX = ".sql"


def _is_sql(name: str) -> bool:
    return name.lower().endswith(_SQL_SUFFIX)


@dataclass
class _TreeBudget:
    """What a tree walk may still list; `exhausted` records that something was left out."""

    nodes_left: int
    exhausted: bool = False


class ScriptsService:
    """The scripts folder: `.sql` files and folders below one root, never anything outside it.

    Every path a method takes goes through `_resolve`. Files are only ever read, written, moved or deleted when they
    are `.sql` files, so a repository used as the scripts folder keeps everything else as it is.
    """

    def __init__(self, root: Callable[[], Path]) -> None:
        self._root = root

    def tree(self) -> ScriptTree:
        root = self._root().resolve()
        budget = _TreeBudget(_MAX_NODES)
        try:
            items = self._scan(root, root, 1, budget)
        except OSError as exc:
            logger.error("Cannot list the scripts folder {}: {}", root, exc)
            raise StorageError(f"Cannot list the scripts folder: {exc.strerror or exc}") from exc
        if budget.exhausted:
            logger.warning(
                "The scripts folder is large: its listing stops at {} levels or {} entries", _MAX_DEPTH, _MAX_NODES
            )
        return ScriptTree(root=str(self._root()), items=items)

    def read(self, path: str) -> ScriptFile:
        root, target = self._resolve(path)
        self._require_sql(target)
        if target.is_dir():
            raise ValidationFailed(f"{path} is a folder")
        try:
            stat = target.stat()
            if stat.st_size > _MAX_SCRIPT_BYTES:
                raise ValidationFailed(f"{path} is larger than 10 MB")
            data = target.read_bytes()  # bytes: reading as text would turn CRLF into LF
        except OSError as exc:
            raise self._io_error("read", path, exc) from exc
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed(f"{path} is not UTF-8 text") from exc
        return ScriptFile(path=self._relative(root, target), content=content, modified_at=self._modified_at(stat))

    def write(self, path: str, content: str) -> ScriptFile:
        root, target = self._resolve(path)
        self._require_sql(target)
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:  # e.g. a lone surrogate
            raise ValidationFailed("The script is not valid text") from exc
        if size > _MAX_SCRIPT_BYTES:
            raise ValidationFailed("A script can be at most 10 MB")
        if target.is_dir():
            raise ValidationFailed(f"{path} is a folder")
        write_text_atomic(target, content, StorageError)  # also creates the parent folders, which are inside the root
        relative = self._relative(root, target)
        logger.info("Saved script {}", relative)
        return ScriptFile(path=relative, content=content, modified_at=self._modified_at(target.stat()))

    def rename(self, source: str, target: str) -> None:
        root, source_path = self._resolve(source)
        _, target_path = self._resolve(target)
        if not source_path.exists():
            raise NotFound(f"{source} does not exist")
        # Never overwrite. On a file system that ignores case, `Report.sql` -> `report.sql` finds "itself" at the target
        # and is a plain rename.
        if target_path.exists() and (target_path == source_path or not os.path.samefile(source_path, target_path)):
            raise AlreadyExists(f"{target} already exists")
        if source_path.is_dir():
            if target_path.is_relative_to(source_path):
                raise ValidationFailed("A folder cannot be moved into itself")
        else:
            self._require_sql(source_path)
            self._require_sql(target_path)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.rename(target_path)
        except OSError as exc:
            raise self._io_error("rename", source, exc) from exc
        logger.info("Renamed {} to {}", self._relative(root, source_path), self._relative(root, target_path))

    def delete(self, path: str) -> None:
        root, target = self._resolve(path)
        if not target.exists():
            raise NotFound(f"{path} does not exist")
        try:
            if target.is_dir():
                target.rmdir()  # only an empty folder: files this list does not show are not ours to delete
            else:
                self._require_sql(target)
                target.unlink()
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise ValidationFailed(f"{path} is not empty") from exc
            raise self._io_error("delete", path, exc) from exc
        logger.info("Deleted {}", self._relative(root, target))

    def create_folder(self, path: str) -> None:
        root, target = self._resolve(path)
        try:
            target.mkdir(parents=True)
        except FileExistsError as exc:
            raise AlreadyExists(f"{path} already exists") from exc
        except OSError as exc:
            raise self._io_error("create", path, exc) from exc
        logger.info("Created folder {}", self._relative(root, target))

    def _resolve(self, relative: str) -> tuple[Path, Path]:
        """(root, target) — both resolved; rejects anything that is not strictly inside the root."""
        if not relative or "\\" in relative or "\0" in relative or relative.startswith("/"):
            raise ValidationFailed("Script paths are relative to the scripts folder, separated by '/'")
        parts = PurePosixPath(relative).parts
        if ".." in parts:
            raise ValidationFailed("Script paths cannot contain '..'")
        root = self._root().resolve()
        target = root.joinpath(*parts).resolve()  # follows symlinks: a link pointing outside is caught below
        if target == root or not target.is_relative_to(root):
            raise ValidationFailed(f"{relative} is outside the scripts folder")
        return root, target

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()

    @staticmethod
    def _require_sql(target: Path) -> None:
        """The resolved file: a link named `x.sql` that points at another kind of file does not get past this."""
        if not _is_sql(target.name):
            raise ValidationFailed(f"Only {_SQL_SUFFIX} files can be used here: {target.name}")

    @staticmethod
    def _modified_at(stat: os.stat_result) -> datetime:
        return datetime.fromtimestamp(stat.st_mtime, tz=UTC)

    @staticmethod
    def _io_error(action: str, path: str, exc: OSError) -> DomainError:
        if isinstance(exc, FileNotFoundError):
            return NotFound(f"{path} does not exist")
        logger.error("Cannot {} script {}: {}", action, path, exc)
        return StorageError(f"Cannot {action} {path}: {exc.strerror or exc}")

    def _scan(self, directory: Path, root: Path, depth: int, budget: _TreeBudget) -> list[ScriptNode]:
        """The folders and `.sql` files directly in `directory`, folders first, then by name. Symlinks are skipped."""
        found: list[tuple[str, bool]] = []  # (name, is a folder)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    found.append((entry.name, True))
                elif entry.is_file(follow_symlinks=False) and _is_sql(entry.name):
                    found.append((entry.name, False))
        found.sort(key=lambda item: (not item[1], item[0].lower(), item[0]))
        nodes: list[ScriptNode] = []
        for name, is_folder in found:
            if budget.nodes_left == 0:
                budget.exhausted = True
                break
            budget.nodes_left -= 1
            path = directory / name
            children: list[ScriptNode] = []
            if is_folder:
                if depth < _MAX_DEPTH:
                    children = self._scan_quietly(path, root, depth + 1, budget)
                else:
                    budget.exhausted = True
            nodes.append(ScriptNode(
                name=name, path=self._relative(root, path), kind="folder" if is_folder else "file", children=children,
            ))
        return nodes

    def _scan_quietly(self, directory: Path, root: Path, depth: int, budget: _TreeBudget) -> list[ScriptNode]:
        """A sub-folder that cannot be read is listed as empty instead of failing the whole listing."""
        try:
            return self._scan(directory, root, depth, budget)
        except OSError as exc:
            logger.warning("Cannot list {}: {}", directory, exc)
            return []
