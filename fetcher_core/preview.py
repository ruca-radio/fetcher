from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable


class PreviewCache:
    def __init__(self, root: Path, *, max_bytes: int, max_age: float,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_age, self.clock = max_bytes, max_age, clock
        self._active: set[Path] = set()
        self._lock = threading.RLock()
        self._key_locks: dict[Path, threading.Lock] = {}

    def _path(self, storage_identity: str, relpath: PurePosixPath) -> Path:
        normalized = str(PurePosixPath(*relpath.parts))
        digest = hashlib.sha256(f"{storage_identity}\0{normalized}".encode()).hexdigest()
        suffix = relpath.suffix if relpath.suffix and len(relpath.suffix) <= 16 else ""
        return self.root / f"{digest}{suffix}"

    def materialize(self, storage_identity: str, relpath: PurePosixPath,
                    producer: Callable[[Path], object]) -> Path:
        final = self._path(storage_identity, relpath)
        with self._lock:
            key_lock = self._key_locks.setdefault(final, threading.Lock())
        with key_lock:
            if final.exists():
                os.utime(final, (self.clock(), self.clock()))
                return final
            partial = self.root / f".{final.name}.{uuid.uuid4().hex}.partial"
            with self._lock:
                self._active.add(partial)
            try:
                producer(partial)
                if not partial.is_file():
                    raise RuntimeError("preview producer did not create a file")
                os.replace(partial, final)
                os.utime(final, (self.clock(), self.clock()))
                with self._lock:
                    self._active.add(final)
                self.cleanup()
                return final
            finally:
                with self._lock:
                    self._active.discard(partial)
                    self._active.discard(final)
                partial.unlink(missing_ok=True)

    def cleanup(self) -> None:
        now = self.clock()
        with self._lock:
            all_files = [path for path in self.root.iterdir() if path.is_file() and not path.name.endswith(".partial")]
            files = [path for path in all_files if path not in self._active]
            for path in files:
                if now - path.stat().st_mtime > self.max_age:
                    path.unlink(missing_ok=True)
            files = [path for path in files if path.exists()]
            total = sum(path.stat().st_size for path in all_files if path.exists())
            for path in sorted(files, key=lambda item: (item.stat().st_mtime, item.name)):
                if total <= self.max_bytes:
                    break
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                total -= size
