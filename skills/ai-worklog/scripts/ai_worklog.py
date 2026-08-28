#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence, TextIO


SCRIPTS_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from worklog.dossier import DossierFormatError, PersistenceError  # noqa: E402
from worklog.adapters.codex import RenameError, rename_thread_via_app_server  # noqa: E402
from worklog.agent import AmbiguousAgent, UnsupportedAgent  # noqa: E402
from worklog.model import (  # noqa: E402
    InvalidSessionId,
    SessionRef,
    ValidationError,
    validate_agent_id,
    validate_session_id,
    validate_session_title,
    validate_work_item_id,
)
from worklog.locking import WorkItemBusy  # noqa: E402
from worklog.obsidian import (  # noqa: E402
    CliEnablementRequired,
    ObsidianState,
    VaultSelectionRequired,
    WorklogConfig,
    detect_obsidian,
    discover_vaults,
    load_config,
    resolve_store,
    save_config,
)
from worklog.operations import (  # noqa: E402
    BindRequest,
    BindingConflict,
    DerivedFields,
    NotFound,
    PartialBindFailure,
    RenameRequired,
    StaleDossier,
    SummaryInput,
    TokenError,
    commit_bind,
    prepare_bind,
    query,
    recall,
    rename_staged_thread,
    stage_bind,
    token_vault_path,
)
from worklog.project import Runner, run_command  # noqa: E402


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ai-worklog" / "config.yaml"
DEFAULT_OBSIDIAN_CONFIG_PATH = (
    Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError("command arguments are invalid")


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="ai_worklog.py", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure", add_help=False)
    configure.add_argument("--vault-path", required=True)

    prepare = commands.add_parser("prepare-bind", add_help=False)
    prepare.add_argument("--work-item-id", required=True)
    prepare.add_argument("--session-title", required=True)
    prepare.add_argument("--cwd", required=True)
    prepare.add_argument("--vault")
    prepare.add_argument("--filesystem-fallback", action="store_true")

    stage = commands.add_parser("stage-bind", add_help=False)
    stage.add_argument("--prepared-token", required=True)
    stage.add_argument("--topics-json", required=True)
    stage.add_argument("--project-role", required=True)
    stage.add_argument("--result", required=True)
    stage.add_argument("--next-step", required=True)
    stage.add_argument("--status", required=True)
    stage.add_argument("--summary-json", required=True)
    stage.add_argument("--filesystem-fallback", action="store_true")

    commit = commands.add_parser("commit-bind", add_help=False)
    commit.add_argument("--staged-token", required=True)
    commit.add_argument("--filesystem-fallback", action="store_true")
    commit.add_argument("--rename-confirmed", action="store_true")

    rename = commands.add_parser("rename-thread", add_help=False)
    rename.add_argument("--staged-token", required=True)

    recall_command = commands.add_parser("recall", add_help=False)
    recall_command.add_argument("--work-item-id", required=True)
    recall_command.add_argument("--vault")
    recall_command.add_argument("--filesystem-fallback", action="store_true")

    query_command = commands.add_parser("query", add_help=False)
    query_command.add_argument("--text", required=True)
    query_command.add_argument("--vault")
    query_command.add_argument("--filesystem-fallback", action="store_true")
    return parser


def _emit(stream: TextIO, payload: Mapping[str, object]) -> None:
    json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _success(stream: TextIO, value: object) -> None:
    payload = _jsonable(value)
    if not isinstance(payload, dict):
        payload = {"result": payload}
    _emit(stream, {"ok": True, **payload})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state(run: Runner) -> ObsidianState:
    state = detect_obsidian(run)
    if not state.installed:
        raise ValidationError("Obsidian installation is required")
    return state


def _explicit_vault(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValidationError("vault path is invalid")
    return path


def _store_for_vault(
    vault: Path,
    run: Runner,
    state: ObsidianState,
    *,
    allow_filesystem_fallback: bool = False,
):
    if not vault.is_dir():
        raise ValidationError("vault path is invalid")
    return resolve_store(
        (vault,),
        None,
        run=run,
        state=state,
        allow_filesystem_fallback=allow_filesystem_fallback,
    )


def _prepare_store(
    explicit: str | None,
    *,
    run: Runner,
    state: ObsidianState,
    config_path: Path,
    obsidian_config_path: Path,
    allow_filesystem_fallback: bool = False,
):
    if explicit is not None:
        return _store_for_vault(
            _explicit_vault(explicit),
            run,
            state,
            allow_filesystem_fallback=allow_filesystem_fallback,
        )
    vaults = discover_vaults(obsidian_config_path)
    return resolve_store(
        vaults,
        load_config(config_path),
        run=run,
        state=state,
        allow_filesystem_fallback=allow_filesystem_fallback,
    )


def _json_value(source: str, name: str) -> object:
    try:
        return json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{name} is invalid JSON") from exc


def _json_session_ref(value: object, name: str) -> SessionRef:
    if not isinstance(value, dict) or set(value) != {"agent_id", "session_id"}:
        raise ValidationError(f"{name} is invalid")
    return SessionRef(
        validate_agent_id(value["agent_id"]),
        validate_session_id(value["session_id"]),
    )


def _derived(args: argparse.Namespace) -> DerivedFields:
    topics = _json_value(args.topics_json, "topics")
    summary = _json_value(args.summary_json, "summary")
    expected = {
        "current_progress",
        "unresolved",
        "recommended_session",
        "evidence_sessions",
    }
    if not isinstance(summary, dict) or set(summary) != expected:
        raise ValidationError("summary JSON is invalid")
    recommended = summary["recommended_session"]
    evidence = summary["evidence_sessions"]
    if recommended is not None:
        recommended = _json_session_ref(recommended, "recommended session")
    if not isinstance(evidence, list):
        raise ValidationError("evidence sessions is invalid")
    return DerivedFields(
        topics=topics,  # type: ignore[arg-type]
        project_role=args.project_role,
        result=args.result,
        next_step=args.next_step,
        status=args.status,
        summary=SummaryInput(
            current_progress=summary["current_progress"],
            unresolved=summary["unresolved"],
            recommended_session=recommended,
            evidence_sessions=tuple(
                _json_session_ref(item, "evidence session") for item in evidence
            ),
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    run: Runner = run_command,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: str | None = None,
    config_path: Path | None = None,
    obsidian_config_path: Path | None = None,
    token_root: Path | None = None,
    rename_backend: Callable[[str, str], None] | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    runtime_env = os.environ if env is None else env
    current_time = now or _now()
    selected_config = config_path or Path(
        runtime_env.get("AI_WORKLOG_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    )
    obsidian_config = obsidian_config_path or Path(
        runtime_env.get(
            "AI_WORKLOG_OBSIDIAN_CONFIG_PATH", DEFAULT_OBSIDIAN_CONFIG_PATH
        )
    )
    selected_token_root = token_root
    if selected_token_root is None and runtime_env.get("AI_WORKLOG_TOKEN_ROOT"):
        selected_token_root = Path(runtime_env["AI_WORKLOG_TOKEN_ROOT"])
    selected_lock_root = (
        Path(runtime_env["AI_WORKLOG_LOCK_ROOT"])
        if runtime_env.get("AI_WORKLOG_LOCK_ROOT")
        else None
    )

    try:
        args = _parser().parse_args(argv)
        if args.command == "configure":
            vault = _explicit_vault(args.vault_path)
            if vault not in discover_vaults(obsidian_config):
                raise ValidationError("vault path was not discovered by Obsidian")
            save_config(selected_config, WorklogConfig(vault_path=vault))
            _success(output, {"vault_path": str(vault)})
            return 0

        if args.command in {"prepare-bind", "recall"}:
            validate_work_item_id(args.work_item_id)
        if args.command == "prepare-bind":
            validate_session_title(args.session_title)
        if args.command == "prepare-bind":
            state = _state(run)
            store = _prepare_store(
                args.vault,
                run=run,
                state=state,
                config_path=selected_config,
                obsidian_config_path=obsidian_config,
                allow_filesystem_fallback=args.filesystem_fallback,
            )
            result = prepare_bind(
                BindRequest(
                    work_item_id=args.work_item_id,
                    session_title=args.session_title,
                    cwd=Path(args.cwd),
                ),
                env=runtime_env,
                store=store,
                now=current_time,
                run=run,
                token_root=selected_token_root,
            )
        elif args.command == "stage-bind":
            state = _state(run)
            vault = token_vault_path(
                args.prepared_token, "prepared", selected_token_root, current_time
            )
            store = _store_for_vault(
                vault,
                run,
                state,
                allow_filesystem_fallback=args.filesystem_fallback,
            )
            result = stage_bind(
                args.prepared_token,
                _derived(args),
                store,
                env=runtime_env,
                now=current_time,
                token_root=selected_token_root,
            )
        elif args.command == "commit-bind":
            if not args.rename_confirmed:
                raise RenameRequired("task rename must be confirmed before commit")
            try:
                state = _state(run)
                vault = token_vault_path(
                    args.staged_token, "staged", selected_token_root, current_time
                )
                store = _store_for_vault(
                    vault,
                    run,
                    state,
                    allow_filesystem_fallback=args.filesystem_fallback,
                )
                result = commit_bind(
                    args.staged_token,
                    store,
                    env=runtime_env,
                    now=current_time,
                    token_root=selected_token_root,
                    rename_confirmed=args.rename_confirmed,
                    lock_root=selected_lock_root,
                )
            except PartialBindFailure:
                raise
            except Exception as exc:
                raise PartialBindFailure(exc) from exc
        elif args.command == "rename-thread":
            result = rename_staged_thread(
                args.staged_token,
                env=runtime_env,
                now=current_time,
                rename=(
                    rename_backend
                    if rename_backend is not None
                    else rename_thread_via_app_server
                ),
                token_root=selected_token_root,
            )
        else:
            state = _state(run)
            store = _prepare_store(
                args.vault,
                run=run,
                state=state,
                config_path=selected_config,
                obsidian_config_path=obsidian_config,
                allow_filesystem_fallback=args.filesystem_fallback,
            )
            if args.command == "recall":
                result = recall(args.work_item_id, store)
            else:
                result = {"groups": query(args.text, store)}
        _success(output, result)
        return 0
    except PartialBindFailure as exc:
        _emit(
            errors,
            {
                "ok": False,
                "error_code": exc.cause_error_code,
                "message": str(exc),
                "rename_already_completed": True,
            },
        )
        return 5
    except BindingConflict as exc:
        _emit(
            errors,
            {"ok": False, "error_code": "binding_conflict", "message": str(exc)},
        )
        return 3
    except RenameError as exc:
        _emit(
            errors,
            {"ok": False, "error_code": "rename_required", "message": str(exc)},
        )
        return 2
    except NotFound as exc:
        _emit(
            errors,
            {"ok": False, "error_code": "not_found", "message": str(exc)},
        )
        return 2
    except VaultSelectionRequired as exc:
        _emit(
            errors,
            {
                "ok": False,
                "error_code": "user_action_required",
                "message": str(exc),
                "choices": [str(path) for path in exc.choices],
            },
        )
        return 2
    except CliEnablementRequired as exc:
        _emit(
            errors,
            {
                "ok": False,
                "error_code": "user_action_required",
                "message": str(exc),
                "action": "enable_obsidian_cli",
                "filesystem_fallback_available": True,
            },
        )
        return 2
    except (
        UnsupportedAgent,
        AmbiguousAgent,
        InvalidSessionId,
        TokenError,
        StaleDossier,
        RenameRequired,
        WorkItemBusy,
    ) as exc:
        _emit(
            errors,
            {"ok": False, "error_code": exc.error_code, "message": str(exc)},
        )
        return 2
    except (ValidationError, DossierFormatError, ValueError) as exc:
        _emit(
            errors,
            {"ok": False, "error_code": "invalid_arguments", "message": str(exc)},
        )
        return 2
    except (PersistenceError, OSError):
        _emit(
            errors,
            {
                "ok": False,
                "error_code": "persistence_failure",
                "message": "persistence failed",
            },
        )
        return 4
    except Exception:
        _emit(
            errors,
            {
                "ok": False,
                "error_code": "persistence_failure",
                "message": "operation failed",
            },
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
