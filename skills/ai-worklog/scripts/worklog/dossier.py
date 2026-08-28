from __future__ import annotations

import base64
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .agent import render_resume_command
from .model import (
    SessionRecord,
    SessionRef,
    ValidationError,
    format_session_ref,
    record_key,
    validate_agent_id,
    validate_session_id,
    validate_work_item_id,
)
from .project import sanitize_remote


MARKER_RE = re.compile(
    r"^<!-- ai-worklog:(summary|projects|session:([0-9a-f]{64})):(start|end) -->$",
    re.MULTILINE,
)
MANAGED_TOKEN_RE = re.compile(r"<!-- ai-worklog:")
FIELD_RE = re.compile(r"^- \*\*(.+?)\*\*：(.*)$", re.MULTILINE)
MANUAL_NOTES_HEADING_RE = re.compile(r"^## 人工备注[ \t]*$", re.MULTILINE)
FRONTMATTER_KEYS = {
    "type",
    "schema_version",
    "work_item_id",
    "created_at",
    "updated_at",
}
SESSION_FIELDS = {
    "Agent",
    "会话标题",
    "快速恢复",
    "项目",
    "项目根目录",
    "仓库",
    "话题",
    "讨论结果",
    "下一步",
    "状态",
}
ALLOWED_STATUSES = {"进行中", "已完成", "暂停", "未知"}
TOPICS_EMPTY = "无"
TOPICS_DELIMITER = "、"


class DossierFormatError(ValueError):
    """Raised when a work-item dossier cannot be safely maintained."""


class PersistenceError(RuntimeError):
    """Raised when an atomic dossier write cannot be verified."""


@dataclass(frozen=True)
class ManagedRegion:
    kind: str
    key: str | None
    start: int
    end: int


@dataclass(frozen=True)
class SummaryFields:
    current_progress: str
    unresolved: str
    recommended_session: SessionRef | None
    evidence_sessions: tuple[SessionRef, ...]
    project_roles: Mapping[str, str]


@dataclass(frozen=True)
class ProjectRow:
    project_name: str
    repository: str | None
    role: str


@dataclass(frozen=True)
class Binding:
    work_item_id: str
    path: Path
    record: SessionRecord


def marker_parts(match: re.Match[str]) -> tuple[str, str | None, str]:
    group, session_id, edge = match.groups()
    if group.startswith("session:"):
        return "session", session_id, edge
    return group, None, edge


def _scan_regions(text: str) -> list[ManagedRegion]:
    valid_starts = {match.start() for match in MARKER_RE.finditer(text)}
    if any(match.start() not in valid_starts for match in MANAGED_TOKEN_RE.finditer(text)):
        raise DossierFormatError("malformed managed marker")
    stack: ManagedRegion | None = None
    regions: list[ManagedRegion] = []
    for match in MARKER_RE.finditer(text):
        kind, key, edge = marker_parts(match)
        if edge == "start":
            if stack is not None:
                raise DossierFormatError("overlapping managed marker")
            stack = ManagedRegion(kind, key, match.start(), -1)
        elif stack is None or (stack.kind, stack.key) != (kind, key):
            raise DossierFormatError("unmatched managed marker")
        else:
            regions.append(replace(stack, end=match.end()))
            stack = None
    if stack is not None:
        raise DossierFormatError("unclosed managed marker")
    return regions


def _safe_text(value: str) -> str:
    """Keep unparsed projection text on one inert Markdown line."""
    cleaned = " ".join(value.splitlines())
    cleaned = "".join(" " if ord(character) < 32 or ord(character) == 127 else character for character in cleaned)
    return cleaned.replace("<", r"\u003c").replace("|", r"\|").replace("`", r"\`")


def _encode_text(value: str, *, force: bool = False) -> str:
    """Encode values that could alter Markdown or collide with the encoding tag."""
    unsafe = force or value.startswith("aiw:") or any(
        character in "<|`" or ord(character) < 32 or ord(character) == 127
        for character in value
    )
    if not unsafe:
        return value
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return f"aiw:{encoded}"


