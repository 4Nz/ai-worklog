from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.adapters.codex import RenameError, rename_thread_via_app_server  # noqa: E402


SESSION_ID = "01a040e7-4f68-7fd0-8804-588374eaa690"


class CodexAppServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fake_server(self, mode: str) -> tuple[str, ...]:
        script = self.root / f"fake-{mode}.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import json
                import sys

                mode = {mode!r}
                initialized = json.loads(sys.stdin.readline())
                print(json.dumps({{"id": initialized["id"], "result": {{}}}}), flush=True)
                notification = json.loads(sys.stdin.readline())
                assert notification["method"] == "initialized"
                renamed = json.loads(sys.stdin.readline())
                if mode == "set-error":
                    print(json.dumps({{"id": renamed["id"], "error": {{"code": -32600, "message": "private path /tmp/secret"}}}}), flush=True)
                    sys.exit(0)
                print(json.dumps({{"id": renamed["id"], "result": {{}}}}), flush=True)
                read = json.loads(sys.stdin.readline())
                title = renamed["params"]["name"] if mode in ("success", "id-mismatch") else "wrong title"
                thread_id = "0191f8c0-7a11-7000-8000-000000000005" if mode == "id-mismatch" else renamed["params"]["threadId"]
                print(json.dumps({{"id": read["id"], "result": {{"thread": {{"id": thread_id, "name": title}}}}}}), flush=True)
                """
            ),
            encoding="utf-8",
        )
        return (sys.executable, str(script))

    def test_renames_and_verifies_exact_title_through_json_rpc(self):
        rename_thread_via_app_server(
            SESSION_ID,
            "REQ-123 design",
            command=self.fake_server("success"),
            timeout=2,
        )

    def test_server_error_is_sanitized(self):
        with self.assertRaisesRegex(RenameError, "task rename failed") as caught:
            rename_thread_via_app_server(
                SESSION_ID,
                "REQ-123 design",
                command=self.fake_server("set-error"),
                timeout=2,
            )
        self.assertNotIn("/tmp/secret", str(caught.exception))

    def test_read_back_name_mismatch_fails(self):
        with self.assertRaisesRegex(RenameError, "verification"):
            rename_thread_via_app_server(
                SESSION_ID,
                "REQ-123 design",
                command=self.fake_server("mismatch"),
                timeout=2,
            )

    def test_read_back_thread_id_mismatch_fails(self):
        with self.assertRaisesRegex(RenameError, "verification"):
            rename_thread_via_app_server(
                SESSION_ID,
                "REQ-123 design",
                command=self.fake_server("id-mismatch"),
                timeout=2,
            )

    def test_early_process_exit_fails_without_raw_process_details(self):
        command = (sys.executable, "-c", "import sys; sys.exit(7)")
        with self.assertRaisesRegex(RenameError, "task rename failed") as caught:
            rename_thread_via_app_server(
                SESSION_ID,
                "REQ-123 design",
                command=command,
                timeout=1,
            )
        self.assertNotIn("exit 7", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
