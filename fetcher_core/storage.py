from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol


class StoragePathError(ValueError):
    pass


class TargetExistsError(FileExistsError):
    pass


@dataclass(frozen=True)
class StorageEntry:
    relpath: PurePosixPath
    size: int | None
    modified: str = ""


class StorageBackend(Protocol):
    def list(self, rel_dir: PurePosixPath = PurePosixPath()) -> list[StorageEntry]: ...
    def stat(self, relpath: PurePosixPath) -> StorageEntry: ...
    def copy_in(self, source: Path, relpath: PurePosixPath, *, overwrite: bool = False) -> StorageEntry: ...
    def copy_out(self, relpath: PurePosixPath, destination: Path, *, overwrite: bool = False) -> Path: ...
    def rename(self, relpath: PurePosixPath, filename: str, *, overwrite: bool = False) -> StorageEntry: ...
    def move(self, source: PurePosixPath, target: PurePosixPath, *, overwrite: bool = False) -> StorageEntry: ...
    def delete(self, relpath: PurePosixPath) -> None: ...
    def materialize_preview(self, relpath: PurePosixPath) -> Path: ...


def _validate_relpath(value: PurePosixPath, *, allow_empty: bool = False) -> PurePosixPath:
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise StoragePathError("storage paths may not contain control characters")
    if value.is_absolute() or any(part in {".", ".."} for part in value.parts):
        raise StoragePathError(f"unsafe storage path: {value}")
    if not allow_empty and (not text or text == "."):
        raise StoragePathError("storage path is required")
    return value


def _validate_filename(filename: str) -> str:
    _validate_relpath(PurePosixPath(filename))
    if PurePosixPath(filename).name != filename or "/" in filename or "\\" in filename:
        raise StoragePathError("rename requires a filename, not a path")
    return filename


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._root = self.root.resolve()

    def _path(self, relpath: PurePosixPath, *, allow_empty: bool = False) -> Path:
        relpath = _validate_relpath(relpath, allow_empty=allow_empty)
        candidate = (self._root / Path(*relpath.parts)).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise StoragePathError(f"path escapes storage root: {relpath}") from exc
        return candidate

    def _entry(self, path: Path) -> StorageEntry:
        stat = path.stat()
        return StorageEntry(PurePosixPath(path.relative_to(self._root).as_posix()), stat.st_size, str(stat.st_mtime))

    def list(self, rel_dir: PurePosixPath = PurePosixPath()) -> list[StorageEntry]:
        directory = self._path(rel_dir, allow_empty=True)
        if not directory.exists():
            return []
        entries: list[StorageEntry] = []
        for path in directory.iterdir():
            if not path.is_file():
                continue
            try:
                path.resolve().relative_to(self._root)
            except ValueError:
                continue
            entries.append(self._entry(path))
        return sorted(entries, key=lambda entry: str(entry.relpath))

    def stat(self, relpath: PurePosixPath) -> StorageEntry:
        path = self._path(relpath)
        if not path.is_file():
            raise FileNotFoundError(path)
        return self._entry(path)

    @staticmethod
    def _publish_no_replace(staged: Path, target: Path) -> None:
        try:
            os.link(staged, target)
        except FileExistsError as exc:
            raise TargetExistsError(target) from exc
        staged.unlink()

    def copy_in(self, source: Path, relpath: PurePosixPath, *, overwrite: bool = False) -> StorageEntry:
        target = self._path(relpath)
        if target.exists() and not overwrite:
            raise TargetExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        try:
            shutil.copy2(source, temporary)
            if overwrite:
                os.replace(temporary, target)
            else:
                self._publish_no_replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self._entry(target)

    def copy_out(self, relpath: PurePosixPath, destination: Path, *, overwrite: bool = False) -> Path:
        source = self._path(relpath)
        if destination.exists() and not overwrite:
            raise TargetExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
        try:
            shutil.copy2(source, temporary)
            if overwrite:
                os.replace(temporary, destination)
            else:
                self._publish_no_replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def rename(self, relpath: PurePosixPath, filename: str, *, overwrite: bool = False) -> StorageEntry:
        source = self._path(relpath)
        filename = _validate_filename(filename)
        target = self._path(relpath.parent / filename)
        if target.exists() and not overwrite:
            raise TargetExistsError(target)
        if overwrite:
            os.replace(source, target)
        else:
            self._publish_no_replace(source, target)
        return self._entry(target)

    def move(self, source: PurePosixPath, target: PurePosixPath, *, overwrite: bool = False) -> StorageEntry:
        source_path = self._path(source)
        target_path = self._path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and not overwrite:
            raise TargetExistsError(target_path)
        if overwrite:
            os.replace(source_path, target_path)
        else:
            self._publish_no_replace(source_path, target_path)
        return self._entry(target_path)

    def delete(self, relpath: PurePosixPath) -> None:
        self._path(relpath).unlink()

    def materialize_preview(self, relpath: PurePosixPath) -> Path:
        path = self._path(relpath)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path


