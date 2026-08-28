from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from .agent import (
    AgentSession,
    agent_session,
    detect_current_session,
    render_resume_command,
)
from .dossier import Dossier, DossierFormatError, PersistenceError, SummaryFields, new_dossier
from .locking import archive_lock
from .model import (
    AgentId,
    RenameMode,
    SessionRecord,
    SessionRef,
    ValidationError,
    format_session_ref,
    record_key,
    validate_agent_id,
    validate_session_id,
    validate_session_title,
    validate_work_item_id,
)
from .obsidian import RECORDS_FOLDER, SearchMatch
from .project import ProjectIdentity, Runner, resolve_project, run_command, sanitize_remote


TOKEN_SCHEMA_VERSION = 1
TOKEN_LIFETIME = timedelta(minutes=10)
DOSSIER_QUERY = "type: ai-work-item-history"


class BindingConflict(RuntimeError):
    """Raised when the current session is bound to another work item."""

    error_code = "binding_conflict"


class TokenError(ValidationError):
    """Raised when a bind capability token is invalid or stale."""

    error_code = "token_error"


class StaleDossier(TokenError):
    """Raised when a dossier no longer matches a prepared snapshot."""

    error_code = "stale_dossier"


class RenameRequired(ValidationError):
    """Raised when a bind has not completed its required task rename."""

    error_code = "rename_required"


class PartialBindFailure(PersistenceError):
    """Raised when rename succeeded but dossier persistence did not."""

    rename_already_completed = True

    def __init__(self, cause: Exception):
        super().__init__(
            "task rename completed, but dossier persistence failed; rerun the same bind"
        )
        self.cause_error_code = getattr(
            cause, "error_code", "rename_only_partial_failure"
        )


class NotFound(LookupError):
    """Raised when an exact work-item lookup has no matching dossier."""


class WorkItemStore(Protocol):
    vault_path: Path

    def search(self, query: str) -> Sequence[SearchMatch]: ...

    def read(self, relative_path: Path) -> str: ...

    def write(self, relative_path: Path, text: str) -> None: ...


@dataclass(frozen=True)
class BindRequest:
    work_item_id: str
    session_title: str
    cwd: Path


@dataclass(frozen=True)
class PreparedBind:
    schema_version: int
    prepared_at: str
    expires_at: str
    agent_id: AgentId
    session_id: str
    work_item_id: str
    session_title: str
    target_title: str
    project: ProjectIdentity
    vault_path: str
    dossier_digest: str | None

    @property
    def ref(self) -> SessionRef:
        return SessionRef(self.agent_id, self.session_id)


@dataclass(frozen=True)
class PrepareResult:
    prepared_token: str
    agent_id: AgentId
    session_id: str
    target_title: str
    project: ProjectIdentity
    vault_path: str
    existing_record: SessionRecord | None
    archive_summary: RecallSummary | None
    archive_sessions: tuple[SessionRecord, ...]


@dataclass(frozen=True)
class SummaryInput:
    current_progress: str
    unresolved: str
    recommended_session: SessionRef | None
    evidence_sessions: Sequence[SessionRef]


@dataclass(frozen=True)
class DerivedFields:
    topics: Sequence[str]
    project_role: str
    result: str
    next_step: str
    status: str
    summary: SummaryInput


@dataclass(frozen=True)
class StagedBind:
    schema_version: int
    staged_at: str
    expires_at: str
    agent_id: AgentId
    session_id: str
    work_item_id: str
    session_title: str
    target_title: str
    project: ProjectIdentity
    vault_path: str
    dossier_digest: str | None
    record: SessionRecord
    summary: SummaryFields

    @property
    def ref(self) -> SessionRef:
        return SessionRef(self.agent_id, self.session_id)


@dataclass(frozen=True)
class StageResult:
    staged_token: str
    target_title: str
    agent_id: AgentId
    rename_mode: RenameMode
    manual_rename_command: str | None


@dataclass(frozen=True)
class RenameResult:
    target_title: str


@dataclass(frozen=True)
class CommitResult:
    dossier_path: str
    resume_command: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RecallSummary:
    current_progress: str
    unresolved: str
    recommended_session: SessionRef | None
    evidence_sessions: tuple[SessionRef, ...]


@dataclass(frozen=True)
class RecallSession:
    agent_id: AgentId
    session_id: str
    title: str
    occurred_at: str
    project: str
    topics: tuple[str, ...]
    result: str
    next_step: str
    status: str
    resume_command: str


@dataclass(frozen=True)
class RecallResult:
    work_item_id: str
    summary: RecallSummary
    projects: tuple[str, ...]
    sessions: tuple[RecallSession, ...]


