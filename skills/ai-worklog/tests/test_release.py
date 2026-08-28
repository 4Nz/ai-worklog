from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "validate_release.py"
PACKAGER_PATH = REPO_ROOT / "scripts" / "package_release.py"
PUBLIC_FILES = (
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/architecture.md",
    "docs/dossier-schema.md",
    "docs/release-process.md",
)
INSTALL_AND_UPGRADE_COMMANDS = (
    "codex plugin marketplace add 4Nz/ai-worklog",
    "codex plugin add ai-worklog@ai-worklog",
    "codex plugin marketplace upgrade ai-worklog",
    "claude plugin marketplace add 4Nz/ai-worklog",
    "claude plugin install ai-worklog@ai-worklog",
    "claude plugin marketplace update ai-worklog",
    "claude plugin update ai-worklog@ai-worklog",
)
GITHUB_PROJECT_FILES = (
    ".gitattributes",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/pull_request_template.md",
)


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected object in {path}")
    return value


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


class CopiedRepository:
    def __enter__(self) -> Path:
        self.temporary = tempfile.TemporaryDirectory()
        target = Path(self.temporary.name) / "ai-worklog"
        shutil.copytree(
            REPO_ROOT,
            target,
            ignore=shutil.ignore_patterns(".git", ".worktrees", "__pycache__", "*.pyc"),
        )
        return target

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.temporary.cleanup()


def copied_repository() -> CopiedRepository:
    return CopiedRepository()