def _decode_text(value: str) -> str:
    if not value.startswith("aiw:"):
        return value
    try:
        return base64.b64decode(value[4:].encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return value


def _code(value: str, *, force: bool = False) -> str:
    return f"`{_encode_text(value, force=force)}`"


def _parse_timestamp(value: str, message: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DossierFormatError(message) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DossierFormatError(message)
    return value


def _parse_ref(value: str) -> SessionRef:
    agent_id, separator, session_id = value.partition("/")
    if not separator:
        raise DossierFormatError("invalid session reference")
    try:
        return SessionRef(validate_agent_id(agent_id), validate_session_id(session_id))
    except ValidationError as exc:
        raise DossierFormatError("invalid session reference") from exc


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise DossierFormatError("missing dossier frontmatter")
    ending = text.find("\n---\n", 4)
    if ending < 0:
        raise DossierFormatError("missing dossier frontmatter")
    values: dict[str, str] = {}
    for line in text[4:ending].splitlines():
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*): (.+)", line)
        if match is None or match.group(1) in values:
            raise DossierFormatError("invalid dossier frontmatter")
        values[match.group(1)] = match.group(2)
    if (
        set(values) != FRONTMATTER_KEYS
        or values["type"] != "ai-work-item-history"
        or values["schema_version"] != "1"
    ):
        raise DossierFormatError("invalid dossier frontmatter")
    try:
        validate_work_item_id(values["work_item_id"])
        _parse_timestamp(values["created_at"], "invalid dossier frontmatter timestamp")
        _parse_timestamp(values["updated_at"], "invalid dossier frontmatter timestamp")
    except ValidationError as exc:
        raise DossierFormatError("invalid frontmatter work item ID") from exc
    return values


def _parse_record(region: ManagedRegion, text: str) -> SessionRecord:
    if region.key is None:
        raise DossierFormatError("invalid session marker")
    content = text[region.start : region.end]
    headings = re.findall(
        r"^### ([^\n]+?) · (.+)$", content, re.MULTILINE
    )
    if len(headings) != 1:
        raise DossierFormatError("invalid session content")
    occurred_at, heading_session_id = (
        _decode_text(headings[0][0]),
        _decode_text(headings[0][1]),
    )
    _parse_timestamp(occurred_at, "invalid session timestamp")
    field_pairs = FIELD_RE.findall(content)
    fields = dict(field_pairs)
    if len(field_pairs) != len(fields) or set(fields) != SESSION_FIELDS:
        raise DossierFormatError("invalid session content")

    def uncode(value: str) -> str:
        value = value.strip()
        if value.startswith("`") and value.endswith("`"):
            return value[1:-1]
        return value

    try:
        ref = SessionRef(
            validate_agent_id(fields["Agent"].strip()),
            validate_session_id(heading_session_id),
        )
    except ValidationError as exc:
        raise DossierFormatError("invalid session reference") from exc
    if record_key(ref) != region.key:
        raise DossierFormatError("invalid session marker")

    topic_projection = fields["话题"].strip()
    topics = (
        ()
        if topic_projection == TOPICS_EMPTY
        else tuple(
            _decode_text(topic)
            for topic in topic_projection.split(TOPICS_DELIMITER)
        )
    )
    repository = _decode_text(fields["仓库"].strip())
    title = _decode_text(uncode(fields["会话标题"]))
    project_name = _decode_text(fields["项目"].strip())
    project_root = _decode_text(uncode(fields["项目根目录"]))
    result = _decode_text(fields["讨论结果"].strip())
    next_step = _decode_text(fields["下一步"].strip())
    status = _decode_text(fields["状态"].strip())
    resume = _decode_text(uncode(fields["快速恢复"]))
    stored_repository = None if repository == "未知" else repository
    if (
        not title
        or not project_name
        or not Path(project_root).is_absolute()
        or not result
        or not next_step
        or status not in ALLOWED_STATUSES
        or any(not topic for topic in topics)
        or resume != render_resume_command(ref)
        or (
            stored_repository is not None
            and sanitize_remote(stored_repository) != stored_repository
        )
    ):
        raise DossierFormatError("invalid session content")
    return SessionRecord(
        agent_id=ref.agent_id,
        session_id=ref.session_id,
        title=title,
        occurred_at=occurred_at,
        project_name=project_name,
        project_root=project_root,
        repository=stored_repository,
        topics=topics,
        result=result,
        next_step=next_step,
        status=status,  # type: ignore[arg-type]
    )


def _parse_summary(
    region: ManagedRegion,
    text: str,
    records: Mapping[SessionRef, SessionRecord],
    project_roles: Mapping[str, str],
) -> SummaryFields:
    content = text[region.start : region.end]
    labels = {
        "current_progress": "当前进展",
        "unresolved": "未决事项",
        "recommended_session": "建议恢复",
        "evidence_sessions": "摘要依据",
    }
    values: dict[str, str] = {}
    for name, label in labels.items():
        matches = re.findall(rf"^> - \*\*{label}\*\*：(.*)$", content, re.MULTILINE)
        if len(matches) != 1:
            raise DossierFormatError("invalid summary projection")
        values[name] = matches[0].strip()

    def uncode(value: str) -> str:
        if value.startswith("`") and value.endswith("`"):
            return value[1:-1]
        return value

    recommended_text = values["recommended_session"]
    recommended = (
        None
        if recommended_text == "无"
        else _parse_ref(_decode_text(uncode(recommended_text)))
    )
    evidence_text = values["evidence_sessions"]
    evidence = (
        ()
        if evidence_text == "无"
        else tuple(
            _parse_ref(_decode_text(uncode(item.strip())))
            for item in evidence_text.split("、")
        )
    )
    if recommended is not None and recommended not in records:
        raise DossierFormatError("summary reference does not exist")
    if not set(evidence).issubset(records):
        raise DossierFormatError("summary reference does not exist")
    return SummaryFields(
        current_progress=_decode_text(values["current_progress"]),
        unresolved=_decode_text(values["unresolved"]),
        recommended_session=recommended,
        evidence_sessions=evidence,
        project_roles=dict(project_roles),
    )


def _table_cells(line: str) -> tuple[str, ...]:
    if not line.startswith("|") or not line.endswith("|"):
        raise DossierFormatError("invalid project projection")
    cells: list[str] = []
    current: list[str] = []
    index = 1
    while index < len(line) - 1:
        character = line[index]
        if character == "\\" and index + 1 < len(line) - 1 and line[index + 1] == "|":
            current.extend(("\\", "|"))
            index += 2
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
        else:
            current.append(character)
            index += 1
    cells.append("".join(current).strip())
    return tuple(cells)


def _unescape_table_cell(value: str) -> str:
    return value.replace(r"\|", "|").replace(r"\`", "`")


def _parse_project_roles(
    region: ManagedRegion, text: str, records: Mapping[SessionRef, SessionRecord]
) -> dict[str, str]:
    lines = text[region.start : region.end].splitlines()
    if len(lines) < 4 or lines[1:3] != [
        "| 项目 | 仓库 | 在工作项中的作用 |",
        "|---|---|---|",
    ]:
        raise DossierFormatError("invalid project projection")
    projects = {
        record.project_name.casefold(): record.project_name for record in records.values()
    }
    roles: dict[str, str] = {}
    for line in lines[3:-1]:
        cells = _table_cells(line)
        if len(cells) != 3:
            raise DossierFormatError("invalid project projection")
        matches = [
            project_name
            for project_name in projects.values()
            if _safe_text(project_name) == cells[0]
        ]
        if len(matches) != 1:
            raise DossierFormatError("invalid project projection")
        project_name = matches[0]
        folded = project_name.casefold()
        if folded in {name.casefold() for name in roles}:
            raise DossierFormatError("duplicate project projection")
        roles[project_name] = _decode_text(_unescape_table_cell(cells[2]))
    return roles


def project_rows(records: Iterable[SessionRecord], roles: Mapping[str, str]) -> list[ProjectRow]:
    latest: dict[str, SessionRecord] = {}
    for record in sorted(records, key=lambda item: item.occurred_at):
        latest[record.project_name.casefold()] = record
    return [
        ProjectRow(record.project_name, record.repository, roles.get(key, "未知"))
        for key, record in sorted(latest.items())
    ]


def summary_counts(records: Sequence[SessionRecord]) -> tuple[int, int]:
    return len({record.project_name.casefold() for record in records}), len(records)


def _render_session(record: SessionRecord) -> str:
    ref = record.ref
    key = record_key(ref)
    topics = TOPICS_DELIMITER.join(
        _encode_text(
            topic,
            force=topic == TOPICS_EMPTY or TOPICS_DELIMITER in topic,
        )
        for topic in record.topics
    ) or TOPICS_EMPTY
    repository = _encode_text(record.repository) if record.repository else "未知"
    return "\n".join(
        (
            f"<!-- ai-worklog:session:{key}:start -->",
            f"### {_encode_text(record.occurred_at)} · {_encode_text(record.session_id)}",
            "",
            f"- **Agent**：{record.agent_id}",
            f"- **会话标题**：{_code(record.title)}",
            f"- **快速恢复**：{_code(render_resume_command(ref))}",
            f"- **项目**：{_encode_text(record.project_name)}",
            f"- **项目根目录**：{_code(record.project_root)}",
            f"- **仓库**：{repository}",
            f"- **话题**：{topics}",
            f"- **讨论结果**：{_encode_text(record.result)}",
            f"- **下一步**：{_encode_text(record.next_step)}",
            f"- **状态**：{_encode_text(record.status)}",
            f"<!-- ai-worklog:session:{key}:end -->",
        )
    )


def _render_summary(records: Sequence[SessionRecord], summary: SummaryFields) -> str:
    known_refs = {record.ref for record in records}
    if summary.recommended_session is not None and summary.recommended_session not in known_refs:
        raise DossierFormatError("summary reference does not exist")
    if not set(summary.evidence_sessions).issubset(known_refs):
        raise DossierFormatError("summary reference does not exist")
    project_count, session_count = summary_counts(records)
    if summary.recommended_session:
        recommended_ref = format_session_ref(summary.recommended_session)
        recommended = _code(recommended_ref, force="、" in recommended_ref)
    else:
        recommended = "无"
    evidence = "、".join(
        _code(ref_text, force="、" in ref_text)
        for ref_text in (format_session_ref(ref) for ref in summary.evidence_sessions)
    ) or "无"
    return "\n".join(
        (
            "<!-- ai-worklog:summary:start -->",
            "> [!summary] 回溯摘要",
            f"> - **规模**：涉及 {project_count} 个项目，共 {session_count} 条会话",
            f"> - **当前进展**：{_encode_text(summary.current_progress)}",
            f"> - **未决事项**：{_encode_text(summary.unresolved)}",
            f"> - **建议恢复**：{recommended}",
            f"> - **摘要依据**：{evidence}",
            "<!-- ai-worklog:summary:end -->",
        )
    )


def _render_projects(records: Sequence[SessionRecord], roles: Mapping[str, str]) -> str:
    lines = [
        "<!-- ai-worklog:projects:start -->",
        "| 项目 | 仓库 | 在工作项中的作用 |",
        "|---|---|---|",
    ]
    for row in project_rows(records, {key.casefold(): value for key, value in roles.items()}):
        repository = _safe_text(row.repository) if row.repository else "未知"
        role = _safe_text(_encode_text(row.role))
        lines.append(f"| {_safe_text(row.project_name)} | {repository} | {role} |")
    lines.append("<!-- ai-worklog:projects:end -->")
    return "\n".join(lines)


@dataclass
class Dossier:
    text: str
    work_item_id: str
    regions: list[ManagedRegion]
    records: dict[SessionRef, SessionRecord]
    project_roles: dict[str, str]
    summary: SummaryFields

    @classmethod
    def parse(cls, text: str) -> "Dossier":
        regions = _scan_regions(text)
        work_item_id = _parse_frontmatter(text)["work_item_id"]
        kinds = {region.kind for region in regions}
        if not {"summary", "projects"}.issubset(kinds):
            raise DossierFormatError("missing managed marker")
        if sum(region.kind == "summary" for region in regions) != 1 or sum(region.kind == "projects" for region in regions) != 1:
            raise DossierFormatError("duplicate managed marker")
        records: dict[SessionRef, SessionRecord] = {}
        record_keys: set[str] = set()
        for region in regions:
            if region.kind == "session":
                record = _parse_record(region, text)
                key = record_key(record.ref)
                if record.ref in records or key in record_keys:
                    raise DossierFormatError("duplicate session marker")
                records[record.ref] = record
                record_keys.add(key)
        projects_region = next(region for region in regions if region.kind == "projects")
        roles = _parse_project_roles(projects_region, text, records)
        summary_region = next(region for region in regions if region.kind == "summary")
        summary = _parse_summary(summary_region, text, records, roles)
        return cls(text, work_item_id, regions, records, roles, summary)

    def upsert(self, record: SessionRecord) -> None:
        self.records[record.ref] = record

    def render(self, work_item_id: str, now: str, summary: SummaryFields) -> str:
        if self.work_item_id.casefold() != work_item_id.casefold():
            raise DossierFormatError("work item ID does not match dossier")
        records = tuple(
            sorted(
                self.records.values(),
                key=lambda item: (item.occurred_at, item.agent_id, item.session_id),
            )
        )
        replacements: list[tuple[ManagedRegion, str]] = []
        for region in self.regions:
            if region.kind == "summary":
                replacements.append((region, _render_summary(records, summary)))
            elif region.kind == "projects":
                replacements.append((region, _render_projects(records, summary.project_roles)))
            elif region.kind == "session":
                record = next(
                    (
                        candidate
                        for candidate in self.records.values()
                        if record_key(candidate.ref) == region.key
                    ),
                    None,
                )
                if record is not None:
                    replacements.append((region, _render_session(record)))
        rendered = self.text
        for region, replacement in sorted(replacements, key=lambda value: value[0].start, reverse=True):
            rendered = rendered[: region.start] + replacement + rendered[region.end :]

        existing = {region.key for region in self.regions if region.kind == "session"}
        additions = [
            _render_session(record)
            for record in records
            if record_key(record.ref) not in existing
        ]
        if additions:
            managed_regions = _scan_regions(rendered)
            anchor = next(
                (
                    match.start()
                    for match in MANUAL_NOTES_HEADING_RE.finditer(rendered)
                    if all(
                        not (region.start <= match.start() < region.end)
                        for region in managed_regions
                    )
                ),
                -1,
            )
            insertion = "\n\n".join(additions) + "\n\n"
            if anchor >= 0:
                rendered = rendered[:anchor] + insertion + rendered[anchor:]
            else:
                rendered = rendered.rstrip("\n") + "\n\n" + insertion.rstrip("\n") + "\n"
        rendered = re.sub(
            r"(?m)^updated_at: .+$", f"updated_at: {now}", rendered, count=1
        )
        Dossier.parse(rendered)
        return rendered


def new_dossier(work_item_id: str, now: str) -> str:
    return f"""---
type: ai-work-item-history
schema_version: 1
work_item_id: {work_item_id}
created_at: {now}
updated_at: {now}
---

# {work_item_id}

<!-- ai-worklog:summary:start -->
> [!summary] 回溯摘要
> - **规模**：涉及 0 个项目，共 0 条会话
> - **当前进展**：未知
> - **未决事项**：未知
> - **建议恢复**：无
> - **摘要依据**：无
<!-- ai-worklog:summary:end -->

## 涉及项目

<!-- ai-worklog:projects:start -->
| 项目 | 仓库 | 在工作项中的作用 |
|---|---|---|
<!-- ai-worklog:projects:end -->

## 会话记录

## 人工备注

此区域不会被 Skill 覆盖。
"""


def _dossiers(root: Path) -> tuple[tuple[Path, Dossier], ...]:
    if not root.is_dir():
        return ()
    dossiers = tuple(
        (path, Dossier.parse(path.read_text(encoding="utf-8")))
        for path in root.iterdir()
        if path.is_file() and path.suffix == ".md"
    )
    seen: set[str] = set()
    for _, dossier in dossiers:
        folded_id = dossier.work_item_id.casefold()
        if folded_id in seen:
            raise DossierFormatError("duplicate case-folded work item ID")
        seen.add(folded_id)
    return dossiers


def resolve_work_item(root: Path, work_item_id: str) -> Path | None:
    matches = [(path, dossier) for path, dossier in _dossiers(root) if dossier.work_item_id.casefold() == work_item_id.casefold()]
    if len(matches) > 1:
        raise DossierFormatError("duplicate case-folded work item ID")
    return matches[0][0] if matches else None


def find_binding(root: Path, ref: SessionRef) -> Binding | None:
    matches = [
        Binding(dossier.work_item_id, path, dossier.records[ref])
        for path, dossier in _dossiers(root)
        if ref in dossier.records
    ]
    if len(matches) > 1:
        raise DossierFormatError("session ID appears in multiple dossiers")
    return matches[0] if matches else None


def atomic_write_verified(path: Path, text: str, ref: SessionRef, title: str) -> None:
    temporary_name: str | None = None
    try:
        candidate = Dossier.parse(text)
        candidate_record = candidate.records.get(ref)
        if candidate_record is None or candidate_record.title != title:
            raise PersistenceError("write verification failed")
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        saved = Dossier.parse(path.read_text(encoding="utf-8"))
        record = saved.records.get(ref)
        if record is None or record.title != title:
            raise PersistenceError("write verification failed")
    except (OSError, DossierFormatError) as exc:
        raise PersistenceError("write verification failed") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
