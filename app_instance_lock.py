"""Cross-process singleton lock for the local dashboard server."""

from __future__ import annotations

import os
import threading
from pathlib import Path


class AppInstanceAlreadyRunning(RuntimeError):
    """Raised when another process already owns the application data directory."""


_local_guard = threading.Lock()
_local_locks: set[str] = set()


class AppInstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._key = os.path.normcase(os.path.abspath(self.path))
        self._handle = None
        self._backend = ""

    def acquire(self) -> "AppInstanceLock":
        with _local_guard:
            if self._key in _local_locks:
                raise AppInstanceAlreadyRunning("DHB Reup YouTube Studio đã chạy trong tiến trình này.")
            _local_locks.add(self._key)

        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")  # noqa: SIM115 - retain the handle until release() unlocks it
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                self._backend = "msvcrt"
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._backend = "fcntl"

            self._handle = handle
            return self
        except (OSError, BlockingIOError) as exc:
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
            with _local_guard:
                _local_locks.discard(self._key)
            if handle is None:
                raise
            raise AppInstanceAlreadyRunning(
                "DHB Reup YouTube Studio đã chạy ở tiến trình khác; không khởi tạo DB lần hai."
            ) from exc
        except Exception:
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
            with _local_guard:
                _local_locks.discard(self._key)
            raise

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if self._backend == "msvcrt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._backend == "fcntl":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            with _local_guard:
                _local_locks.discard(self._key)

    def __enter__(self) -> "AppInstanceLock":  # noqa: PYI034 - runtime context manager returns the lock owner
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_app_instance_lock(path: Path) -> AppInstanceLock:
    return AppInstanceLock(path).acquire()
