import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HOOK = Path(__file__).resolve().parents[3] / "hooks" / "claude_session_start.py"


class ClaudeSessionHookTests(unittest.TestCase):
    def run_hook(self, payload: str, env_file: Path | None):
        env = os.environ.copy()
        if env_file is None:
            env.pop("CLAUDE_ENV_FILE", None)
        else:
            env["CLAUDE_ENV_FILE"] = str(env_file)
        return subprocess.run(
            [sys.executable, str(HOOK)], input=payload, text=True,
            capture_output=True, env=env, check=False,
        )

    def test_writes_shell_safe_identity_for_start_resume_and_fork_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            session_id = "id with 'quotes' and $HOME"
            for source in ("startup", "resume", "fork"):
                with self.subTest(source=source):
                    env_file = Path(temporary) / f"claude-env-{source}"
                    result = self.run_hook(json.dumps({"session_id": session_id, "source": source}), env_file)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    exports = env_file.read_text(encoding="utf-8")
                    self.assertIn("export AI_WORKLOG_AGENT=claude-code\n", exports)
                    shell = subprocess.run(
                        ["/bin/sh", "-c", f". {shlex.quote(str(env_file))}; printf '%s' \"$AI_WORKLOG_SESSION_ID\""],
                        text=True, capture_output=True, check=True,
                    )
                    self.assertEqual(shell.stdout, session_id)

    def test_malformed_input_and_missing_env_file_fail_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "claude-env"
            for payload, destination in (("not-json", env_file), ("{}", env_file), (json.dumps({"session_id": "ok"}), None)):
                with self.subTest(payload=payload, destination=destination):
                    result = self.run_hook(payload, destination)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
            self.assertFalse(env_file.exists())
