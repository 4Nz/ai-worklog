from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class WorkItemBusy(RuntimeError):
    """Raised when another local process holds a worklog commit lock."""

    error_code = "work_item_busy"


@contextmanager
def archive_lock(
    vault_path: Path,
    *,
    lock_root: Path | None = None,
    timeout: float = 2.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    root = (
        lock_root or Path.home() / ".config" / "ai-worklog" / "locks"
    ).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(
        b"archive\0" + str(vault_path.resolve()).encode("utf-8")
    ).hexdigest()
    descriptor = os.open(root / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkItemBusy("work item archive is busy")
                time.sleep(poll_interval)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def work_item_lock(
    vault_path: Path,
    work_item_id: str,
    *,
    lock_root: Path | None = None,
    timeout: float = 2.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    root = (
        lock_root or Path.home() / ".config" / "ai-worklog" / "locks"
    ).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(
        str(vault_path.resolve()).encode("utf-8")
        + b"\0"
        + work_item_id.casefold().encode("utf-8")
    ).hexdigest()
    descriptor = os.open(root / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkItemBusy("work item is busy")
                time.sleep(poll_interval)
        yield
    finally:
        os.close(descriptor)