FIELD_RANK = {
    "session_id": 0,
    "title": 1,
    "topics": 2,
    "project": 3,
    "result": 4,
    "next_step": 5,
    "summary": 6,
}


@dataclass(frozen=True)
class WorkItemEvidence:
    field: Literal["summary"]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class QuerySession:
    agent_id: AgentId
    session_id: str
    title: str
    occurred_at: str
    project: str
    topics: tuple[str, ...]
    result: str
    next_step: str
    status: str
    resume_command: str
    rank: int
    matched_fields: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class QueryGroup:
    work_item_id: str
    updated_at: str
    sessions: tuple[QuerySession, ...]
    work_item_evidence: WorkItemEvidence | None
    rank: int


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _token_directory(root: Path, ref: SessionRef) -> Path:
    directory = root / record_key(ref)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _fsync_directory(directory: Path) -> None:
    unsupported = {
        errno.EBADF,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def _remove_token(path: Path, *, missing_ok: bool = False) -> None:
    path.unlink(missing_ok=missing_ok)
    _fsync_directory(path.parent)


def _write_token(
    kind: str,
    value: Mapping[str, object],
    ref: SessionRef,
    token_root: Path | None,
) -> str:
    root = token_root or Path(tempfile.gettempdir()) / "ai-worklog"
    directory = _token_directory(root, ref)
    path = directory / f"{kind}-{secrets.token_hex(16)}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
            json.dump({"kind": kind, "value": value}, token_file, ensure_ascii=False)
            token_file.write("\n")
            token_file.flush()
            os.fsync(token_file.fileno())
        _fsync_directory(directory)
    except Exception:
        try:
            _remove_token(path)
        except FileNotFoundError:
            pass
        raise
    return str(path)


def _token_root(token_root: Path | None) -> Path:
    return (token_root or Path(tempfile.gettempdir()) / "ai-worklog").resolve()


def _read_token(
    token: str | Path, expected_kind: str, token_root: Path | None
) -> Mapping[str, Any]:
    root = _token_root(token_root)
    source = Path(token)
    try:
        if source.is_symlink():
            raise TokenError("bind token is invalid")
        path = source.resolve(strict=True)
        path.relative_to(root)
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TokenError("bind token is invalid")
        if metadata.st_size > 1024 * 1024:
            raise TokenError("bind token is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except TokenError:
        raise
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError("bind token is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != expected_kind
        or not isinstance(payload.get("value"), dict)
    ):
        raise TokenError("bind token is invalid")
    return cast(Mapping[str, Any], payload["value"])


def _project(value: object) -> ProjectIdentity:
    if not isinstance(value, dict) or set(value) != {
        "project_name",
        "project_root",
        "repository",
    }:
        raise TokenError("bind token is invalid")
    name = value["project_name"]
    root = value["project_root"]
    repository = value["repository"]
    if not isinstance(name, str) or not isinstance(root, str):
        raise TokenError("bind token is invalid")
    if repository is not None and not isinstance(repository, str):
        raise TokenError("bind token is invalid")
    return ProjectIdentity(name, root, repository)


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _token_agent_id(value: object) -> AgentId:
    try:
        return validate_agent_id(value)
    except ValidationError as exc:
        raise TokenError("bind token is invalid") from exc


def _validate_token_window(created_at: str, expires_at: str, current: datetime) -> None:
    try:
        created = _parse_time(created_at)
        expires = _parse_time(expires_at)
    except ValueError as exc:
        raise TokenError("bind token is invalid") from exc
    if expires - created != TOKEN_LIFETIME or created > current:
        raise TokenError("bind token is invalid")
    if current > expires:
        raise TokenError("bind token expired")


def _canonical_absolute_path(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() and str(path.resolve()) == value


def _prepared(value: Mapping[str, Any], current: datetime) -> PreparedBind:
    expected = {
        "schema_version",
        "prepared_at",
        "expires_at",
        "agent_id",
        "session_id",
        "work_item_id",
        "session_title",
        "target_title",
        "project",
        "vault_path",
        "dossier_digest",
    }
    if set(value) != expected or value.get("schema_version") != TOKEN_SCHEMA_VERSION:
        raise TokenError("bind token is invalid")
    strings = (
        "prepared_at",
        "expires_at",
        "agent_id",
        "session_id",
        "work_item_id",
        "session_title",
        "target_title",
        "vault_path",
    )
    if any(not isinstance(value.get(name), str) for name in strings):
        raise TokenError("bind token is invalid")
    digest = value.get("dossier_digest")
    if digest is not None and not _valid_digest(digest):
        raise TokenError("bind token is invalid")
    prepared = PreparedBind(
        schema_version=TOKEN_SCHEMA_VERSION,
        prepared_at=value["prepared_at"],
        expires_at=value["expires_at"],
        agent_id=_token_agent_id(value["agent_id"]),
        session_id=value["session_id"],
        work_item_id=value["work_item_id"],
        session_title=value["session_title"],
        target_title=value["target_title"],
        project=_project(value["project"]),
        vault_path=value["vault_path"],
        dossier_digest=digest,
    )
    try:
        validate_session_id(prepared.session_id)
        validate_work_item_id(prepared.work_item_id)
        session_title = validate_session_title(prepared.session_title)
        project_name = _single_line(prepared.project.project_name, "project name")
        _validate_token_window(prepared.prepared_at, prepared.expires_at, current)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, TokenError):
            raise
        raise TokenError("bind token is invalid") from exc
    if (
        not _canonical_absolute_path(prepared.vault_path)
        or not _canonical_absolute_path(prepared.project.project_root)
        or project_name != prepared.project.project_name
        or session_title != prepared.session_title
        or (
            prepared.project.repository is not None
            and sanitize_remote(prepared.project.repository)
            != prepared.project.repository
        )
        or prepared.target_title
        != f"{prepared.work_item_id} {prepared.session_title}"
    ):
        raise TokenError("bind token is invalid")
    return prepared


def _record(value: object) -> SessionRecord:
    if not isinstance(value, dict):
        raise TokenError("bind token is invalid")
    expected = {
        "agent_id",
        "session_id",
        "title",
        "occurred_at",
        "project_name",
        "project_root",
        "repository",
        "topics",
        "result",
        "next_step",
        "status",
    }
    if set(value) != expected or not isinstance(value.get("topics"), list):
        raise TokenError("bind token is invalid")
    string_fields = {
        "agent_id",
        "session_id",
        "title",
        "occurred_at",
        "project_name",
        "project_root",
        "result",
        "next_step",
        "status",
    }
    if any(not isinstance(value.get(name), str) for name in string_fields):
        raise TokenError("bind token is invalid")
    if not all(isinstance(topic, str) for topic in value["topics"]):
        raise TokenError("bind token is invalid")
    if value["repository"] is not None and not isinstance(value["repository"], str):
        raise TokenError("bind token is invalid")
    try:
        return SessionRecord(
            agent_id=validate_agent_id(value["agent_id"]),
            session_id=value["session_id"],
            title=value["title"],
            occurred_at=value["occurred_at"],
            project_name=value["project_name"],
            project_root=value["project_root"],
            repository=value["repository"],
            topics=tuple(value["topics"]),
            result=value["result"],
            next_step=value["next_step"],
            status=value["status"],
        )
    except (TypeError, KeyError, ValidationError) as exc:
        raise TokenError("bind token is invalid") from exc


def _session_ref(value: object, name: str) -> SessionRef:
    if not isinstance(value, Mapping) or set(value) != {"agent_id", "session_id"}:
        raise ValidationError(f"{name} is invalid")
    return SessionRef(
        validate_agent_id(value["agent_id"]),
        validate_session_id(value["session_id"]),
    )


def _summary(value: object) -> SummaryFields:
    if not isinstance(value, dict) or set(value) != {
        "current_progress",
        "unresolved",
        "recommended_session",
        "evidence_sessions",
        "project_roles",
    }:
        raise TokenError("bind token is invalid")
    evidence = value["evidence_sessions"]
    roles = value["project_roles"]
    if (
        not isinstance(evidence, list)
        or not isinstance(roles, dict)
        or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in roles.items()
        )
        or not isinstance(value["current_progress"], str)
        or not isinstance(value["unresolved"], str)
    ):
        raise TokenError("bind token is invalid")
    try:
        recommended = value["recommended_session"]
        return SummaryFields(
            current_progress=value["current_progress"],
            unresolved=value["unresolved"],
            recommended_session=(
                None
                if recommended is None
                else _session_ref(recommended, "recommended session")
            ),
            evidence_sessions=tuple(
                _session_ref(item, "summary evidence") for item in evidence
            ),
            project_roles=roles,
        )
    except (TypeError, KeyError, ValidationError) as exc:
        raise TokenError("bind token is invalid") from exc


def _staged(value: Mapping[str, Any], current: datetime) -> StagedBind:
    expected = {
        "schema_version",
        "staged_at",
        "expires_at",
        "agent_id",
        "session_id",
        "work_item_id",
        "session_title",
        "target_title",
        "project",
        "vault_path",
        "dossier_digest",
        "record",
        "summary",
    }
    if set(value) != expected or value.get("schema_version") != TOKEN_SCHEMA_VERSION:
        raise TokenError("bind token is invalid")
    digest = value.get("dossier_digest")
    if digest is not None and not _valid_digest(digest):
        raise TokenError("bind token is invalid")
    required_strings = (
        "staged_at",
        "expires_at",
        "agent_id",
        "session_id",
        "work_item_id",
        "session_title",
        "target_title",
        "vault_path",
    )
    if any(not isinstance(value.get(name), str) for name in required_strings):
        raise TokenError("bind token is invalid")
    staged = StagedBind(
        schema_version=TOKEN_SCHEMA_VERSION,
        staged_at=value["staged_at"],
        expires_at=value["expires_at"],
        agent_id=_token_agent_id(value["agent_id"]),
        session_id=value["session_id"],
        work_item_id=value["work_item_id"],
        session_title=value["session_title"],
        target_title=value["target_title"],
        project=_project(value["project"]),
        vault_path=value["vault_path"],
        dossier_digest=digest,
        record=_record(value["record"]),
        summary=_summary(value["summary"]),
    )
    try:
        validate_session_id(staged.session_id)
        validate_work_item_id(staged.work_item_id)
        session_title = validate_session_title(staged.session_title)
        project_name = _single_line(staged.project.project_name, "project name")
        _validate_token_window(staged.staged_at, staged.expires_at, current)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, TokenError):
            raise
        raise TokenError("bind token is invalid") from exc
    record = staged.record
    allowed_status = {"进行中", "已完成", "暂停", "未知"}
    try:
        topics = _topics(record.topics)
        result = _single_line(record.result, "result", unknown=True)
        next_step = _single_line(record.next_step, "next step", unknown=True)
        current_progress = _single_line(
            staged.summary.current_progress, "summary progress", unknown=True
        )
        unresolved = _single_line(
            staged.summary.unresolved, "summary unresolved", unknown=True
        )
        evidence = _evidence(staged.summary.evidence_sessions)
        recommended = staged.summary.recommended_session
        if recommended is not None:
            recommended = _validated_ref(recommended, "recommended session")
        normalized_roles: dict[str, tuple[str, str]] = {}
        for name, role in staged.summary.project_roles.items():
            normalized_name = _single_line(name, "project name")
            normalized_role = _single_line(role, "project role", unknown=True)
            folded = normalized_name.casefold()
            if folded in normalized_roles:
                raise ValidationError("project role is invalid")
            normalized_roles[folded] = (normalized_name, normalized_role)
    except ValidationError as exc:
        raise TokenError("bind token is invalid") from exc
    current_role = normalized_roles.get(staged.project.project_name.casefold())
    if (
        not _canonical_absolute_path(staged.vault_path)
        or not _canonical_absolute_path(staged.project.project_root)
        or project_name != staged.project.project_name
        or session_title != staged.session_title
        or staged.target_title
        != f"{staged.work_item_id} {staged.session_title}"
        or (
            staged.project.repository is not None
            and sanitize_remote(staged.project.repository) != staged.project.repository
        )
        or record.ref != staged.ref
        or record.title != staged.target_title
        or record.occurred_at != staged.staged_at
        or record.project_name != staged.project.project_name
        or record.project_root != staged.project.project_root
        or record.repository != staged.project.repository
        or record.status not in allowed_status
        or topics != record.topics
        or result != record.result
        or next_step != record.next_step
        or current_progress != staged.summary.current_progress
        or unresolved != staged.summary.unresolved
        or evidence != staged.summary.evidence_sessions
        or recommended != staged.summary.recommended_session
        or current_role is None
        or current_role[0] != staged.project.project_name
        or any(
            (name, role) != normalized_roles[folded]
            for name, role in staged.summary.project_roles.items()
            for folded in (name.casefold(),)
        )
    ):
        raise TokenError("bind token is invalid")
    return staged


