from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .adapters import claude_code, codex
from .model import (
    AgentId,
    RenameMode,
    SessionRef,
    ValidationError,
    validate_agent_id,
    validate_session_id,
)


class UnsupportedAgent(ValidationError):
    error_code = "unsupported_agent"


class AmbiguousAgent(ValidationError):
    error_code = "ambiguous_agent"


@dataclass(frozen=True)
class AgentSession:
    agent_id: AgentId
    session_id: str
    resume_argv: tuple[str, ...]
    rename_mode: RenameMode

    @property
    def ref(self) -> SessionRef:
        return SessionRef(self.agent_id, self.session_id)


@dataclass(frozen=True)
class AdapterSpec:
    agent_id: AgentId
    rename_mode: RenameMode
    matches: Callable[[Mapping[str, str]], bool]
    session_id_from_env: Callable[[Mapping[str, str]], str]
    resume_argv: Callable[[str], tuple[str, ...]]


REGISTERED_ADAPTERS = (
    AdapterSpec(
        "codex", "automatic", codex.matches, codex.session_id_from_env, codex.resume_argv
    ),
    AdapterSpec(
        "claude-code",
        "manual",
        claude_code.matches,
        claude_code.session_id_from_env,
        claude_code.resume_argv,
    ),
)


def detect_current_session(env: Mapping[str, str]) -> AgentSession:
    matches = tuple(adapter for adapter in REGISTERED_ADAPTERS if adapter.matches(env))
    if not matches:
        raise UnsupportedAgent("no supported Agent session is available")
    if len(matches) != 1:
        raise AmbiguousAgent("multiple supported Agent sessions are available")
    adapter = matches[0]
    session_id = adapter.session_id_from_env(env)
    return AgentSession(
        adapter.agent_id,
        session_id,
        adapter.resume_argv(session_id),
        adapter.rename_mode,
    )


def agent_session(ref: SessionRef) -> AgentSession:
    agent_id = validate_agent_id(ref.agent_id)
    session_id = validate_session_id(ref.session_id)
    adapter = next(item for item in REGISTERED_ADAPTERS if item.agent_id == agent_id)
    return AgentSession(
        agent_id, session_id, adapter.resume_argv(session_id), adapter.rename_mode
    )


def render_resume_command(ref: SessionRef) -> str:
    return shlex.join(agent_session(ref).resume_argv)
