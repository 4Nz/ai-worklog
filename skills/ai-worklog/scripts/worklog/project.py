from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


Runner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
SCP_REMOTE_RE = re.compile(r"^(?:[^@/:]+@)?([^:/]+):(.+)$")


@dataclass(frozen=True)
class ProjectIdentity:
    project_name: str
    project_root: str
    repository: str | None


def run_command(
    args: Sequence[str], cwd: Path | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def sanitize_remote(remote: str) -> str | None:
    try:
        parsed = urlsplit(remote)
    except ValueError:
        return None
    if parsed.scheme and "://" in remote:
        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if host is None:
            if (
                parsed.scheme != "file"
                or parsed.netloc
                or not parsed.path.startswith("/")
            ):
                return None
            return urlunsplit((parsed.scheme, "", parsed.path, "", ""))
        normalized_host = f"[{host}]" if ":" in host else host
        netloc = normalized_host if port is None else f"{normalized_host}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    if parsed.scheme and remote.startswith(f"{parsed.scheme}:/"):
        return None

    match = SCP_REMOTE_RE.fullmatch(remote) if "://" not in remote else None
    if match:
        host, path = match.groups()
        if "@" in path:
            return None
        path = re.split(r"[?#]", path, maxsplit=1)[0]
        if not path:
            return None
        return f"ssh://{host}/{path}"
    return remote


def repository_name(remote: str) -> str | None:
    sanitized = sanitize_remote(remote)
    if sanitized is None:
        return None
    parsed = urlsplit(sanitized)
    path = parsed.path if parsed.scheme else sanitized
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    if basename.endswith(".git"):
        basename = basename[:-4]
    if not basename:
        return None
    return basename


def resolve_project(cwd: Path, run: Runner = run_command) -> ProjectIdentity:
    root_result = run(("git", "rev-parse", "--show-toplevel"), cwd)
    root = (
        Path(root_result.stdout.strip()).resolve()
        if root_result.returncode == 0
        else cwd.resolve()
    )
    remote_result = run(("git", "remote", "get-url", "origin"), root)
    remote = (
        sanitize_remote(remote_result.stdout.strip())
        if remote_result.returncode == 0
        else None
    )
    name = repository_name(remote) if remote else None
    if name is None:
        name = root.name
    return ProjectIdentity(project_name=name, project_root=str(root), repository=remote)