def token_vault_path(
    token: str | Path,
    kind: Literal["prepared", "staged"],
    token_root: Path | None = None,
    now: str | None = None,
) -> Path:
    value = _read_token(token, kind, token_root)
    if now is not None:
        current = _parse_time(now)
        parsed = _prepared(value, current) if kind == "prepared" else _staged(value, current)
        return Path(parsed.vault_path)
    vault = value.get("vault_path")
    if not isinstance(vault, str) or not _canonical_absolute_path(vault):
        raise TokenError("bind token is invalid")
    return Path(vault)


def _archive(
    store: WorkItemStore, *, binding_ref: SessionRef | None = None
) -> tuple[tuple[SearchMatch, Dossier], ...]:
    loaded: list[tuple[SearchMatch, Dossier]] = []
    for match in store.search(DOSSIER_QUERY):
        if (
            match.relative_path.is_absolute()
            or match.relative_path.parent != RECORDS_FOLDER
            or match.relative_path.suffix != ".md"
        ):
            continue
        dossier = Dossier.parse(match.text)
        expected_path = RECORDS_FOLDER / f"{dossier.work_item_id}.md"
        if match.relative_path != expected_path:
            raise DossierFormatError("work item dossier path is invalid")
        loaded.append((match, dossier))
    archive = tuple(loaded)
    work_items: set[str] = set()
    sessions: set[SessionRef] = set()
    for _, dossier in archive:
        folded = dossier.work_item_id.casefold()
        if folded in work_items:
            raise DossierFormatError("duplicate case-folded work item ID")
        work_items.add(folded)
        for ref in dossier.records:
            if ref in sessions and ref != binding_ref:
                raise DossierFormatError("session ID appears in multiple dossiers")
            sessions.add(ref)
    return archive