Runner = Callable[[list[str], str, float], subprocess.CompletedProcess[str]]


def _default_runner(argv: list[str], batch: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, input=batch, text=True, capture_output=True, timeout=timeout, check=False)


class SftpStorage:
    def __init__(self, host: str, root: PurePosixPath, *, user: str = "", port: int | None = None,
                 runner: Runner = _default_runner, operation_timeout: float = 120) -> None:
        if (
            not host
            or host.startswith("-")
            or user.startswith("-")
            or "@" in host
            or "@" in user
            or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in host + user)
        ):
            raise StoragePathError("invalid SFTP host or user")
        if port is not None and not 1 <= port <= 65535:
            raise StoragePathError("invalid SFTP port")
        if not root.is_absolute() or any(part in {".", ".."} for part in root.parts):
            raise StoragePathError("SFTP root must be a safe absolute path")
        self.host, self.root, self.user, self.port = host, root, user, port
        self.runner, self.operation_timeout = runner, operation_timeout

    @property
    def identity(self) -> str:
        return f"sftp:{self.user + '@' if self.user else ''}{self.host}:{self.port or 22}{self.root}"

    def _remote(self, relpath: PurePosixPath, *, allow_empty: bool = False) -> str:
        relpath = _validate_relpath(relpath, allow_empty=allow_empty)
        return str(self.root if str(relpath) in {"", "."} else self.root / relpath)

    @staticmethod
    def _quote(value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise StoragePathError("SFTP values may not contain control characters")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _run(self, commands: list[str]) -> subprocess.CompletedProcess[str]:
        target = f"{self.user}@{self.host}" if self.user else self.host
        argv = ["sftp", "-q", "-oBatchMode=yes", "-oConnectTimeout=8", "-oConnectionAttempts=1"]
        if self.port is not None:
            argv.extend(["-P", str(self.port)])
        argv.append(target)
        proc = self.runner(argv, "\n".join(commands) + "\n", self.operation_timeout)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "SFTP operation failed")
        return proc

    def list(self, rel_dir: PurePosixPath = PurePosixPath()) -> list[StorageEntry]:
        remote = self._remote(rel_dir, allow_empty=True)
        proc = self._run([f"ls -l {self._quote(remote)}"])
        entries: list[StorageEntry] = []
        for line in proc.stdout.splitlines():
            parts = line.split(maxsplit=8)
            if len(parts) < 9 or parts[0].startswith("d"):
                continue
            name = PurePosixPath(parts[8]).name
            relpath = (rel_dir / name) if str(rel_dir) not in {"", "."} else PurePosixPath(name)
            entries.append(StorageEntry(relpath, int(parts[4]), " ".join(parts[5:8])))
        return sorted(entries, key=lambda entry: str(entry.relpath))

    def stat(self, relpath: PurePosixPath) -> StorageEntry:
        for entry in self.list(relpath.parent):
            if entry.relpath == relpath:
                return entry
        raise FileNotFoundError(str(relpath))

    def _exists(self, relpath: PurePosixPath) -> bool:
        return any(entry.relpath == relpath for entry in self.list(relpath.parent))

    def copy_in(self, source: Path, relpath: PurePosixPath, *, overwrite: bool = False) -> StorageEntry:
        remote = self._remote(relpath)
        exists = self._exists(relpath)
        if exists and not overwrite:
            raise TargetExistsError(remote)
        temporary = f"{remote}.{uuid.uuid4().hex}.partial"
        backup = f"{remote}.{uuid.uuid4().hex}.backup"
        backed_up = False
        try:
            self._run([f"put {self._quote(str(source))} {self._quote(temporary)}"])
            if exists and overwrite:
                self._run([f"rename {self._quote(remote)} {self._quote(backup)}"])
                backed_up = True
            self._run([f"rename {self._quote(temporary)} {self._quote(remote)}"])
        except Exception:
            if backed_up:
                try:
                    self._run([f"rename {self._quote(backup)} {self._quote(remote)}"])
                except Exception:
                    pass
            try:
                self._run([f"rm {self._quote(temporary)}"])
            except Exception:
                pass
            raise
        if exists and overwrite:
            try:
                self._run([f"rm {self._quote(backup)}"])
            except RuntimeError:
                pass
        return StorageEntry(relpath, source.stat().st_size)

    def copy_out(self, relpath: PurePosixPath, destination: Path, *, overwrite: bool = False) -> Path:
        remote = self._remote(relpath)
        if destination.exists() and not overwrite:
            raise TargetExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
        try:
            self._run([f"get {self._quote(remote)} {self._quote(str(temporary))}"])
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def rename(self, relpath: PurePosixPath, filename: str, *, overwrite: bool = False) -> StorageEntry:
        filename = _validate_filename(filename)
        target_relpath = relpath.parent / filename
        source, target = self._remote(relpath), self._remote(target_relpath)
        exists = self._exists(target_relpath)
        if exists and not overwrite:
            raise TargetExistsError(target)
        backup = f"{target}.{uuid.uuid4().hex}.backup"
        backed_up = False
        try:
            if exists and overwrite:
                self._run([f"rename {self._quote(target)} {self._quote(backup)}"])
                backed_up = True
            self._run([f"rename {self._quote(source)} {self._quote(target)}"])
        except Exception:
            if backed_up:
                try:
                    self._run([f"rename {self._quote(backup)} {self._quote(target)}"])
                except Exception:
                    pass
            raise
        if exists and overwrite:
            try:
                self._run([f"rm {self._quote(backup)}"])
            except RuntimeError:
                pass
        return StorageEntry(target_relpath, None)

    def move(self, source: PurePosixPath, target: PurePosixPath, *, overwrite: bool = False) -> StorageEntry:
        source_remote, target_remote = self._remote(source), self._remote(target)
        exists = self._exists(target)
        if exists and not overwrite:
            raise TargetExistsError(target_remote)
        backup = f"{target_remote}.{uuid.uuid4().hex}.backup"
        backed_up = False
        try:
            if exists and overwrite:
                self._run([f"rename {self._quote(target_remote)} {self._quote(backup)}"])
                backed_up = True
            self._run([f"rename {self._quote(source_remote)} {self._quote(target_remote)}"])
        except Exception:
            if backed_up:
                try:
                    self._run([f"rename {self._quote(backup)} {self._quote(target_remote)}"])
                except Exception:
                    pass
            raise
        if exists and overwrite:
            try:
                self._run([f"rm {self._quote(backup)}"])
            except RuntimeError:
                pass
        return StorageEntry(target, None)

    def delete(self, relpath: PurePosixPath) -> None:
        self._run([f"rm {self._quote(self._remote(relpath))}"])

    def materialize_preview(self, relpath: PurePosixPath) -> Path:
        suffix = relpath.suffix
        fd, name = tempfile.mkstemp(prefix="fetcher-preview-", suffix=suffix)
        os.close(fd)
        path = Path(name)
        path.unlink()
        try:
            return self.copy_out(relpath, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
