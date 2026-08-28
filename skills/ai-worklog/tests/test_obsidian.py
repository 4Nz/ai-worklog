from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.obsidian import (  # noqa: E402
    ObsidianState,
    RECORDS_FOLDER,
    VaultSelectionRequired,
    WorklogConfig,
    detect_obsidian,
    discover_vaults,
    load_config,
    resolve_store,
    save_config,
)


class FakeRunner:
    def __init__(
        self,
        outputs: dict[tuple[str, ...], tuple[int, str] | tuple[int, str, str]],
        errors: set[tuple[str, ...]] | None = None,
    ):
        self.outputs = outputs
        self.errors = errors or set()
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, args: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append(command)
        if command in self.errors:
            raise FileNotFoundError("obsidian is unavailable")
        result = self.outputs.get(command, (1, ""))
        returncode, stdout = result[:2]
        stderr = result[2] if len(result) == 3 else ""
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    @classmethod
    def not_found(cls) -> "FakeRunner":
        return cls({}, {("obsidian", "version")})


class ObsidianTests(unittest.TestCase):
    def test_detect_marks_absent_app_and_failed_cli_as_uninstalled(self):
        state = detect_obsidian(FakeRunner.not_found(), applications=())
        self.assertFalse(state.installed)
        self.assertFalse(state.cli_present)
        self.assertFalse(state.cli_usable)

    def test_failed_cli_command_does_not_prove_app_is_installed(self):
        runner = FakeRunner({("obsidian", "version"): (1, "not enabled")})
        state = detect_obsidian(runner, applications=())
        self.assertFalse(state.installed)
        self.assertTrue(state.cli_present)
        self.assertEqual(state.cli_status, "registration_required")

    def test_cli_must_execute_real_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Obsidian.app"
            app.mkdir()
            runner = FakeRunner({("obsidian", "version"): (1, "not enabled")})
            state = detect_obsidian(runner, applications=(app,))
        self.assertTrue(state.installed)
        self.assertTrue(state.cli_present)
        self.assertFalse(state.cli_usable)
        self.assertEqual(state.cli_status, "registration_required")

    def test_explicitly_unregistered_cli_requires_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Obsidian.app"
            app.mkdir()
            runner = FakeRunner(
                {("obsidian", "version"): (1, "", "Obsidian CLI is not registered")}
            )
            state = detect_obsidian(runner, applications=(app,))
        self.assertEqual(state.cli_status, "registration_required")

    def test_explicitly_disabled_cli_requires_choice_before_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            state = ObsidianState(
                installed=True,
                cli_present=True,
                cli_status="registration_required",
            )

            with self.assertRaisesRegex(ValueError, "CLI"):
                resolve_store(
                    vaults=(vault,),
                    config=None,
                    run=FakeRunner.not_found(),
                    state=state,
                )

            store = resolve_store(
                vaults=(vault,),
                config=None,
                run=FakeRunner.not_found(),
                state=state,
                allow_filesystem_fallback=True,
            )

        self.assertEqual(store.mode, "filesystem")

    def test_not_running_message_uses_filesystem_without_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Obsidian.app"
            app.mkdir()
            vault = Path(temporary) / "vault"
            vault.mkdir()
            runner = FakeRunner(
                {
                    ("obsidian", "version"): (
                        1,
                        "",
                        "The CLI is unable to find Obsidian. Please make sure Obsidian is running and try again.\n",
                    )
                }
            )
            state = detect_obsidian(runner, applications=(app,))
            store = resolve_store((vault,), None, run=runner, state=state)

        self.assertEqual(state.cli_status, "temporarily_unavailable")
        self.assertEqual(store.mode, "filesystem")

    def test_unknown_cli_failure_uses_filesystem_without_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Obsidian.app"
            app.mkdir()
            vault = Path(temporary) / "vault"
            vault.mkdir()
            runner = FakeRunner({("obsidian", "version"): (70, "", "unexpected failure")})
            state = detect_obsidian(runner, applications=(app,))
            store = resolve_store((vault,), None, run=runner, state=state)

        self.assertEqual(state.cli_status, "temporarily_unavailable")
        self.assertEqual(store.mode, "filesystem")

    def test_missing_cli_with_installed_app_requires_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Obsidian.app"
            app.mkdir()
            state = detect_obsidian(FakeRunner.not_found(), applications=(app,))
        self.assertTrue(state.installed)
        self.assertFalse(state.cli_present)
        self.assertEqual(state.cli_status, "registration_required")

    def test_multiple_vaults_require_selection_without_valid_cache(self):
        vaults = (Path("/v/a"), Path("/v/b"))
        with self.assertRaises(VaultSelectionRequired) as caught:
            resolve_store(vaults=vaults, config=None)
        self.assertEqual(caught.exception.choices, vaults)

    def test_stale_cached_vault_requires_user_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current"
            current.mkdir()
            config = WorklogConfig(vault_path=(root / "removed").resolve())
            with self.assertRaises(VaultSelectionRequired) as caught:
                resolve_store(
                    vaults=(current,), config=config, run=FakeRunner.not_found()
                )
        self.assertEqual(caught.exception.choices, (current.resolve(),))

    def test_discover_vaults_uses_only_existing_configured_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            config = root / "obsidian.json"
            config.write_text(
                json.dumps(
                    {
                        "vaults": {
                            str(first): {"ts": 1},
                            "missing": {"path": str(root / "missing")},
                            "alias": {"path": str(second)},
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(discover_vaults(config), (first.resolve(), second.resolve()))

    def test_config_round_trips_only_fixed_folder_and_auto_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.yaml"
            config = WorklogConfig(vault_path=(root / "vault").resolve())
            save_config(path, config)
            self.assertEqual(load_config(path), config)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "vault_path: " + str(config.vault_path) + "\n"
                "records_folder: AI-Coding-Archive/WorkItems\n"
                "write_mode: auto\n",
            )

    def test_config_fails_closed_for_unknown_or_malformed_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(
                "vault_path: /vault\n"
                "records_folder: AI-Coding-Archive/WorkItems\n"
                "write_mode: auto\n"
                "extra: value\n",
                encoding="utf-8",
            )
            self.assertIsNone(load_config(path))
            path.write_text("vault_path: relative\n", encoding="utf-8")
            self.assertIsNone(load_config(path))
            path.write_text(
                "vault_path: ~/Vault\n"
                "records_folder: AI-Coding-Archive/WorkItems\n"
                "write_mode: auto\n",
                encoding="utf-8",
            )
            self.assertIsNone(load_config(path))

    def test_cli_store_passes_path_to_search_context_and_rereads_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relative = RECORDS_FOLDER / "REQ-1.md"
            runner = FakeRunner(
                {
                    ("obsidian", "version"): (0, "1.8.0\n"),
                    (
                        "obsidian",
                        "search:context",
                        "query=幂等",
                        "path=AI-Coding-Archive/WorkItems",
                    ): (0, str(relative) + "\n"),
                    ("obsidian", "read", f"path={relative.as_posix()}"): (0, "found 幂等\n"),
                }
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            matches = store.search("幂等")
        self.assertEqual(store.mode, "cli")
        self.assertEqual([match.relative_path for match in matches], [relative])
        self.assertEqual([match.text for match in matches], ["found 幂等\n"])
        self.assertIn(
            ("obsidian", "search:context", "query=幂等", "path=AI-Coding-Archive/WorkItems"),
            runner.calls,
        )

    def test_search_falls_back_to_files_but_never_scans_vault_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            directory = vault / RECORDS_FOLDER
            directory.mkdir(parents=True)
            (directory / "REQ-1.md").write_text("payment-api\n", encoding="utf-8")
            (vault / "unrelated.md").write_text("unrelated vault note\n", encoding="utf-8")
            store = resolve_store(
                vaults=(vault,),
                config=None,
                run=FakeRunner.not_found(),
                allow_filesystem_fallback=True,
            )
            matches = store.search("payment-api")
        self.assertEqual(store.mode, "filesystem")
        self.assertEqual([match.relative_path for match in matches], [RECORDS_FOLDER / "REQ-1.md"])
        self.assertNotIn("unrelated vault note\n", [match.text for match in matches])

    def test_failed_cli_write_switches_to_filesystem_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            runner = FakeRunner({("obsidian", "version"): (0, "1.8.0\n")})
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            relative = RECORDS_FOLDER / "REQ-1.md"
            store.write(relative, "stored\n")
            self.assertEqual(store.mode, "filesystem")
            self.assertEqual((vault / relative).read_text(encoding="utf-8"), "stored\n")

    def test_cli_runner_errors_switch_read_and_write_to_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relative = RECORDS_FOLDER / "REQ-1.md"
            target = vault / relative
            target.parent.mkdir(parents=True)
            target.write_text("previous\n", encoding="utf-8")
            read = ("obsidian", "read", f"path={relative.as_posix()}")
            runner = FakeRunner(
                {("obsidian", "version"): (0, "1.8.0\n")}, {read}
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            self.assertEqual(store.read(relative), "previous\n")
            self.assertEqual(store.mode, "filesystem")

            create = (
                "obsidian",
                "create",
                f"path={relative.as_posix()}",
                "content=updated\n",
                "overwrite",
            )
            runner = FakeRunner(
                {("obsidian", "version"): (0, "1.8.0\n")}, {create}
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            store.write(relative, "updated\n")
            self.assertEqual(store.mode, "filesystem")
            self.assertEqual(target.read_text(encoding="utf-8"), "updated\n")

    def test_cli_search_runner_error_uses_scoped_filesystem_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relative = RECORDS_FOLDER / "REQ-1.md"
            target = vault / relative
            target.parent.mkdir(parents=True)
            target.write_text("payment-api\n", encoding="utf-8")
            search = (
                "obsidian",
                "search:context",
                "query=payment-api",
                "path=AI-Coding-Archive/WorkItems",
            )
            runner = FakeRunner(
                {("obsidian", "version"): (0, "1.8.0\n")}, {search}
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            matches = store.search("payment-api")
        self.assertEqual(store.mode, "filesystem")
        self.assertEqual([match.relative_path for match in matches], [relative])

    def test_cli_candidate_read_runner_error_uses_filesystem_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relative = RECORDS_FOLDER / "REQ-1.md"
            target = vault / relative
            target.parent.mkdir(parents=True)
            target.write_text("payment-api\n", encoding="utf-8")
            read = ("obsidian", "read", f"path={relative.as_posix()}")
            runner = FakeRunner(
                {
                    ("obsidian", "version"): (0, "1.8.0\n"),
                    (
                        "obsidian",
                        "search:context",
                        "query=payment-api",
                        "path=AI-Coding-Archive/WorkItems",
                    ): (0, str(relative) + "\n"),
                },
                {read},
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            matches = store.search("payment-api")
        self.assertEqual(store.mode, "filesystem")
        self.assertEqual([match.relative_path for match in matches], [relative])

    def test_unrecognized_nonempty_cli_output_uses_scoped_filesystem_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relative = RECORDS_FOLDER / "REQ-1.md"
            target = vault / relative
            target.parent.mkdir(parents=True)
            target.write_text("payment-api\n", encoding="utf-8")
            runner = FakeRunner(
                {
                    ("obsidian", "version"): (0, "1.8.0\n"),
                    (
                        "obsidian",
                        "search:context",
                        "query=payment-api",
                        "path=AI-Coding-Archive/WorkItems",
                    ): (0, "unrecognized context payload\n"),
                }
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            matches = store.search("payment-api")
        self.assertEqual(store.mode, "filesystem")
        self.assertEqual([match.relative_path for match in matches], [relative])

    def test_store_rejects_paths_outside_fixed_records_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            store = resolve_store(
                vaults=(vault,),
                config=None,
                run=FakeRunner.not_found(),
                allow_filesystem_fallback=True,
            )
            for relative in (Path("REQ-1.md"), Path("../outside.md"), Path("/tmp/outside.md")):
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    store.write(relative, "not allowed")

    def test_cli_search_ignores_unscoped_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relative = RECORDS_FOLDER / "REQ-1.md"
            runner = FakeRunner(
                {
                    ("obsidian", "version"): (0, "1.8.0\n"),
                    (
                        "obsidian",
                        "search:context",
                        "query=payment-api",
                        "path=AI-Coding-Archive/WorkItems",
                    ): (0, "../../unrelated.md\n" + str(relative) + "\n"),
                    ("obsidian", "read", f"path={relative.as_posix()}"): (0, "payment-api\n"),
                }
            )
            store = resolve_store(vaults=(vault,), config=None, run=runner)
            matches = store.search("payment-api")
        self.assertEqual([match.relative_path for match in matches], [relative])
        self.assertNotIn(("obsidian", "read", "path=../../unrelated.md"), runner.calls)


if __name__ == "__main__":
    unittest.main()