def _summary_fields(dossier: Dossier) -> RecallSummary:
    summary = dossier.summary
    return RecallSummary(
        current_progress=summary.current_progress,
        unresolved=summary.unresolved,
        recommended_session=summary.recommended_session,
        evidence_sessions=summary.evidence_sessions,
    )


def _recall_session(record: SessionRecord) -> RecallSession:
    return RecallSession(
        agent_id=record.agent_id,
        session_id=record.session_id,
        title=record.title,
        occurred_at=record.occurred_at,
        project=record.project_name,
        topics=record.topics,
        result=record.result,
        next_step=record.next_step,
        status=record.status,
        resume_command=render_resume_command(record.ref),
    )


def recall(work_item_id: str, store: WorkItemStore) -> RecallResult:
    validated_id = validate_work_item_id(work_item_id)
    target = _target(_archive(store), validated_id)
    if target is None:
        raise NotFound(f"work item {validated_id} was not found")
    dossier = target[1]
    records = tuple(
        sorted(
            dossier.records.values(),
            key=lambda record: (
                _parse_time(record.occurred_at),
                record.agent_id,
                record.session_id,
            ),
            reverse=True,
        )
    )
    projects = tuple(
        value
        for _, value in sorted(
            {
                record.project_name.casefold(): record.project_name
                for record in records
            }.items()
        )
    )
    return RecallResult(
        work_item_id=dossier.work_item_id,
        summary=_summary_fields(dossier),
        projects=projects,
        sessions=tuple(_recall_session(record) for record in records),
    )


