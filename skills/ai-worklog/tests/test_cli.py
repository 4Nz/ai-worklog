from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_worklog  # noqa: E402
from worklog.adapters.codex import RenameError  # noqa: E402
from worklog.agent import AmbiguousAgent, UnsupportedAgent  # noqa: E402
from worklog.obsidian import ObsidianState, load_config  # noqa: E402
from worklog.operations import PartialBindFailure, TokenError  # noqa: E402


SESSION_ID = "01a040e7-4f68-7fd0-8804-588374eaa690"
ENV = {"CODEX_SESSION_ID": SESSION_ID}
CLAUDE_ENV = {
    "AI_WORKLOG_AGENT": "claude-code",
    "AI_WORKLOG_SESSION_ID": "claude-1",
}
NOW = "2026-08-27T10:00:00+08:00"


class FakeRunner:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, args: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append(command)
        if command == ("obsidian", "version"):
            return subprocess.CompletedProcess(args, 0, "1.8.0\n", "")
        if command == ("git", "rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, str(cwd) + "\n", "")
        if command == ("git", "remote", "get-url", "origin"):
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 1, "", "")


class MissingRunner:
    def __call__(self, args: Sequence[str], cwd: Path | None):
        raise FileNotFoundError("command unavailable")


class DisabledCliRunner(FakeRunner):
    def __call__(self, args: Sequence[str], cwd: Path | None):
        if tuple(args) == ("obsidian", "version"):
            self.calls.append(tuple(args))
            return subprocess.CompletedProcess(args, 1, "CLI is not enabled\n", "")
        return super().__call__(args, cwd)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config_path = self.root / "codex" / "config.yaml"
        self.obsidian_config = self.root / "obsidian.json"
        self.token_root = self.root / "tokens"
        self.runner = FakeRunner()

    def run_cli(self, *args: str, env=ENV):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = ai_worklog.main(
            list(args),
            env=env,
            run=self.runner,
            stdout=stdout,
            stderr=stderr,
            now=NOW,
            config_path=self.config_path,
            obsidian_config_path=self.obsidian_config,
            token_root=self.token_root,
            rename_backend=getattr(self, "rename_backend", None),
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def stage_cli(self, *, env=ENV):
        code, stdout, stderr = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "design",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
            env=env,
        )
        self.assertEqual((code, stderr), (0, ""))
        prepared = json.loads(stdout)
        agent_id = "claude-code" if env == CLAUDE_ENV else "codex"
        session_id = env.get("AI_WORKLOG_SESSION_ID", SESSION_ID)
        code, stdout, stderr = self.run_cli(
            "stage-bind",
            "--prepared-token",
            prepared["prepared_token"],
            "--topics-json",
            "[]",
            "--project-role",
            "owner",
            "--result",
            "selected",
            "--next-step",
            "implement",
            "--status",
            "进行中",
            "--summary-json",
            json.dumps(
                {
                    "current_progress": "selected",
                    "unresolved": "none",
                    "recommended_session": {
                        "agent_id": agent_id,
                        "session_id": session_id,
                    },
                    "evidence_sessions": [
                        {"agent_id": agent_id, "session_id": session_id}
                    ],
                }
            ),
            env=env,
        )
        self.assertEqual((code, stderr), (0, ""))
        return json.loads(stdout)

    def test_cli_emits_machine_readable_prepare_success(self):
        code, stdout, stderr = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "设计",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent_id"], "codex")
        self.assertEqual(payload["session_id"], SESSION_ID)
        self.assertEqual(payload["target_title"], "REQ-1 设计")
        self.assertIn("prepared_token", payload)
        self.assertFalse(any(command[0] == "codex" for command in self.runner.calls))

    def test_default_config_is_agent_neutral(self):
        self.assertEqual(
            ai_worklog.DEFAULT_CONFIG_PATH,
            Path.home() / ".config" / "ai-worklog" / "config.yaml",
        )

    def test_prepare_maps_unsupported_and_ambiguous_agents(self):
        environments = (
            ({}, UnsupportedAgent.error_code),
            (
                {
                    "CODEX_SESSION_ID": SESSION_ID,
                    **CLAUDE_ENV,
                },
                AmbiguousAgent.error_code,
            ),
        )
        for environment, expected_code in environments:
            with self.subTest(expected_code=expected_code):
                code, stdout, stderr = self.run_cli(
                    "prepare-bind",
                    "--work-item-id",
                    "REQ-1",
                    "--session-title",
                    "design",
                    "--cwd",
                    str(self.root / "payment-api"),
                    "--vault",
                    str(self.vault),
                    env=environment,
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["error_code"], expected_code)

    def test_commit_without_confirmation_keeps_staged_token_and_dossier_unwritten(self):
        staged = self.stage_cli()

        code, stdout, stderr = self.run_cli(
            "commit-bind", "--staged-token", staged["staged_token"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "rename_required")
        self.assertTrue(Path(staged["staged_token"]).is_file())
        self.assertFalse(
            (self.vault / "AI-Coding-Archive/WorkItems/REQ-1.md").exists()
        )

    def test_claude_rename_thread_never_calls_codex_backend(self):
        staged = self.stage_cli(env=CLAUDE_ENV)
        self.rename_backend = mock.Mock()

        code, stdout, stderr = self.run_cli(
            "rename-thread",
            "--staged-token",
            staged["staged_token"],
            env=CLAUDE_ENV,
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "rename_required")
        self.rename_backend.assert_not_called()
        self.assertTrue(Path(staged["staged_token"]).is_file())

    def test_stage_rejects_legacy_summary_session_strings(self):
        code, stdout, stderr = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "design",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
        )
        self.assertEqual((code, stderr), (0, ""))
        prepared = json.loads(stdout)

        code, stdout, stderr = self.run_cli(
            "stage-bind",
            "--prepared-token",
            prepared["prepared_token"],
            "--topics-json",
            "[]",
            "--project-role",
            "owner",
            "--result",
            "selected",
            "--next-step",
            "implement",
            "--status",
            "进行中",
            "--summary-json",
            json.dumps(
                {
                    "current_progress": "selected",
                    "unresolved": "none",
                    "recommended_session_id": SESSION_ID,
                    "evidence_session_ids": [SESSION_ID],
                }
            ),
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "invalid_arguments")

    def test_cli_emits_one_json_validation_error(self):
        code, stdout, stderr = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "中文",
            "--session-title",
            "设计",
            "--cwd",
            str(self.root),
            "--vault",
            str(self.vault),
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "invalid_arguments")
        self.assertEqual(stderr.count("\n"), 1)

    def test_prepare_validates_arguments_before_obsidian_availability(self):
        self.runner = MissingRunner()  # type: ignore[assignment]
        with mock.patch.object(
            ai_worklog,
            "detect_obsidian",
            return_value=ObsidianState(
                installed=False,
                cli_present=False,
                cli_status="temporarily_unavailable",
            ),
        ):
            code, stdout, stderr = self.run_cli(
                "prepare-bind",
                "--work-item-id",
                "中文",
                "--session-title",
                "设计",
                "--cwd",
                str(self.root),
                "--vault",
                str(self.vault),
            )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("work item", json.loads(stderr)["message"])

    def test_exact_recall_miss_is_structured_not_found(self):
        code, stdout, stderr = self.run_cli(
            "recall",
            "--work-item-id",
            "REQ-404",
            "--vault",
            str(self.vault),
        )
        payload = json.loads(stderr)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(payload["error_code"], "not_found")
        self.assertIn("REQ-404", payload["message"])

    def test_argparse_errors_are_json_instead_of_usage_text(self):
        code, stdout, stderr = self.run_cli("prepare-bind")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "invalid_arguments")

    def test_configure_caches_only_a_discovered_vault(self):
        self.obsidian_config.write_text(
            json.dumps({"vaults": {"selected": {"path": str(self.vault)}}}),
            encoding="utf-8",
        )
        code, stdout, stderr = self.run_cli(
            "configure", "--vault-path", str(self.vault)
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["ok"])
        configured = load_config(self.config_path)
        self.assertIsNotNone(configured)
        assert configured is not None
        self.assertEqual(configured.vault_path, self.vault.resolve())

        undiscovered = self.root / "other"
        undiscovered.mkdir()
        code, stdout, stderr = self.run_cli(
            "configure", "--vault-path", str(undiscovered)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "invalid_arguments")

    def test_prepare_stage_rename_commit_round_trip_is_json(self):
        code, stdout, _ = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "设计",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
        )
        self.assertEqual(code, 0)
        prepared = json.loads(stdout)
        code, stdout, stderr = self.run_cli(
            "stage-bind",
            "--prepared-token",
            prepared["prepared_token"],
            "--topics-json",
            '["topic", "topic"]',
            "--project-role",
            "owner",
            "--result",
            "selected",
            "--next-step",
            "implement",
            "--status",
            "进行中",
            "--summary-json",
            json.dumps(
                {
                    "current_progress": "selected",
                    "unresolved": "none",
                    "recommended_session": {
                        "agent_id": "codex",
                        "session_id": SESSION_ID,
                    },
                    "evidence_sessions": [
                        {"agent_id": "codex", "session_id": SESSION_ID}
                    ],
                }
            ),
        )
        self.assertEqual((code, stderr), (0, ""))
        staged = json.loads(stdout)
        renamed: list[tuple[str, str]] = []
        self.rename_backend = lambda session_id, title: renamed.append(
            (session_id, title)
        )
        code, stdout, stderr = self.run_cli(
            "rename-thread", "--staged-token", staged["staged_token"]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["target_title"], "REQ-1 设计")
        self.assertEqual(renamed, [(SESSION_ID, "REQ-1 设计")])
        code, stdout, stderr = self.run_cli(
            "commit-bind",
            "--staged-token",
            staged["staged_token"],
            "--rename-confirmed",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["resume_command"], f"codex resume {SESSION_ID}")

    def test_rename_rejects_wrong_runtime_before_starting_backend(self):
        code, stdout, _ = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "设计",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
        )
        self.assertEqual(code, 0)
        prepared = json.loads(stdout)
        code, stdout, _ = self.run_cli(
            "stage-bind",
            "--prepared-token",
            prepared["prepared_token"],
            "--topics-json",
            "[]",
            "--project-role",
            "owner",
            "--result",
            "selected",
            "--next-step",
            "implement",
            "--status",
            "进行中",
            "--summary-json",
            json.dumps(
                {
                    "current_progress": "selected",
                    "unresolved": "implement",
                    "recommended_session": {
                        "agent_id": "codex",
                        "session_id": SESSION_ID,
                    },
                    "evidence_sessions": [
                        {"agent_id": "codex", "session_id": SESSION_ID}
                    ],
                }
            ),
        )
        staged = json.loads(stdout)
        renamed: list[tuple[str, str]] = []
        self.rename_backend = lambda session_id, title: renamed.append(
            (session_id, title)
        )

        code, stdout, stderr = self.run_cli(
            "rename-thread",
            "--staged-token",
            staged["staged_token"],
            env={"CODEX_SESSION_ID": "0191f8c0-7a11-7000-8000-000000000005"},
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "token_error")
        self.assertEqual(renamed, [])
        self.assertTrue(Path(staged["staged_token"]).is_file())

    def test_backend_rename_failure_keeps_token_and_dossier_unwritten(self):
        code, stdout, _ = self.run_cli(
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "设计",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
        )
        prepared = json.loads(stdout)
        self.assertEqual(code, 0)
        code, stdout, _ = self.run_cli(
            "stage-bind",
            "--prepared-token",
            prepared["prepared_token"],
            "--topics-json",
            "[]",
            "--project-role",
            "owner",
            "--result",
            "selected",
            "--next-step",
            "implement",
            "--status",
            "进行中",
            "--summary-json",
            json.dumps(
                {
                    "current_progress": "selected",
                    "unresolved": "implement",
                    "recommended_session": {
                        "agent_id": "codex",
                        "session_id": SESSION_ID,
                    },
                    "evidence_sessions": [
                        {"agent_id": "codex", "session_id": SESSION_ID}
                    ],
                }
            ),
        )
        staged = json.loads(stdout)
        self.assertEqual(code, 0)
        self.rename_backend = mock.Mock(
            side_effect=RenameError("task rename failed")
        )

        code, stdout, stderr = self.run_cli(
            "rename-thread", "--staged-token", staged["staged_token"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "rename_required")
        self.assertTrue(Path(staged["staged_token"]).is_file())
        self.assertFalse(
            (self.vault / "AI-Coding-Archive/WorkItems/REQ-1.md").exists()
        )

    def test_partial_failure_has_documented_exit_code_without_token_leak(self):
        secret_token = str(self.token_root / "secret-capability")
        with (
            mock.patch.object(ai_worklog, "token_vault_path", return_value=self.vault),
            mock.patch.object(
                ai_worklog,
                "commit_bind",
                side_effect=PartialBindFailure(OSError("disk full")),
            ),
        ):
            code, stdout, stderr = self.run_cli(
                "commit-bind", "--staged-token", secret_token, "--rename-confirmed"
            )
        payload = json.loads(stderr)
        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertEqual(payload["error_code"], "rename_only_partial_failure")
        self.assertTrue(payload["rename_already_completed"])
        self.assertNotIn(secret_token, stderr)

    def test_confirmed_commit_preflight_retains_token_error_code(self):
        secret_token = str(self.token_root / "missing-token")
        with mock.patch.object(
            ai_worklog,
            "token_vault_path",
            side_effect=TokenError("bind token is invalid"),
        ):
            code, stdout, stderr = self.run_cli(
                "commit-bind", "--staged-token", secret_token, "--rename-confirmed"
            )

        payload = json.loads(stderr)
        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertEqual(payload["error_code"], "token_error")
        self.assertTrue(payload["rename_already_completed"])
        self.assertNotIn(secret_token, stderr)

    def test_disabled_cli_decline_runs_complete_bind_in_filesystem_mode(self):
        self.runner = DisabledCliRunner()
        state_patch = mock.patch.object(
            ai_worklog,
            "detect_obsidian",
            return_value=ObsidianState(
                installed=True,
                cli_present=True,
                cli_status="registration_required",
            ),
        )
        state_patch.start()
        self.addCleanup(state_patch.stop)
        prepare_args = (
            "prepare-bind",
            "--work-item-id",
            "REQ-1",
            "--session-title",
            "设计",
            "--cwd",
            str(self.root / "payment-api"),
            "--vault",
            str(self.vault),
        )

        code, stdout, stderr = self.run_cli(*prepare_args)
        payload = json.loads(stderr)
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(payload["error_code"], "user_action_required")
        self.assertEqual(payload["action"], "enable_obsidian_cli")
        self.assertTrue(payload["filesystem_fallback_available"])
        self.assertFalse(self.token_root.exists())

        code, stdout, stderr = self.run_cli(
            *prepare_args, "--filesystem-fallback"
        )
        self.assertEqual((code, stderr), (0, ""))
        prepared = json.loads(stdout)
        code, stdout, stderr = self.run_cli(
            "stage-bind",
            "--prepared-token",
            prepared["prepared_token"],
            "--topics-json",
            "[]",
            "--project-role",
            "owner",
            "--result",
            "selected",
            "--next-step",
            "implement",
            "--status",
            "进行中",
            "--summary-json",
            json.dumps(
                {
                    "current_progress": "selected",
                    "unresolved": "implement",
                    "recommended_session": {
                        "agent_id": "codex",
                        "session_id": SESSION_ID,
                    },
                    "evidence_sessions": [
                        {"agent_id": "codex", "session_id": SESSION_ID}
                    ],
                }
            ),
            "--filesystem-fallback",
        )
        self.assertEqual((code, stderr), (0, ""))
        staged = json.loads(stdout)
        code, stdout, stderr = self.run_cli(
            "commit-bind",
            "--staged-token",
            staged["staged_token"],
            "--filesystem-fallback",
            "--rename-confirmed",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["warnings"], [])
        self.assertTrue(
            (self.vault / "AI-Coding-Archive/WorkItems/REQ-1.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