def load_validate_release():
    if not VALIDATOR_PATH.is_file():
        raise AssertionError("scripts/validate_release.py must exist")
    spec = importlib.util.spec_from_file_location("validate_release", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("release validator must be importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_release


def load_package_release():
    if not PACKAGER_PATH.is_file():
        raise AssertionError("scripts/package_release.py must exist")
    spec = importlib.util.spec_from_file_location("package_release", PACKAGER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("release packager must be importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.package_release


class ReleaseValidationTests(unittest.TestCase):
    def test_current_repository_satisfies_release_contract(self):
        validate_release = load_validate_release()
        self.assertEqual(validate_release(REPO_ROOT), ())

    def test_manifest_version_drift_is_rejected(self):
        validate_release = load_validate_release()
        with copied_repository() as root:
            manifest = read_json(root / ".claude-plugin" / "plugin.json")
            manifest["version"] = "0.1.1"
            write_json(root / ".claude-plugin" / "plugin.json", manifest)
            self.assertIn(
                "version fields must all equal 0.1.0",
                validate_release(root),
            )

    def test_marketplace_source_escape_is_rejected(self):
        validate_release = load_validate_release()
        with copied_repository() as root:
            path = root / ".agents" / "plugins" / "marketplace.json"
            marketplace = read_json(path)
            plugins = marketplace["plugins"]
            self.assertIsInstance(plugins, list)
            plugins[0]["source"]["path"] = "../outside"
            write_json(path, marketplace)
            self.assertTrue(
                any("inside repository root" in error for error in validate_release(root))
            )

    def test_missing_referenced_component_is_rejected(self):
        validate_release = load_validate_release()
        with copied_repository() as root:
            shutil.rmtree(root / "skills")
            self.assertTrue(
                any("skills path does not exist" in error for error in validate_release(root))
            )

    def test_skill_version_drift_is_rejected(self):
        validate_release = load_validate_release()
        with copied_repository() as root:
            path = root / "skills" / "ai-worklog" / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            before, frontmatter, after = text.split("---", 2)
            frontmatter += '\nmetadata:\n  author: 4Nz\n  version: "0.1.1"\n'
            path.write_text(
                "---".join((before, frontmatter, after)), encoding="utf-8"
            )
            self.assertIn(
                "version fields must all equal 0.1.0",
                validate_release(root),
            )

    def test_public_documentation_set_exists(self):
        missing = [path for path in PUBLIC_FILES if not (REPO_ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_missing_public_document_is_rejected_by_release_validator(self):
        validate_release = load_validate_release()
        with copied_repository() as root:
            (root / "README.md").unlink()
            self.assertIn("missing public release file: README.md", validate_release(root))

    def test_skill_exposes_public_release_metadata(self):
        text = (REPO_ROOT / "skills" / "ai-worklog" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        frontmatter = text.split("---", 2)[1]
        for required in (
            "license: Apache-2.0",
            "author: 4Nz",
            'version: "0.1.0"',
            'compatibility: "macOS; Codex or Claude Code; Python 3.10+; Obsidian"',
        ):
            self.assertIn(required, frontmatter)

    def test_readmes_share_install_upgrade_and_privacy_contract(self):
        for relative in ("README.md", "README.zh-CN.md"):
            with self.subTest(path=relative):
                path = REPO_ROOT / relative
                self.assertTrue(path.is_file(), f"missing {relative}")
                text = path.read_text(encoding="utf-8")
                for command in INSTALL_AND_UPGRADE_COMMANDS:
                    self.assertIn(command, text)
                for required in (
                    "~/.config/ai-worklog",
                    "AI-Coding-Archive/WorkItems",
                    "Session ID",
                    "schema_version",
                    "Apache-2.0",
                    "Codex",
                    "Claude Code",
                    "Obsidian",
                ):
                    self.assertIn(required, text)

    def test_readmes_describe_requirements_projects_and_sessions(self):
        english_path = REPO_ROOT / "README.md"
        chinese_path = REPO_ROOT / "README.zh-CN.md"
        self.assertTrue(english_path.is_file(), "missing README.md")
        self.assertTrue(chinese_path.is_file(), "missing README.zh-CN.md")
        english = english_path.read_text(encoding="utf-8")
        chinese = chinese_path.read_text(encoding="utf-8")
        for required in ("requirements", "multiple projects", "multiple sessions"):
            self.assertIn(required, english.casefold())
        for required in ("需求", "多个项目", "多个会话"):
            self.assertIn(required, chinese)

    def test_github_project_automation_exists(self):
        missing = [path for path in GITHUB_PROJECT_FILES if not (REPO_ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_release_archive_has_public_root_and_excludes_internal_files(self):
        package_release = load_package_release()
        with tempfile.TemporaryDirectory() as temporary:
            archive = package_release(REPO_ROOT, Path(temporary))
            self.assertEqual(archive.name, "ai-worklog-0.1.0.tar.gz")
            with tarfile.open(archive, "r:gz") as package:
                names = set(package.getnames())
            required = {
                "ai-worklog-0.1.0/.codex-plugin/plugin.json",
                "ai-worklog-0.1.0/.claude-plugin/plugin.json",
                "ai-worklog-0.1.0/.claude-plugin/marketplace.json",
                "ai-worklog-0.1.0/.agents/plugins/marketplace.json",
                "ai-worklog-0.1.0/skills/ai-worklog/SKILL.md",
                "ai-worklog-0.1.0/hooks/hooks.json",
                "ai-worklog-0.1.0/README.md",
                "ai-worklog-0.1.0/LICENSE",
            }
            self.assertTrue(required.issubset(names))
            for name in names:
                self.assertNotIn("docs/superpowers", name)
                self.assertNotIn("/.git/", name)
                self.assertNotIn("/.worktrees/", name)
                self.assertNotIn("__pycache__", name)
                self.assertFalse(name.endswith((".pyc", ".pyo")))

    def test_release_archive_is_reproducible_and_ignores_python_cache(self):
        package_release = load_package_release()
        with copied_repository() as root, tempfile.TemporaryDirectory() as temporary:
            cache = root / "skills" / "ai-worklog" / "__pycache__"
            cache.mkdir()
            (cache / "secret.pyc").write_bytes(b"not release material")
            first_dir = Path(temporary) / "first"
            second_dir = Path(temporary) / "second"
            first = package_release(root, first_dir)
            second = package_release(root, second_dir)
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).digest(),
                hashlib.sha256(second.read_bytes()).digest(),
            )
            with tarfile.open(first, "r:gz") as package:
                self.assertFalse(any("secret.pyc" in name for name in package.getnames()))

    def test_release_archive_can_use_repository_dist_directory(self):
        package_release = load_package_release()
        with copied_repository() as root:
            archive = package_release(root, root / "dist")
            self.assertTrue(archive.is_file())
            with tarfile.open(archive, "r:gz") as package:
                self.assertFalse(any("/dist/" in name for name in package.getnames()))


if __name__ == "__main__":
    unittest.main()
