from __future__ import annotations

import hashlib
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.locking import WorkItemBusy, work_item_lock  # noqa: E402


def hold_lock(
    vault: str,
    item: str,
    root: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with work_item_lock(
        Path(vault), item, lock_root=Path(root), timeout=1.0
    ):
        entered.set()
        release.wait(5)


class WorkItemLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "private-vault-name"
        self.vault.mkdir()
        self.lock_root = self.root / "locks"
        self.context = multiprocessing.get_context("spawn")
        self.entered = self.context.Event()
        self.release = self.context.Event()

    def start_holder(self, item: str = "REQ-123") -> multiprocessing.Process:
        process = self.context.Process(
            target=hold_lock,
            args=(
                str(self.vault),
                item,
                str(self.lock_root),
                self.entered,
                self.release,
            ),
        )
        process.start()
        self.assertTrue(self.entered.wait(2))
        return process

    def release_holder(self, process: multiprocessing.Process) -> None:
        self.release.set()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(2)
        self.assertEqual(process.exitcode, 0)

    def test_second_process_times_out_on_same_casefolded_item(self):
        process = self.start_holder()
        try:
            with self.assertRaises(WorkItemBusy) as caught:
                with work_item_lock(
                    self.vault,
                    "req-123",
                    lock_root=self.lock_root,
                    timeout=0.1,
                    poll_interval=0.01,
                ):
                    self.fail("contended lock was acquired")
        finally:
            self.release_holder(process)

        self.assertEqual(caught.exception.error_code, "work_item_busy")

    def test_different_work_items_do_not_contend(self):
        process = self.start_holder()
        acquired = False
        try:
            with work_item_lock(
                self.vault,
                "REQ-456",
                lock_root=self.lock_root,
                timeout=0.1,
                poll_interval=0.01,
            ):
                acquired = True
        finally:
            self.release_holder(process)

        self.assertTrue(acquired)

    def test_lock_filename_is_only_the_scoped_digest(self):
        work_item_id = "SECRET-REQ-123"
        with work_item_lock(
            self.vault, work_item_id, lock_root=self.lock_root
        ):
            lock_files = tuple(self.lock_root.iterdir())

        expected = hashlib.sha256(
            str(self.vault.resolve()).encode("utf-8")
            + b"\0"
            + work_item_id.casefold().encode("utf-8")
        ).hexdigest()
        self.assertEqual([path.name for path in lock_files], [f"{expected}.lock"])
        self.assertNotIn(self.vault.name.casefold(), lock_files[0].name.casefold())
        self.assertNotIn(work_item_id.casefold(), lock_files[0].name.casefold())


if __name__ == "__main__":
    unittest.main()