def _query_text(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValidationError("query text is invalid")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValidationError("query text is invalid")
    text = value.strip()
    if not text:
        raise ValidationError("query text is invalid")
    return text


def _updated_at(dossier: Dossier) -> str:
    if not dossier.text.startswith("---\n"):
        raise DossierFormatError("missing dossier frontmatter")
    ending = dossier.text.find("\n---\n", 4)
    if ending < 0:
        raise DossierFormatError("missing dossier frontmatter")
    frontmatter = dossier.text[4:ending]
    match = re.search(r"^updated_at: (.+)$", frontmatter, re.MULTILINE)
    if match is None:
        raise DossierFormatError("missing frontmatter updated_at")
    value = match.group(1)
    _parse_time(value)
    return value


def _session_match(record: SessionRecord, folded_query: str) -> QuerySession | None:
    fields: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("session_id", (record.session_id,)),
        ("title", (record.title,)),
        ("topics", record.topics),
        ("project", (record.project_name,)),
        ("result", (record.result,)),
        ("next_step", (record.next_step,)),
    )
    matched_fields: list[str] = []
    evidence: list[str] = []
    for field, values in fields:
        matches = [value for value in values if folded_query in value.casefold()]
        if matches:
            matched_fields.append(field)
            evidence.extend(matches)
    if not matched_fields:
        return None
    return QuerySession(
        agent_id=record.agent_id,
        session_id=record.session_id,
        title=record.title,
        occurred_at=record.occurred_at,
        project=record.project_name,
        topics=record.topics,
        result=record.result,
        next_step=record.next_step,
        status=record.status,
        resume_command=render_resume_command(record.ref),
        rank=FIELD_RANK[matched_fields[0]],
        matched_fields=tuple(matched_fields),
        evidence=tuple(evidence),
    )


