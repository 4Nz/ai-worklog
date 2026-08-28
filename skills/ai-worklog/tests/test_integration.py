from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import ai_worklog  # noqa: E402
from worklog.dossier import Dossier  # noqa: E402
from worklog.model import SessionRef  # noqa: E402
from worklog.obsidian import RECORDS_FOLDER  # noqa: E402


SESSION_ID = "01a040e7-4f68-7fd0-8804-588374eaa690"
ENV = {"CODEX_SESSION_ID": SESSION_ID}
NOW = "2026-08-27T10:00:00+08:00"


class IntegrationRunner:
    def __init__(self, git_root: Path):
        self.git_root = git_root
        self.remote = "https://git.example.com/pay/payment-api.git"

    def __call__(
        self, args: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        if command == ("obsidian", "version"):
            return subprocess.CompletedProcess(args, 0, "1.8.0\n", "")
        if command == ("git", "rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, str(self.git_root) + "\n", "")
        if command == ("git", "remote", "get-url", "origin"):
            return subprocess.CompletedProcess(args, 0, self.remote + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.project = self.root / "payment-api"
        self.project.mkdir()
        self.config = self.root / "config" / "config.yaml"
        self.obsidian_config = self.root / "config" / "obsidian.json"
        self.tokens = self.root / "tokens"
        self.runner = IntegrationRunner(self.project)

    def invoke_cli(
        self,
        args: Sequence[str],
        *,
        env: dict[str, str],
        token_root: Path,
    ) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = ai_worklog.main(
            list(args),
            env=env,
            run=self.runner,
            stdout=stdout,
            stderr=stderr,
            now=NOW,
            config_path=self.config,
            obsidian_config_path=self.obsidian_config,
            token_root=token_root,
        )
        payload = json.loads(stdout.getvalue() or stderr.getvalue())
        return code, payload

    def cli(self, *args: str, env=ENV) -> dict[str, object]:
        code, payload = self.invoke_cli(args, env=env, token_root=self.tokens)
        self.assertEqual(code, 0, payload)
        return payload

    def assert_agent_flow(
        self,
        *,
        env: dict[str, str],
        expected_agent: str,
        expected_rename_mode: str,
        expected_manual_rename_command: str | None,
        expected_resume: str,
        work_item_id: str = "REQ-123",
        assert_dossier_absent: bool = True,
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        session_id = env.get("CODEX_SESSION_ID") or env["AI_WORKLOG_SESSION_ID"]
        dossier_path = self.vault / RECORDS_FOLDER / f"{work_item_id}.md"
        code, prepared = self.invoke_cli(
            [
                "prepare-bind",
                "--work-item-id",
                work_item_id,
                "--session-title",
                "支付幂等",
                "--cwd",
                str(self.project),
                "--vault",
                str(self.vault),
                "--filesystem-fallback",
            ],
            env=env,
            token_root=self.tokens,
        )
        self.assertEqual(code, 0, prepared)
        self.assertEqual(prepared["agent_id"], expected_agent)

        code, staged = self.invoke_cli(
            [
                "stage-bind",
                "--prepared-token",
                str(prepared["prepared_token"]),
                "--topics-json",
                '["幂等", "callback"]',
                "--project-role",
                "payment owner",
                "--result",
                "design selected",
                "--next-step",
                "add tests",
                "--status",
                "进行中",
                "--summary-json",
                json.dumps(
                    {
                        "current_progress": "design selected",
                        "unresolved": "add tests",
                        "recommended_session": {
                            "agent_id": expected_agent,
                            "session_id": session_id,
                        },
                        "evidence_sessions": [
                            {"agent_id": expected_agent, "session_id": session_id}
                        ],
                    }
                ),
                "--filesystem-fallback",
            ],
            env=env,
            token_root=self.tokens,
        )
        self.assertEqual(code, 0, staged)
        self.assertEqual(staged["agent_id"], expected_agent)
        self.assertEqual(staged["rename_mode"], expected_rename_mode)
        self.assertIn("target_title", staged)
        self.assertIn("manual_rename_command", staged)
        self.assertEqual(
            staged["manual_rename_command"], expected_manual_rename_command
        )
        if assert_dossier_absent:
            self.assertFalse(dossier_path.exists())

        code, completed = self.invoke_cli(
            [
                "commit-bind",
                "--staged-token",
                str(staged["staged_token"]),
                "--rename-confirmed",
                "--filesystem-fallback",
            ],
            env=env,
            token_root=self.tokens,
        )
        self.assertEqual(code, 0, completed)
        self.assertTrue(dossier_path.is_file())
        dossier = Dossier.parse(dossier_path.read_text(encoding="utf-8"))
        self.assertIn(SessionRef(expected_agent, session_id), dossier.records)

        recalled = self.cli(
            "recall",
            "--work-item-id",
            work_item_id.lower(),
            "--vault",
            str(self.vault),
            "--filesystem-fallback",
            env=env,
        )
        queried = self.cli(
            "query",
            "--text",
            "payment-api",
            "--vault",
            str(self.vault),
            "--filesystem-fallback",
            env=env,
        )
        self.assertEqual(completed["resume_command"], expected_resume)
        self.assertEqual(recalled["sessions"][0]["agent_id"], expected_agent)  # type: ignore[index]
        self.assertEqual(recalled["sessions"][0]["session_id"], session_id)  # type: ignore[index]
        self.assertEqual(recalled["sessions"][0]["resume_command"], expected_resume)  # type: ignore[index]
        self.assertEqual(
            queried["groups"][0]["sessions"][0]["project"],  # type: ignore[index]
            "payment-api",
        )
        self.assertEqual(
            queried["groups"][0]["sessions"][0]["agent_id"],  # type: ignore[index]
            expected_agent,
        )
        return prepared, completed, recalled, queried

    def test_codex_bind_recall_query_flow(self):
        self.assert_agent_flow(
            env={"CODEX_SESSION_ID": "codex opaque"},
            expected_agent="codex",
            expected_rename_mode="automatic",
            expected_manual_rename_command=None,
            expected_resume="codex resume 'codex opaque'",
        )

    def test_claude_bind_recall_query_flow(self):
        self.assert_agent_flow(
            env={
                "AI_WORKLOG_AGENT": "claude-code",
                "AI_WORKLOG_SESSION_ID": "claude opaque",
            },
            expected_agent="claude-code",
            expected_rename_mode="manual",
            expected_manual_rename_command="/rename REQ-123 支付幂等",
            expected_resume="claude --resume 'claude opaque'",
        )

    def test_claude_without_session_hook_fails_before_token_or_dossier(self):
        token_root = self.root / "tokens"
        code, payload = self.invoke_cli(
            [
                "prepare-bind",
                "--work-item-id",
                "REQ-123",
                "--session-title",
                "design",
                "--cwd",
                str(self.project),
                "--vault",
                str(self.vault),
                "--filesystem-fallback",
            ],
            env={},
            token_root=token_root,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "unsupported_agent")
        self.assertFalse(token_root.exists())
        self.assertFalse((self.vault / "AI-Coding-Archive/WorkItems/REQ-123.md").exists())

    def test_fixture_recalls_five_sessions_across_two_projects(self):
        records = self.vault / RECORDS_FOLDER
        records.mkdir(parents=True)
        shutil.copyfile(
            FIXTURES / "two_project_five_sessions.md",
            records / "REQ-500.md",
        )

        recalled = self.cli(
            "recall",
            "--work-item-id",
            "REQ-500",
            "--vault",
            str(self.vault),
        )

        self.assertEqual(recalled["projects"], ["order-service", "payment-api"])
        self.assertEqual(len(recalled["sessions"]), 5)  # type: ignore[arg-type]
        self.assertEqual(
            [
                (item["agent_id"], item["session_id"], item["resume_command"])
                for item in recalled["sessions"]  # type: ignore[index]
            ],
            [
                ("codex", "0191f8c0-7a11-7000-8000-000000000005", "codex resume 0191f8c0-7a11-7000-8000-000000000005"),
                ("claude-code", "0191f8c0-7a11-7000-8000-000000000004", "claude --resume 0191f8c0-7a11-7000-8000-000000000004"),
                ("codex", "0191f8c0-7a11-7000-8000-000000000003", "codex resume 0191f8c0-7a11-7000-8000-000000000003"),
                ("claude-code", "0191f8c0-7a11-7000-8000-000000000002", "claude --resume 0191f8c0-7a11-7000-8000-000000000002"),
                ("codex", "0191f8c0-7a11-7000-8000-000000000001", "codex resume 0191f8c0-7a11-7000-8000-000000000001"),
            ],
        )

    def test_malformed_markers_fail_closed_as_structured_json(self):
        records = self.vault / RECORDS_FOLDER
        records.mkdir(parents=True)
        shutil.copyfile(
            FIXTURES / "malformed_markers.md",
            records / "BROKEN-1.md",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = ai_worklog.main(
            ["query", "--text", "BROKEN", "--vault", str(self.vault)],
            env=ENV,
            run=self.runner,
            stdout=stdout,
            stderr=stderr,
            now=NOW,
            config_path=self.config,
            obsidian_config_path=self.obsidian_config,
            token_root=self.tokens,
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error_code"], "invalid_arguments")
        self.assertIn("managed marker", payload["message"])

    def test_credential_remotes_are_never_persisted_or_returned(self):
        scenarios = json.loads(
            (FIXTURES / "credential_remotes.json").read_text(encoding="utf-8")
        )
        dossier_path = self.vault / RECORDS_FOLDER / "SEC-1.md"
        for scenario in scenarios:
            with self.subTest(remote=scenario["remote"]):
                self.runner.remote = scenario["remote"]
                prepared, completed, recalled, queried = self.assert_agent_flow(
                    env=ENV,
                    expected_agent="codex",
                    expected_rename_mode="automatic",
                    expected_manual_rename_command=None,
                    expected_resume=f"codex resume {SESSION_ID}",
                    work_item_id="SEC-1",
                    assert_dossier_absent=False,
                )
                text = dossier_path.read_text(encoding="utf-8")
                dossier = Dossier.parse(text)
                prepared_repository = prepared["project"]["repository"]  # type: ignore[index]
                self.assertEqual(prepared_repository, scenario["stored"])
                self.assertEqual(
                    dossier.records[SessionRef("codex", SESSION_ID)].repository,
                    scenario["stored"],
                )
                self.assertEqual(
                    recalled["sessions"][0]["session_id"],  # type: ignore[index]
                    SESSION_ID,
                )
                self.assertEqual(
                    queried["groups"][0]["sessions"][0]["session_id"],  # type: ignore[index]
                    SESSION_ID,
                )
                repository_text = prepared_repository or ""
                self.assertNotIn("@", repository_text)
                self.assertNotIn("?", repository_text)
                self.assertNotIn("#", repository_text)
                serialized = text + json.dumps(
                    (prepared, completed, recalled, queried), ensure_ascii=False
                )
                for forbidden in (
                    "user:",
                    "password",
                    "access_token",
                    "deploy-token",
                    "secret",
                    "fragment",
                ):
                    self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
