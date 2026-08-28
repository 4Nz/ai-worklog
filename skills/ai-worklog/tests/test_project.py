from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import Sequence


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.project import repository_name, resolve_project, sanitize_remote  # noqa: E402


class FakeRunner:
    def __init__(self, outputs: dict[tuple[str, ...], str]):
        self.outputs = outputs

    def __call__(
        self, args: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        output = self.outputs.get(tuple(args))
        if output is None:
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, output, "")

    @classmethod
    def failing(cls) -> "FakeRunner":
        return cls({})


class ProjectIdentityTests(unittest.TestCase):
    def test_https_remote_is_sanitized_and_names_project(self):
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--show-toplevel"): "/work/payment-api\n",
                ("git", "remote", "get-url", "origin"): (
                    "https://user:secret@git.example.com/pay/payment-api.git\n"
                ),
            }
        )
        value = resolve_project(Path("/work/payment-api/src"), runner)
        self.assertEqual(value.project_name, "payment-api")
        self.assertEqual(
            value.repository, "https://git.example.com/pay/payment-api.git"
        )

    def test_scp_remote_and_non_git_fallbacks(self):
        self.assertEqual(
            repository_name("git@git.example.com:pay/order-service.git"), "order-service"
        )
        value = resolve_project(Path("/work/local-tool"), FakeRunner.failing())
        self.assertEqual(value.project_name, "local-tool")
        self.assertIsNone(value.repository)

    def test_standard_ssh_remote_removes_username(self):
        remote = "ssh://git@git.example.com/pay/order-service.git"
        self.assertEqual(
            sanitize_remote(remote), "ssh://git.example.com/pay/order-service.git"
        )
        self.assertEqual(repository_name(remote), "order-service")

    def test_malformed_credential_bearing_https_fails_closed(self):
        self.assertIsNone(sanitize_remote("https://user:secret@/payment-api.git"))

    def test_malformed_hostless_hierarchical_remote_fails_closed(self):
        remote = (
            "https:///user:password@host/payment-api.git"
            "?access_token=secret#fragment"
        )
        self.assertIsNone(sanitize_remote(remote))
        self.assertIsNone(repository_name(remote))

    def test_https_remote_drops_query_and_fragment_tokens(self):
        self.assertEqual(
            sanitize_remote(
                "https://user:secret@git.example.com/pay/payment-api.git"
                "?access_token=secret#fragment-token"
            ),
            "https://git.example.com/pay/payment-api.git",
        )

    def test_generic_url_remote_removes_credentials_and_tokens(self):
        self.assertEqual(
            sanitize_remote(
                "ftp://user:password@git.example.com/pay/payment-api.git"
                "?access_token=secret#fragment-token"
            ),
            "ftp://git.example.com/pay/payment-api.git",
        )

    def test_scp_remote_strips_query_and_fragment(self):
        remote = (
            "git@git.example.com:pay/payment-api.git"
            "?access_token=secret#fragment"
        )
        self.assertEqual(
            sanitize_remote(remote),
            "ssh://git.example.com/pay/payment-api.git",
        )
        self.assertEqual(repository_name(remote), "payment-api")

    def test_password_bearing_scp_remote_fails_closed(self):
        remote = "user:password@git.example.com:pay/payment-api.git"
        self.assertIsNone(sanitize_remote(remote))
        self.assertIsNone(repository_name(remote))

    def test_password_bearing_scp_without_repository_path_fails_closed(self):
        remote = "user:password@git.example.com"
        self.assertIsNone(sanitize_remote(remote))
        self.assertIsNone(repository_name(remote))

    def test_malformed_single_slash_hierarchical_remote_fails_closed(self):
        remote = (
            "https:/user:password@git.example.com/payment-api.git"
            "?access_token=secret#fragment"
        )
        self.assertIsNone(sanitize_remote(remote))
        self.assertIsNone(repository_name(remote))

    def test_hostless_file_remote_is_preserved_without_query_or_fragment(self):
        remote = "file:///absolute/payment-api.git?access_token=secret#fragment"
        self.assertEqual(sanitize_remote(remote), "file:///absolute/payment-api.git")
        self.assertEqual(repository_name(remote), "payment-api")

    def test_bracketed_ipv6_remote_is_preserved_without_credentials_or_tokens(self):
        remote = (
            "ssh://git@[2001:db8::1]:2222/pay/payment-api.git"
            "?access_token=secret#fragment"
        )
        self.assertEqual(
            sanitize_remote(remote),
            "ssh://[2001:db8::1]:2222/pay/payment-api.git",
        )
        self.assertEqual(repository_name(remote), "payment-api")

    def test_malformed_remote_uses_git_root_project_fallback(self):
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--show-toplevel"): "/work/payment-api\n",
                ("git", "remote", "get-url", "origin"): (
                    "https:///user:password@host/payment-api.git"
                    "?access_token=secret#fragment\n"
                ),
            }
        )
        value = resolve_project(Path("/work/payment-api/src"), runner)
        self.assertEqual(value.project_name, "payment-api")
        self.assertIsNone(value.repository)

    def test_remote_without_basename_uses_git_root_name(self):
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--show-toplevel"): "/work/payment-api\n",
                ("git", "remote", "get-url", "origin"): "https://git.example.com/\n",
            }
        )
        value = resolve_project(Path("/work/payment-api/src"), runner)
        self.assertEqual(value.project_name, "payment-api")
        self.assertEqual(value.repository, "https://git.example.com/")
