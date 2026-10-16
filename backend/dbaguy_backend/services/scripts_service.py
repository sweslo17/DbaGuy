import errno
import os
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from stat import S_ISDIR, S_ISREG

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


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


class ScriptsService:
    """The scripts folder: `.sql` files and folders below one root, never anything outside it.

    Every path a method takes goes through `_resolve`. Names that start with `.` (`.git` in a repository) are never
    used or listed, and files are only ever read, written, moved or deleted when they are regular `.sql` files, so a
    repository used as the scripts folder keeps everything else as it is.
    """

    def __init__(self, root: Callable[[], Path]) -> None:
        self._root = root

    def tree(self) -> ScriptTree:
        items, truncated = self._walk(self._root().resolve())
        if truncated:
            logger.warning(
                "The scripts folder is large: its listing stops at {} levels or {} entries", _MAX_DEPTH, _MAX_NODES
            )
        return ScriptTree(root=str(self._root()), items=items)

    def read(self, path: str) -> ScriptFile:
        root, target = self._resolve(path)
        self._require_sql(target)
        info = self._stat(target, path)
        if info is None:
            raise NotFound(f"{path} does not exist")
        self._require_regular(info, path)
        if info.st_size > _MAX_SCRIPT_BYTES:
            raise ValidationFailed(f"{path} is larger than 10 MB")
        try:
            data = target.read_bytes()  # bytes: reading as text would turn CRLF into LF
        except OSError as exc:
            raise self._io_error("read", path, exc) from exc
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed(f"{path} is not UTF-8 text") from exc
        return ScriptFile(path=self._relative(root, target), content=content, modified_at=self._modified_at(info))

    def write(self, path: str, content: str) -> ScriptFile:
        root, target = self._resolve(path)
        self._require_sql(target)
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:  # e.g. a lone surrogate
            raise ValidationFailed("The script is not valid text") from exc
        if size > _MAX_SCRIPT_BYTES:
            raise ValidationFailed("A script can be at most 10 MB")
        existing = self._stat(target, path)
        if existing is not None:
            self._require_regular(existing, path)
        self._make_parents(target, path)
        write_text_atomic(target, content, StorageError)
        written = self._stat(target, path)
        if written is None:  # removed again right after it was written
            raise NotFound(f"{path} does not exist")
        relative = self._relative(root, target)
        logger.info("Saved script {}", relative)
        return ScriptFile(path=relative, content=content, modified_at=self._modified_at(written))

    def rename(self, source: str, target: str) -> None:
        root, source_path = self._resolve(source)
        _, target_path = self._resolve(target)
        source_info = self._stat(source_path, source)
        if source_info is None:
            raise NotFound(f"{source} does not exist")
        target_info = self._stat(target_path, target)
        # Never overwrite. On a file system that ignores case, `Report.sql` -> `report.sql` finds "itself" at the
        # target and is a plain rename.
        if target_info is not None and (target_path == source_path or not os.path.samestat(source_info, target_info)):
            raise AlreadyExists(f"{target} already exists")
        if S_ISDIR(source_info.st_mode):
            if target_path.is_relative_to(source_path):
                raise ValidationFailed("A folder cannot be moved into itself")
        else:
            self._require_regular(source_info, source)
            self._require_sql(source_path)
            self._require_sql(target_path)
        self._make_parents(target_path, target)
        try:
            source_path.rename(target_path)
        except OSError as exc:
            raise self._io_error("rename", source, exc) from exc
        logger.info("Renamed {} to {}", self._relative(root, source_path), self._relative(root, target_path))

    def delete(self, path: str) -> None:
        root, target = self._resolve(path)
        info = self._stat(target, path)
        if info is None:
            raise NotFound(f"{path} does not exist")
        if not S_ISDIR(info.st_mode):
            self._require_sql(target)
            self._require_regular(info, path)
        try:
            if S_ISDIR(info.st_mode):
                target.rmdir()  # only an empty folder: files this list does not show are not ours to delete
            else:
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
        except NotADirectoryError as exc:
            raise AlreadyExists(f"A file is in the way of {path}") from exc
        except OSError as exc:
            raise self._io_error("create", path, exc) from exc
        logger.info("Created folder {}", self._relative(root, target))

    def _resolve(self, relative: str) -> tuple[Path, Path]:
        """(root, target) — both resolved; rejects anything that is not strictly inside the root, and any name that
        starts with `.` (also when only a link leads there)."""
        if not relative or "\\" in relative or "\0" in relative or relative.startswith("/"):
            raise ValidationFailed("Script paths are relative to the scripts folder, separated by '/'")
        parts = PurePosixPath(relative).parts
        if ".." in parts:
            raise ValidationFailed("Script paths cannot contain '..'")
        if any(map(_is_hidden, parts)):
            raise ValidationFailed("Names in script paths cannot start with '.'")
        root = self._root().resolve()
        target = root.joinpath(*parts).resolve()  # follows symlinks: a link pointing outside is caught below
        if target == root or not target.is_relative_to(root):
            raise ValidationFailed(f"{relative} is outside the scripts folder")
        if any(map(_is_hidden, target.relative_to(root).parts)):
            raise ValidationFailed(f"{relative} leads to a name that starts with '.'")
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
    def _require_regular(info: os.stat_result, path: str) -> None:
        if S_ISDIR(info.st_mode):
            raise ValidationFailed(f"{path} is a folder")
        if not S_ISREG(info.st_mode):
            raise ValidationFailed(f"{path} is not a regular file")  # a pipe or socket: opening it could block

    @staticmethod
    def _modified_at(info: os.stat_result) -> datetime:
        return datetime.fromtimestamp(info.st_mtime, tz=UTC)

    def _stat(self, target: Path, path: str) -> os.stat_result | None:
        """The resolved `target`'s stat, or None when nothing is there: it is missing, one of its parents is a file,
        or it is a link that goes nowhere."""
        try:
            return target.stat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None
            raise self._io_error("look up", path, exc) from exc

    def _make_parents(self, target: Path, path: str) -> None:
        """Create the folders above `target` (they are inside the root, because `target` is)."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as exc:
            raise AlreadyExists(f"A file is in the way of {path}") from exc
        except OSError as exc:
            raise self._io_error("create the folders of", path, exc) from exc

    @staticmethod
    def _io_error(action: str, path: str, exc: OSError) -> DomainError:
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            return NotFound(f"{path} does not exist")
        if exc.errno == errno.ENAMETOOLONG:
            return ValidationFailed(f"A name in {path} is too long")
        logger.error("Cannot {} script {}: {}", action, path, exc)
        return StorageError(f"Cannot {action} {path}: {exc.strerror or exc}")

    def _walk(self, root: Path) -> tuple[list[ScriptNode], bool]:
        """The listing, breadth first, so that when a limit is reached the shallow entries are all there and only the
        deepest ones are missing. Also whether anything was left out."""
        items: list[ScriptNode] = []
        pending = deque([(root, 1, items)])  # (folder to list, its depth, the list its entries go into)
        nodes_left = _MAX_NODES
        truncated = False
        while pending:
            folder, depth, into = pending.popleft()
            for name, is_folder in self._entries(folder, root):
                if nodes_left == 0:
                    return items, True  # an entry that does not fit: what else is left does not matter
                nodes_left -= 1
                path = folder / name
                node = ScriptNode(
                    name=name, path=self._relative(root, path), kind="folder" if is_folder else "file", children=[]
                )
                into.append(node)
                if not is_folder:
                    continue
                if depth < _MAX_DEPTH:
                    pending.append((path, depth + 1, node.children))
                elif self._entries(path, root):  # the deepest level shown: is there anything below it?
                    truncated = True
        return items, truncated

    def _entries(self, folder: Path, root: Path) -> list[tuple[str, bool]]:
        """The folders and `.sql` files directly in `folder` as (name, is a folder), folders first, then by name.
        Hidden names and symlinks are skipped. A sub-folder that cannot be read counts as empty, the root cannot."""
        found: list[tuple[str, bool]] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if _is_hidden(entry.name):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        found.append((entry.name, True))
                    elif entry.is_file(follow_symlinks=False) and _is_sql(entry.name):
                        found.append((entry.name, False))
        except OSError as exc:
            if folder == root:
                logger.error("Cannot list the scripts folder {}: {}", root, exc)
                raise StorageError(f"Cannot list the scripts folder: {exc.strerror or exc}") from exc
            logger.warning("Cannot list {}: {}", folder, exc)
            return []
        found.sort(key=lambda item: (not item[1], item[0].lower(), item[0]))
        return found