def _summary_match(
    summary: RecallSummary, folded_query: str
) -> WorkItemEvidence | None:
    values = (
        summary.current_progress,
        summary.unresolved,
        *(
            (format_session_ref(summary.recommended_session),)
            if summary.recommended_session
            else ()
        ),
        *(format_session_ref(ref) for ref in summary.evidence_sessions),
    )
    evidence = tuple(value for value in values if folded_query in value.casefold())
    return WorkItemEvidence("summary", evidence) if evidence else None


def query(text: str, store: WorkItemStore) -> tuple[QueryGroup, ...]:
    folded_query = _query_text(text).casefold()
    groups: list[QueryGroup] = []
    for _, dossier in _archive(store):
        sessions = [
            match
            for record in dossier.records.values()
            if (match := _session_match(record, folded_query)) is not None
        ]
        sessions.sort(key=lambda match: (match.agent_id, match.session_id))
        sessions.sort(key=lambda match: _parse_time(match.occurred_at), reverse=True)
        sessions.sort(key=lambda match: match.rank)
        work_item_evidence = _summary_match(
            _summary_fields(dossier), folded_query
        )
        if not sessions and work_item_evidence is None:
            continue
        rank = min(
            [match.rank for match in sessions]
            + ([FIELD_RANK["summary"]] if work_item_evidence else [])
        )
        groups.append(
            QueryGroup(
                work_item_id=dossier.work_item_id,
                updated_at=_updated_at(dossier),
                sessions=tuple(sessions),
                work_item_evidence=work_item_evidence,
                rank=rank,
            )
        )
    groups.sort(key=lambda group: group.work_item_id.casefold())
    groups.sort(key=lambda group: _parse_time(group.updated_at), reverse=True)
    groups.sort(key=lambda group: group.rank)
    return tuple(groups)


def _target(
    archive: Sequence[tuple[SearchMatch, Dossier]], work_item_id: str
) -> tuple[SearchMatch, Dossier] | None:
    return next(
        (
            item
            for item in archive
            if item[1].work_item_id.casefold() == work_item_id.casefold()
        ),
        None,
    )


def _validate_snapshot(
    *,
    ref: SessionRef,
    work_item_id: str,
    expected_digest: str | None,
    store: WorkItemStore,
) -> tuple[SearchMatch, Dossier] | None:
    archive = _archive(store, binding_ref=ref)
    for _, dossier in archive:
        if (
            ref in dossier.records
            and dossier.work_item_id.casefold() != work_item_id.casefold()
        ):
            raise BindingConflict(
                f"Session is already bound to {dossier.work_item_id}"
            )
    target = _target(archive, work_item_id)
    actual_digest = _digest(target[0].text) if target else None
    if actual_digest != expected_digest:
        raise StaleDossier("dossier changed; rerun prepare-bind")
    return target


def _check_runtime_session(
    *, expected: SessionRef, env: Mapping[str, str]
) -> AgentSession:
    current = detect_current_session(env)
    if current.ref != expected:
        raise TokenError("runtime Agent or Session ID changed")
    return current


def _single_line(value: object, name: str, *, unknown: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} is invalid")
    if "\n" in value or "\r" in value or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValidationError(f"{name} is invalid")
    cleaned = value.strip()
    if not cleaned:
        if unknown:
            return "未知"
        raise ValidationError(f"{name} is invalid")
    return cleaned


