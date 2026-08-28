from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, cast


WORK_ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AgentId = Literal["codex", "claude-code"]
RenameMode = Literal["automatic", "manual"]
REGISTERED_AGENT_IDS = frozenset(("codex", "claude-code"))


class ValidationError(ValueError):
    """Raised when a worklog value violates its storage contract."""


class InvalidSessionId(ValidationError):
    error_code = "invalid_session_id"


@dataclass(frozen=True)
class SessionRef:
    agent_id: AgentId
    session_id: str


def validate_agent_id(value: object) -> AgentId:
    if not isinstance(value, str) or value not in REGISTERED_AGENT_IDS:
        raise ValidationError("Agent ID is invalid")
    return cast(AgentId, value)


def validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise InvalidSessionId("Session ID is invalid")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise InvalidSessionId("Session ID is invalid")
    return value


def record_key(ref: SessionRef) -> str:
    agent_id = validate_agent_id(ref.agent_id)
    session_id = validate_session_id(ref.session_id)
    payload = agent_id.encode("utf-8") + b"\0" + session_id.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def format_session_ref(ref: SessionRef) -> str:
    agent_id = validate_agent_id(ref.agent_id)
    session_id = validate_session_id(ref.session_id)
    return f"{agent_id}/{session_id}"


def validate_work_item_id(value: str) -> str:
    if not WORK_ITEM_RE.fullmatch(value):
        raise ValidationError("work item ID is invalid")
    return value


def validate_session_title(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValidationError("session title is invalid")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValidationError("session title is invalid")
    title = value.strip()
    if not title or len(title) > 100:
        raise ValidationError("session title is invalid")
    return title


@dataclass(frozen=True)
class SessionRecord:
    agent_id: AgentId
    session_id: str
    title: str
    occurred_at: str
    project_name: str
    project_root: str
    repository: str | None
    topics: tuple[str, ...]
    result: str
    next_step: str
    status: Literal["进行中", "已完成", "暂停", "未知"]

    @property
    def ref(self) -> SessionRef:
        return SessionRef(self.agent_id, self.session_id)
