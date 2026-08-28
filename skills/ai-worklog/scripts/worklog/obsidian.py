from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from .project import Runner, run_command


RECORDS_FOLDER = Path("AI-Coding-Archive/WorkItems")
RECORDS_FOLDER_TEXT = RECORDS_FOLDER.as_posix()
DEFAULT_APPS = (Path("/Applications/Obsidian.app"),)
_CANDIDATE_RE = re.compile(
    r"(?:^|[\s\"'])(AI-Coding-Archive/WorkItems/[^\s\"']+\.md)(?=$|[\s\"'])"
)


@dataclass(frozen=True)
class ObsidianState:
    installed: bool
    cli_present: bool
    cli_status: Literal[
        "usable", "registration_required", "temporarily_unavailable"
    ]

    @property
    def cli_usable(self) -> bool:
        return self.cli_status == "usable"


@dataclass(frozen=True)
class WorklogConfig:
    vault_path: Path
    records_folder: str = RECORDS_FOLDER_TEXT
    write_mode: Literal["auto"] = "auto"


@dataclass(frozen=True)
class SearchMatch:
    relative_path: Path
    text: str


class VaultSelectionRequired(ValueError):
    """Raised when the caller must ask the user to select an Obsidian vault."""

    def __init__(self, choices: tuple[Path, ...]):
        super().__init__("vault selection required")
        self.choices = choices


class CliEnablementRequired(ValueError):
    """Raised when official CLI enablement needs an explicit user decision."""

    def __init__(self):
        super().__init__("Obsidian CLI enablement choice is required")


def records_dir(vault: Path) -> Path:
    root = vault.resolve()
    candidate = (root / RECORDS_FOLDER).resolve()
    candidate.relative_to(root)
    return candidate


def detect_obsidian(
    run: Runner, applications: Sequence[Path] = DEFAULT_APPS
) -> ObsidianState:
    app_present = any(path.is_dir() for path in applications)
    try:
        version = run(("obsidian", "version"), None)
    except OSError:
        return ObsidianState(
            installed=app_present,
            cli_present=False,
            cli_status=(
                "registration_required"
                if app_present
                else "temporarily_unavailable"
            ),
        )
    cli_present = True
    cli_usable = version.returncode == 0 and bool(version.stdout.strip())
    output = f"{version.stdout}\n{version.stderr}".casefold()
    registration_required = any(
        phrase in output
        for phrase in (
            "cli is not enabled",
            "cli not enabled",
            "not enabled",
            "cli is disabled",
            "enable the cli",
            "register the cli",
            "cli registration",
            "not registered",
            "unregistered",
        )
    )
    return ObsidianState(
        installed=app_present or cli_usable,
        cli_present=cli_present,
        cli_status=(
            "usable"
            if cli_usable
            else (
                "registration_required"
                if registration_required
                else "temporarily_unavailable"
            )
        ),
    )


