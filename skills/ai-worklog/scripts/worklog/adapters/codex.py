from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from ..model import ValidationError, validate_session_id


DEFAULT_APP_SERVER_COMMAND = ("codex", "app-server", "--stdio")


def matches(env: Mapping[str, str]) -> bool:
    return "CODEX_SESSION_ID" in env or "CODEX_THREAD_ID" in env


def session_id_from_env(env: Mapping[str, str]) -> str:
    has_session_id = "CODEX_SESSION_ID" in env
    has_thread_id = "CODEX_THREAD_ID" in env
    if has_session_id and has_thread_id:
        session_id = env["CODEX_SESSION_ID"]
        if session_id != env["CODEX_THREAD_ID"]:
            raise ValidationError("Codex Session ID sources disagree")
        return validate_session_id(session_id)
    if has_session_id:
        return validate_session_id(env["CODEX_SESSION_ID"])
    return validate_session_id(env["CODEX_THREAD_ID"])


def resume_argv(session_id: str) -> tuple[str, ...]:
    return ("codex", "resume", session_id)


class RenameError(RuntimeError):
    """Raised when Codex cannot rename and verify the current task."""


def _read_lines(stream: TextIO, output: queue.Queue[object]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def _send(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _response(
    output: queue.Queue[object], request_id: int, deadline: float
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RenameError("task rename failed")
        try:
            raw = output.get(timeout=remaining)
        except queue.Empty as exc:
            raise RenameError("task rename failed") from exc
        if raw is None:
            raise RenameError("task rename failed")
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RenameError("task rename failed") from exc
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message or "result" not in message:
            raise RenameError("task rename failed")
        return message


def rename_thread_via_app_server(
    session_id: str,
    target_title: str,
    *,
    command: Sequence[str] = DEFAULT_APP_SERVER_COMMAND,
    timeout: float = 10.0,
) -> None:
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    try:
        process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            raise RenameError("task rename failed")
        output: queue.Queue[object] = queue.Queue()
        reader = threading.Thread(
            target=_read_lines, args=(process.stdout, output), daemon=True
        )
        reader.start()
        deadline = time.monotonic() + timeout

        _send(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "ai-worklog",
                        "title": "AI Worklog",
                        "version": "1.0.0",
                    }
                },
            },
        )
        _response(output, 1, deadline)
        _send(process.stdin, {"method": "initialized"})
        _send(
            process.stdin,
            {
                "id": 2,
                "method": "thread/name/set",
                "params": {"threadId": session_id, "name": target_title},
            },
        )
        _response(output, 2, deadline)
        _send(
            process.stdin,
            {
                "id": 3,
                "method": "thread/read",
                "params": {"threadId": session_id, "includeTurns": False},
            },
        )
        response = _response(output, 3, deadline)
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        if (
            not isinstance(thread, dict)
            or thread.get("id") != session_id
            or thread.get("name") != target_title
        ):
            raise RenameError("task rename verification failed")
    except RenameError:
        raise
    except (OSError, ValueError, TypeError, BrokenPipeError) as exc:
        raise RenameError("task rename failed") from exc
    finally:
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            else:
                process.wait()
            if reader is not None:
                reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
