#!/usr/bin/env python3
"""Expose the Claude Code session identity to the Claude environment."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Mapping, TextIO


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "ai-worklog" / "scripts"))

from worklog.model import validate_session_id


def main(stdin: TextIO = sys.stdin, env: Mapping[str, str] = os.environ) -> int:
    try:
        payload = json.load(stdin)
        if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str):
            raise ValueError("invalid hook input")
        session_id = validate_session_id(payload["session_id"])
        destination = env.get("CLAUDE_ENV_FILE", "")
        if not destination:
            raise ValueError("CLAUDE_ENV_FILE is unavailable")
        with Path(destination).open("a", encoding="utf-8") as output:
            output.write("export AI_WORKLOG_AGENT=claude-code\n")
            output.write(f"export AI_WORKLOG_SESSION_ID={shlex.quote(session_id)}\n")
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print("ai-worklog SessionStart hook failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
