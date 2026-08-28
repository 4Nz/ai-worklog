#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any


PLUGIN_NAME = "ai-worklog"
RELEASE_VERSION = "0.1.0"
REPOSITORY = "https://github.com/4Nz/ai-worklog.git"
LICENSE = "Apache-2.0"
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PUBLIC_RELEASE_FILES = (
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


def _load_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing required JSON file: {path.name}")
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON file {path.name}: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"JSON root must be an object: {path.name}")
        return None
    return value


def _one_plugin(
    marketplace: dict[str, Any] | None,
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if marketplace is None:
        return None
    if marketplace.get("name") != PLUGIN_NAME:
        errors.append(f"{label} marketplace name must be {PLUGIN_NAME}")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        errors.append(f"{label} marketplace must contain exactly one plugin")
        return None
    plugin = plugins[0]
    if not isinstance(plugin, dict):
        errors.append(f"{label} marketplace plugin must be an object")
        return None
    if plugin.get("name") != PLUGIN_NAME:
        errors.append(f"{label} marketplace plugin name must be {PLUGIN_NAME}")
    return plugin


def _inside_root(root: Path, relative: object, label: str, errors: list[str]) -> None:
    if not isinstance(relative, str) or not relative.startswith("./"):
        errors.append(f"{label} must be a ./ relative path inside repository root")
        return
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        errors.append(f"{label} must resolve inside repository root")


def _validate_manifest(
    manifest: dict[str, Any] | None,
    label: str,
    root: Path,
    errors: list[str],
) -> str | None:
    if manifest is None:
        return None
    expected = {
        "name": PLUGIN_NAME,
        "repository": REPOSITORY,
        "license": LICENSE,
        "skills": "./skills/",
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            errors.append(f"{label} manifest {field} must equal {value}")
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        errors.append(f"{label} manifest version must be strict SemVer")
        version = None
    _inside_root(root, manifest.get("skills"), f"{label} skills path", errors)
    if manifest.get("skills") == "./skills/" and not (root / "skills").is_dir():
        errors.append(f"{label} skills path does not exist")
    return version


def _validate_skill(root: Path, errors: list[str]) -> str | None:
    path = root / "skills" / "ai-worklog" / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"unable to read Skill metadata: {exc}")
        return None
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        errors.append("Skill frontmatter is missing or malformed")
        return None
    frontmatter = parts[1]
    required_lines = (
        "license: Apache-2.0",
        "  author: 4Nz",
        '  compatibility: "macOS; Codex or Claude Code; Python 3.10+; Obsidian"',
    )
    for line in required_lines:
        if frontmatter.count(f"\n{line}\n") != 1:
            errors.append(f"Skill metadata must contain exactly one {line.strip()}")
    versions = re.findall(r'^  version: "([^"]+)"$', frontmatter, re.MULTILINE)
    if len(versions) != 1:
        errors.append("Skill metadata must contain exactly one version")
        return None
    return versions[0]


def validate_release(root: Path) -> tuple[str, ...]:
    root = root.resolve()
    errors: list[str] = []

    codex_manifest = _load_object(root / ".codex-plugin" / "plugin.json", errors)
    claude_manifest = _load_object(root / ".claude-plugin" / "plugin.json", errors)
    codex_marketplace = _load_object(
        root / ".agents" / "plugins" / "marketplace.json", errors
    )
    claude_marketplace = _load_object(
        root / ".claude-plugin" / "marketplace.json", errors
    )

    versions = [
        _validate_manifest(codex_manifest, "Codex", root, errors),
        _validate_manifest(claude_manifest, "Claude", root, errors),
        _validate_skill(root, errors),
    ]
    codex_plugin = _one_plugin(codex_marketplace, "Codex", errors)
    claude_plugin = _one_plugin(claude_marketplace, "Claude", errors)

    if codex_plugin is not None:
        source = codex_plugin.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            errors.append("Codex marketplace source must be a local source object")
        else:
            _inside_root(root, source.get("path"), "Codex marketplace source", errors)
        if codex_plugin.get("policy") != {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        }:
            errors.append("Codex marketplace policy must be AVAILABLE and ON_INSTALL")
        if codex_plugin.get("category") != "Productivity":
            errors.append("Codex marketplace category must be Productivity")

    if claude_plugin is not None:
        _inside_root(root, claude_plugin.get("source"), "Claude marketplace source", errors)
        version = claude_plugin.get("version")
        versions.append(version if isinstance(version, str) else None)

    if versions != [
        RELEASE_VERSION,
        RELEASE_VERSION,
        RELEASE_VERSION,
        RELEASE_VERSION,
    ]:
        errors.append(f"version fields must all equal {RELEASE_VERSION}")

    for required in (
        root / "hooks" / "hooks.json",
        root / "hooks" / "claude_session_start.py",
        root / "skills" / "ai-worklog" / "SKILL.md",
    ):
        if not required.is_file():
            errors.append(f"missing required component: {required.relative_to(root)}")

    for relative in PUBLIC_RELEASE_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing public release file: {relative}")

    return tuple(sorted(set(errors)))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = validate_release(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Release validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