def discover_vaults(config_path: Path) -> tuple[Path, ...]:
    """Return existing vault directories named by Obsidian's JSON configuration."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, dict) or not isinstance(raw.get("vaults"), dict):
        return ()

    discovered: list[Path] = []
    for key, metadata in raw["vaults"].items():
        candidate = metadata.get("path") if isinstance(metadata, dict) else None
        if not isinstance(candidate, str):
            candidate = key
        if not isinstance(candidate, str) or not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute() or not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved not in discovered:
            discovered.append(resolved)
    return tuple(discovered)


def _valid_config(config: WorklogConfig) -> bool:
    return (
        isinstance(config.vault_path, Path)
        and config.vault_path.is_absolute()
        and config.records_folder == RECORDS_FOLDER_TEXT
        and config.write_mode == "auto"
    )


def load_config(path: Path) -> WorklogConfig | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    values: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"(vault_path|records_folder|write_mode): ([^\r\n]+)", line)
        if match is None or match.group(1) in values:
            return None
        values[match.group(1)] = match.group(2)
    if set(values) != {"vault_path", "records_folder", "write_mode"}:
        return None
    source_vault_path = Path(values["vault_path"])
    if not source_vault_path.is_absolute():
        return None
    vault_path = source_vault_path.expanduser()
    config = WorklogConfig(
        vault_path=vault_path.resolve(),
        records_folder=values["records_folder"],
        write_mode=values["write_mode"],  # type: ignore[arg-type]
    )
    return config if _valid_config(config) else None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
        raise


def save_config(path: Path, config: WorklogConfig) -> None:
    if not _valid_config(config):
        raise ValueError("invalid worklog config")
    text = (
        f"vault_path: {config.vault_path.resolve()}\n"
        f"records_folder: {RECORDS_FOLDER_TEXT}\n"
        "write_mode: auto\n"
    )
    _atomic_write(path, text)


def _select_vault(
    vaults: Sequence[Path], config: WorklogConfig | None
) -> Path:
    choices: list[Path] = []
    for vault in vaults:
        resolved = vault.resolve()
        if resolved not in choices:
            choices.append(resolved)
    if config is not None:
        if _valid_config(config):
            cached = config.vault_path.resolve()
            if cached in choices:
                return cached
        raise VaultSelectionRequired(tuple(choices))
    if len(choices) == 1:
        return choices[0]
    raise VaultSelectionRequired(tuple(choices))


class StoreResolution:
    """CLI-first work-item store with a filesystem fallback in auto mode."""

    def __init__(self, vault_path: Path, mode: Literal["cli", "filesystem"], run: Runner):
        self.vault_path = vault_path.resolve()
        self.records_dir = records_dir(self.vault_path)
        self.mode: Literal["cli", "filesystem"] = mode
        self._run = run

    def _path(self, relative_path: Path) -> tuple[Path, Path]:
        if relative_path.is_absolute():
            raise ValueError("record path must be relative to the selected vault")
        candidate = (self.vault_path / relative_path).resolve()
        try:
            candidate.relative_to(self.records_dir)
            vault_relative = candidate.relative_to(self.vault_path)
        except ValueError as exc:
            raise ValueError("record path is outside the fixed records directory") from exc
        return candidate, vault_relative

    def _filesystem_read(self, candidate: Path) -> str:
        return candidate.read_text(encoding="utf-8")

    def _filesystem_write(self, candidate: Path, text: str) -> None:
        _atomic_write(candidate, text)

    def read(self, relative_path: Path) -> str:
        candidate, vault_relative = self._path(relative_path)
        if self.mode == "cli":
            try:
                completed = self._run(
                    ("obsidian", "read", f"path={vault_relative.as_posix()}"),
                    self.vault_path,
                )
            except OSError:
                completed = None
            if completed is not None and completed.returncode == 0:
                return completed.stdout
            self.mode = "filesystem"
        return self._filesystem_read(candidate)

    def write(self, relative_path: Path, text: str) -> None:
        candidate, vault_relative = self._path(relative_path)
        if self.mode == "cli":
            try:
                completed = self._run(
                    (
                        "obsidian",
                        "create",
                        f"path={vault_relative.as_posix()}",
                        f"content={text}",
                        "overwrite",
                    ),
                    self.vault_path,
                )
            except OSError:
                completed = None
            if completed is not None and completed.returncode == 0:
                return
            self.mode = "filesystem"
        self._filesystem_write(candidate, text)

    def _cli_candidates(self, output: str) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for candidate_text in _CANDIDATE_RE.findall(output):
            try:
                _, normalized = self._path(Path(candidate_text))
            except ValueError:
                continue
            if normalized not in candidates:
                candidates.append(normalized)
        return tuple(candidates)

    def _filesystem_search(self, query: str) -> tuple[SearchMatch, ...]:
        if not self.records_dir.is_dir():
            return ()
        matches: list[SearchMatch] = []
        for candidate in sorted(self.records_dir.glob("*.md")):
            resolved = candidate.resolve()
            try:
                relative_path = resolved.relative_to(self.vault_path)
                resolved.relative_to(self.records_dir)
            except ValueError:
                continue
            text = self._filesystem_read(resolved)
            if query in text:
                matches.append(SearchMatch(relative_path, text))
        return tuple(matches)

    def search(self, query: str) -> tuple[SearchMatch, ...]:
        if self.mode == "cli":
            try:
                completed = self._run(
                    (
                        "obsidian",
                        "search:context",
                        f"query={query}",
                        f"path={RECORDS_FOLDER_TEXT}",
                    ),
                    self.vault_path,
                )
            except OSError:
                completed = None
            if completed is not None and completed.returncode == 0:
                candidates = self._cli_candidates(completed.stdout)
                if completed.stdout.strip() and not candidates:
                    self.mode = "filesystem"
                    return self._filesystem_search(query)
                matches: list[SearchMatch] = []
                for relative_path in candidates:
                    text = self.read(relative_path)
                    if query in text:
                        matches.append(SearchMatch(relative_path, text))
                return tuple(matches)
            self.mode = "filesystem"
        return self._filesystem_search(query)


def resolve_store(
    vaults: Sequence[Path],
    config: WorklogConfig | None,
    run: Runner = run_command,
    state: ObsidianState | None = None,
    allow_filesystem_fallback: bool = False,
) -> StoreResolution:
    vault = _select_vault(vaults, config)
    current_state = state if state is not None else detect_obsidian(run)
    if (
        current_state.cli_status == "registration_required"
        and not allow_filesystem_fallback
    ):
        raise CliEnablementRequired()
    mode: Literal["cli", "filesystem"] = (
        "cli" if current_state.cli_usable else "filesystem"
    )
    return StoreResolution(vault, mode, run)
