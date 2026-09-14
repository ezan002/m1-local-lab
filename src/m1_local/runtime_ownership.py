"""Process-lifetime ownership of local SQLite resources; no persistent lease rows."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class RuntimeOwnership:
    def __init__(self, databases: list[Path]) -> None:
        self.paths = sorted({path.resolve() for path in databases}, key=lambda path: str(path).casefold())
        self._handles: list[BinaryIO] = []

    def acquire(self) -> None:
        if self._handles:
            return
        try:
            for path in self.paths:
                # Never unlink this file: another process may have opened the same inode.
                lock_path = path.with_name(path.name + ".owner.lock")
                handle = lock_path.open("a+b")
                try:
                    if os.name == "nt":
                        import msvcrt

                        if not os.fstat(handle.fileno()).st_size:
                            handle.write(b"\0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    handle.close()
                    raise RuntimeError(
                        "database is owned by another runtime or cannot be locked; "
                        "stop the other API/CLI process before opening the same database"
                    ) from exc
                self._handles.append(handle)
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        while self._handles:
            # Closing releases the OS lock, including after process termination.
            self._handles.pop().close()
