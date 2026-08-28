from __future__ import annotations

from collections.abc import Mapping

from ..model import validate_session_id


def matches(env: Mapping[str, str]) -> bool:
    return (
        env.get("AI_WORKLOG_AGENT") == "claude-code"
        and "AI_WORKLOG_SESSION_ID" in env
    )


def session_id_from_env(env: Mapping[str, str]) -> str:
    return validate_session_id(env["AI_WORKLOG_SESSION_ID"])


def resume_argv(session_id: str) -> tuple[str, ...]:
    return ("claude", "--resume", session_id)
