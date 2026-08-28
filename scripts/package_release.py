#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import importlib.util
import os
from pathlib import Path
import tarfile


_VALIDATOR_PATH = Path(__file__).with_name("validate_release.py")
_VALIDATOR_SPEC = importlib.util.spec_from_file_location("ai_worklog_release_validator", _VALIDATOR_PATH)
if _VALIDATOR_SPEC is None or _VALIDATOR_SPEC.loader is None:
    raise RuntimeError("release validator is not importable")
_VALIDATOR = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR)
RELEASE_VERSION = _VALIDATOR.RELEASE_VERSION
validate_release = _VALIDATOR.validate_release


ARCHIVE_ROOT = f"ai-worklog-{RELEASE_VERSION}"
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".worktrees",
        ".superpowers",
        "__pycache__",
        "dist",
    }
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".DS_Store")


def _included(relative: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if relative.parts[:2] == ("docs", "superpowers"):
        return False
    return not relative.name.endswith(EXCLUDED_SUFFIXES)


def _tar_info(source: Path, archive_name: str, epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(archive_name)
    info.mtime = epoch
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if source.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
        info.size = source.stat().st_size
    return info


def package_release(root: Path, output_dir: Path) -> Path:
    root = root.resolve()
    errors = validate_release(root)
    if errors:
        raise ValueError("release validation failed: " + "; ".join(errors))

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{ARCHIVE_ROOT}.tar.gz"
    epoch_text = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        epoch = int(epoch_text)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must not be negative")

    sources = [path for path in root.rglob("*") if _included(path.relative_to(root))]
    sources.sort(key=lambda path: path.relative_to(root).as_posix())

    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as package:
                root_info = _tar_info(root, ARCHIVE_ROOT, epoch)
                package.addfile(root_info)
                for source in sources:
                    relative = source.relative_to(root).as_posix()
                    info = _tar_info(source, f"{ARCHIVE_ROOT}/{relative}", epoch)
                    if source.is_dir():
                        package.addfile(info)
                    else:
                        with source.open("rb") as content:
                            package.addfile(info, content)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a validated AI Worklog release archive")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = package_release(root, args.output_dir)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