def _topics(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValidationError("topics are invalid")
    topics: list[str] = []
    seen: set[str] = set()
    for value in values:
        topic = _single_line(value, "topic")
        if topic not in seen:
            seen.add(topic)
            topics.append(topic)
    return tuple(topics)


def _validated_ref(value: object, name: str) -> SessionRef:
    if not isinstance(value, SessionRef):
        raise ValidationError(f"{name} is invalid")
    return SessionRef(
        validate_agent_id(value.agent_id), validate_session_id(value.session_id)
    )


def _evidence(values: object) -> tuple[SessionRef, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValidationError("summary evidence is invalid")
    result: list[SessionRef] = []
    for value in values:
        item = _validated_ref(value, "summary evidence")
        if item not in result:
            result.append(item)
    return tuple(result)


def prepare_bind(
    request: BindRequest,
    *,
    env: Mapping[str, str],
    store: WorkItemStore,
    now: str,
    run: Runner = run_command,
    token_root: Path | None = None,
) -> PrepareResult:
    requested_id = validate_work_item_id(request.work_item_id)
    session_title = validate_session_title(request.session_title)
    prepared_at = _parse_time(now)
    current = detect_current_session(env)
    ref = current.ref
    project = resolve_project(request.cwd, run)

    target: tuple[SearchMatch, Dossier] | None = None
    existing_record: SessionRecord | None = None
    for match, dossier in _archive(store, binding_ref=ref):
        if dossier.work_item_id.casefold() == requested_id.casefold():
            if target is not None:
                raise ValueError("duplicate case-folded work item ID")
            target = (match, dossier)
        if ref in dossier.records:
            if dossier.work_item_id.casefold() != requested_id.casefold():
                raise BindingConflict(
                    f"Session is already bound to {dossier.work_item_id}"
                )
            existing_record = dossier.records[ref]

    work_item_id = target[1].work_item_id if target else requested_id
    target_title = f"{work_item_id} {session_title}"
    prepared = PreparedBind(
        schema_version=TOKEN_SCHEMA_VERSION,
        prepared_at=prepared_at.isoformat(),
        expires_at=(prepared_at + TOKEN_LIFETIME).isoformat(),
        agent_id=current.agent_id,
        session_id=current.session_id,
        work_item_id=work_item_id,
        session_title=session_title,
        target_title=target_title,
        project=project,
        vault_path=str(store.vault_path.resolve()),
        dossier_digest=_digest(target[0].text) if target else None,
    )
    token = _write_token("prepared", asdict(prepared), prepared.ref, token_root)
    archive_records = (
        tuple(
            sorted(
                target[1].records.values(),
                key=lambda record: (
                    _parse_time(record.occurred_at),
                    record.agent_id,
                    record.session_id,
                ),
                reverse=True,
            )
        )
        if target
        else ()
    )
    return PrepareResult(
        prepared_token=token,
        agent_id=current.agent_id,
        session_id=current.session_id,
        target_title=target_title,
        project=project,
        vault_path=prepared.vault_path,
        existing_record=existing_record,
        archive_summary=_summary_fields(target[1]) if target else None,
        archive_sessions=archive_records,
    )


def stage_bind(
    prepared_token: str | Path,
    fields: DerivedFields,
    store: WorkItemStore,
    *,
    env: Mapping[str, str] = os.environ,
    now: str,
    token_root: Path | None = None,
) -> StageResult:
    staged_at = _parse_time(now)
    prepared = _prepared(
        _read_token(prepared_token, "prepared", token_root), staged_at
    )
    current = _check_runtime_session(expected=prepared.ref, env=env)
    if str(store.vault_path.resolve()) != prepared.vault_path:
        raise TokenError("selected vault changed")
    target = _validate_snapshot(
        ref=prepared.ref,
        work_item_id=prepared.work_item_id,
        expected_digest=prepared.dossier_digest,
        store=store,
    )

    topics = _topics(fields.topics)
    project_role = _single_line(fields.project_role, "project role", unknown=True)
    result = _single_line(fields.result, "result", unknown=True)
    next_step = _single_line(fields.next_step, "next step", unknown=True)
    allowed_status = {"进行中", "已完成", "暂停", "未知"}
    if fields.status not in allowed_status:
        raise ValidationError("status is invalid")

    current_progress = _single_line(
        fields.summary.current_progress, "summary progress", unknown=True
    )
    unresolved = _single_line(
        fields.summary.unresolved, "summary unresolved", unknown=True
    )
    evidence = _evidence(fields.summary.evidence_sessions)
    known_refs = set(target[1].records) if target else set()
    known_refs.add(prepared.ref)
    if not set(evidence).issubset(known_refs):
        raise ValidationError("summary evidence session does not exist")
    recommended = fields.summary.recommended_session
    if recommended is not None:
        recommended = _validated_ref(recommended, "recommended session")
        if recommended not in known_refs:
            raise ValidationError("recommended session does not exist")

    record = SessionRecord(
        agent_id=prepared.agent_id,
        session_id=prepared.session_id,
        title=prepared.target_title,
        occurred_at=staged_at.isoformat(),
        project_name=prepared.project.project_name,
        project_root=prepared.project.project_root,
        repository=prepared.project.repository,
        topics=topics,
        result=result,
        next_step=next_step,
        status=cast(Literal["进行中", "已完成", "暂停", "未知"], fields.status),
    )
    project_roles = dict(target[1].project_roles) if target else {}
    for project_name in tuple(project_roles):
        if project_name.casefold() == prepared.project.project_name.casefold():
            del project_roles[project_name]
    project_roles[prepared.project.project_name] = project_role
    summary = SummaryFields(
        current_progress=current_progress,
        unresolved=unresolved,
        recommended_session=recommended,
        evidence_sessions=evidence,
        project_roles=project_roles,
    )
    staged = StagedBind(
        schema_version=TOKEN_SCHEMA_VERSION,
        staged_at=staged_at.isoformat(),
        expires_at=(staged_at + TOKEN_LIFETIME).isoformat(),
        agent_id=prepared.agent_id,
        session_id=prepared.session_id,
        work_item_id=prepared.work_item_id,
        session_title=prepared.session_title,
        target_title=prepared.target_title,
        project=prepared.project,
        vault_path=prepared.vault_path,
        dossier_digest=prepared.dossier_digest,
        record=record,
        summary=summary,
    )
    staged_token = _write_token("staged", asdict(staged), staged.ref, token_root)
    try:
        _remove_token(Path(prepared_token))
    except OSError:
        _remove_token(Path(staged_token), missing_ok=True)
        raise
    manual = (
        f"/rename {prepared.target_title}" if current.rename_mode == "manual" else None
    )
    return StageResult(
        staged_token=staged_token,
        target_title=prepared.target_title,
        agent_id=current.agent_id,
        rename_mode=current.rename_mode,
        manual_rename_command=manual,
    )


def rename_staged_thread(
    staged_token: str | Path,
    *,
    env: Mapping[str, str] = os.environ,
    now: str,
    rename: Callable[[str, str], None],
    token_root: Path | None = None,
) -> RenameResult:
    current = _parse_time(now)
    staged = _staged(_read_token(staged_token, "staged", token_root), current)
    if agent_session(staged.ref).rename_mode != "automatic":
        raise RenameRequired("manual task rename must be confirmed outside this command")
    _check_runtime_session(expected=staged.ref, env=env)
    rename(staged.session_id, staged.target_title)
    return RenameResult(target_title=staged.target_title)


def commit_bind(
    staged_token: str | Path,
    store: WorkItemStore,
    *,
    env: Mapping[str, str] = os.environ,
    now: str,
    token_root: Path | None = None,
    rename_confirmed: bool,
    lock_root: Path | None = None,
) -> CommitResult:
    if not rename_confirmed:
        raise RenameRequired("task rename must be confirmed before commit")
    try:
        current = _parse_time(now)
        staged = _staged(
            _read_token(staged_token, "staged", token_root), current
        )
        _check_runtime_session(expected=staged.ref, env=env)
        if str(store.vault_path.resolve()) != staged.vault_path:
            raise TokenError("selected vault changed")
        with archive_lock(
            store.vault_path,
            lock_root=lock_root,
        ):
            target = _validate_snapshot(
                ref=staged.ref,
                work_item_id=staged.work_item_id,
                expected_digest=staged.dossier_digest,
                store=store,
            )
            known_refs = set(target[1].records) if target else set()
            known_refs.add(staged.ref)
            known_projects = (
                {
                    record.project_name.casefold()
                    for record in target[1].records.values()
                }
                if target
                else set()
            )
            known_projects.add(staged.project.project_name.casefold())
            role_projects = {
                project_name.casefold()
                for project_name in staged.summary.project_roles
            }
            if (
                not set(staged.summary.evidence_sessions).issubset(known_refs)
                or role_projects != known_projects
                or (
                    staged.summary.recommended_session is not None
                    and staged.summary.recommended_session not in known_refs
                )
            ):
                raise TokenError("bind token is invalid")

            relative_path = RECORDS_FOLDER / f"{staged.work_item_id}.md"
            if target is not None and target[0].relative_path != relative_path:
                raise DossierFormatError("work item dossier path is invalid")
            dossier = (
                target[1]
                if target is not None
                else Dossier.parse(
                    new_dossier(staged.work_item_id, staged.staged_at)
                )
            )
            dossier.upsert(staged.record)
            rendered = dossier.render(staged.work_item_id, now, staged.summary)
            Dossier.parse(rendered)
            store.write(relative_path, rendered)
            saved = Dossier.parse(store.read(relative_path))
            saved_record = saved.records.get(staged.ref)
            if (
                saved_record is None
                or saved_record != staged.record
                or saved_record.title != staged.target_title
            ):
                raise PersistenceError("write verification failed")
    except Exception as exc:
        raise PartialBindFailure(exc) from exc

    warnings: tuple[str, ...] = ()
    try:
        _remove_token(Path(staged_token))
    except OSError:
        warnings = ("dossier committed; staged token cleanup failed",)
    return CommitResult(
        dossier_path=str(store.vault_path.resolve() / relative_path),
        resume_command=render_resume_command(staged.ref),
        warnings=warnings,
    )
