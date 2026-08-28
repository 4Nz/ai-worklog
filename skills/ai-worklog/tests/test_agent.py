from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.agent import (  # noqa: E402
    AmbiguousAgent,
    UnsupportedAgent,
    detect_current_session,
    render_resume_command,
)
from worklog.model import SessionRef, ValidationError, validate_session_id  # noqa: E402


class AgentRegistryTests(unittest.TestCase):
    def test_detects_each_codex_source_and_matching_pair(self):
        for env in (
            {"CODEX_SESSION_ID": "codex/opaque id"},
            {"CODEX_THREAD_ID": "codex/opaque id"},
            {"CODEX_SESSION_ID": "codex/opaque id", "CODEX_THREAD_ID": "codex/opaque id"},
        ):
            with self.subTest(env=env):
                session = detect_current_session(env)
                self.assertEqual(session.agent_id, "codex")
                self.assertEqual(session.resume_argv, ("codex", "resume", "codex/opaque id"))
                self.assertEqual(session.rename_mode, "automatic")

    def test_detects_only_hook_injected_claude_identity(self):
        session = detect_current_session({
            "AI_WORKLOG_AGENT": "claude-code",
            "AI_WORKLOG_SESSION_ID": "claude id 'quoted'",
        })
        self.assertEqual(session.agent_id, "claude-code")
        self.assertEqual(session.resume_argv, ("claude", "--resume", "claude id 'quoted'"))
        self.assertEqual(session.rename_mode, "manual")

    def test_zero_and_multiple_matches_fail_closed(self):
        with self.assertRaises(UnsupportedAgent):
            detect_current_session({"PATH": "/bin:/usr/bin"})
        with self.assertRaises(UnsupportedAgent):
            detect_current_session({"AI_WORKLOG_SESSION_ID": "not-hooked"})
        with self.assertRaises(UnsupportedAgent):
            detect_current_session({"AI_WORKLOG_AGENT": "claude-code"})
        with self.assertRaises(AmbiguousAgent):
            detect_current_session({
                "CODEX_SESSION_ID": "same",
                "AI_WORKLOG_AGENT": "claude-code",
                "AI_WORKLOG_SESSION_ID": "same",
            })

    def test_codex_disagreement_and_invalid_opaque_ids_fail(self):
        with self.assertRaises(ValidationError):
            detect_current_session({"CODEX_SESSION_ID": "one", "CODEX_THREAD_ID": "two"})
        for value in ("", "line\nbreak", "null\0byte", "x" * 257, "hidden\u200bcontrol"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_session_id(value)

    def test_resume_rendering_quotes_opaque_id_from_trusted_argv(self):
        ref = SessionRef("claude-code", "id with 'quotes' and $HOME")
        self.assertEqual(
            render_resume_command(ref),
            "claude --resume 'id with '\"'\"'quotes'\"'\"' and $HOME'",
        )


if __name__ == "__main__":
    unittest.main()
