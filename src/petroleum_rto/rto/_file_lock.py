"""Crash-safe non-blocking writer locks for local RTO persistence."""

from __future__ import annotations

import errno
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockUnavailableError(RuntimeError):
    """Raised when another writer already owns the requested kernel lock."""


_HELD_PATHS: set[Path] = set()
_HELD_PATHS_GUARD = threading.Lock()


def _acquire_kernel_lock(descriptor: int, *, label: str) -> None:
    if os.name != "posix":  # pragma: no cover - supported execution images are POSIX
        raise RuntimeError(f"{label} writer locking requires a POSIX flock backend")
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise LockUnavailableError(f"{label} is locked by another writer") from exc
        raise


def _release_kernel_lock(descriptor: int) -> None:
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(path: Path, *, label: str) -> Iterator[None]:
    """Hold one persistent lock inode for the complete writer critical section."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent.resolve() / path.name
    with _HELD_PATHS_GUARD:
        if lock_path in _HELD_PATHS:
            raise LockUnavailableError(f"{label} is locked by another writer")
        _HELD_PATHS.add(lock_path)

    descriptor: int | None = None
    acquired = False
    try:
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"{label} lock must be a regular file")
        _acquire_kernel_lock(descriptor, label=label)
        acquired = True
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        if descriptor is not None:
            try:
                if acquired:
                    _release_kernel_lock(descriptor)
            finally:
                os.close(descriptor)
        with _HELD_PATHS_GUARD:
            _HELD_PATHS.discard(lock_path)


__all__ = ["LockUnavailableError", "exclusive_file_lock"]
