"""Small, dependency-free readers for Claude Code transcript files."""
from __future__ import annotations

import json
import pathlib
import re
from typing import Optional


CLAUDE_TAIL_CHUNK = 1 << 20
CLAUDE_TAIL_CHUNKS = 16


def claude_home(home=None) -> pathlib.Path:
    return pathlib.Path(home) if home else pathlib.Path.home() / ".claude"


def claude_session_path(cwd: str, *, home=None, session_id: str = ""):
    """Resolve Claude's transcript for ``cwd`` and optional session id."""
    if not cwd:
        return None
    try:
        directory = claude_home(home) / "projects" / re.sub(r"[/._]", "-", cwd)
        if session_id:
            path = directory / f"{session_id}.jsonl"
            return path if path.is_file() else None
        sessions = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime,
                          reverse=True)
        return sessions[0] if sessions else None
    except Exception:  # noqa: BLE001 — transcript observation must not break callers
        return None


def claude_session_paths(cwd: str, *, home=None) -> Optional[list[str]]:
    """Return the exact transcript paths present for ``cwd`` at one instant.

    This is used to prove fresh-session ownership: only a path absent from the
    dispatch snapshot can be considered newly created by that invocation.
    Observation errors intentionally yield no proof rather than a guess.
    """
    if not cwd:
        return None
    try:
        directory = claude_home(home) / "projects" / re.sub(r"[/._]", "-", cwd)
        return [str(path) for path in directory.glob("*.jsonl") if path.is_file()]
    except Exception:  # noqa: BLE001 — transcript observation must not break callers
        return None


def claude_session_size(cwd: str, *, home=None) -> tuple:
    """Return ``(bytes, session_id)`` for Claude's newest transcript."""
    path = claude_session_path(cwd, home=home)
    if path is None:
        return (0, "")
    try:
        return (path.stat().st_size, path.stem)
    except OSError:
        return (0, "")


def token(value) -> Optional[int]:
    """Return a concrete numeric token field without treating bool as int."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_context_tokens(usage) -> Optional[int]:
    """Sum the input-side token fields, preserving unknown versus zero."""
    if not isinstance(usage, dict):
        return None
    values = tuple(token(usage.get(key)) for key in (
        "cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens",
    ))
    return sum(value for value in values if value is not None) if any(
        value is not None for value in values) else None


def last_message(path, *, require_input_tokens: bool = False) -> Optional[dict]:
    """Read the newest JSONL message carrying a ``usage`` block from the tail.

    With ``require_input_tokens``, output-only or empty usage blocks are skipped
    so callers measuring a resumed context preserve unknown versus zero.
    """
    try:
        with open(path, "rb") as transcript:
            end, buffer = path.stat().st_size, b""
            for _ in range(CLAUDE_TAIL_CHUNKS):
                start = max(0, end - CLAUDE_TAIL_CHUNK)
                transcript.seek(start)
                buffer = transcript.read(end - start) + buffer
                end = start
                for line in reversed(buffer.split(b"\n")):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:  # noqa: BLE001 — malformed JSONL is skipped
                        continue
                    message = entry.get("message") or {}
                    if (isinstance(message.get("usage"), dict)
                            and (not require_input_tokens
                                 or usage_context_tokens(message["usage"]) is not None)):
                        return message
                if start == 0:
                    break
    except Exception:  # noqa: BLE001 — transcript observation must not break callers
        return None
    return None


def last_usage(path) -> Optional[dict]:
    """Read the newest JSONL ``message.usage`` block from a transcript tail."""
    message = last_message(path)
    return message.get("usage") if message is not None else None


def messages_from_offset(path, offset: int) -> list[dict]:
    """Read usage-bearing JSONL records in the bounded transcript suffix.

    The end is snapshotted before reading, so records appended after result
    handling starts are outside this observation.  Malformed lines remain
    unobserved rather than being inferred from neighbouring records.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return []
    try:
        with open(path, "rb") as transcript:
            end = path.stat().st_size
            if offset > end:
                return []
            transcript.seek(offset)
            lines = transcript.read(end - offset).splitlines()
    except Exception:  # noqa: BLE001 — transcript observation must not break callers
        return []

    messages = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001 — malformed JSONL is skipped
            continue
        message = entry.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            messages.append({"entry": entry, "message": message})
    return messages


def claude_context_tokens(cwd: str, *, home=None) -> tuple:
    """Return ``(input-side tokens, session_id)`` for Claude's newest session."""
    path = claude_session_path(cwd, home=home)
    if path is None:
        return (None, "")
    message = last_message(path, require_input_tokens=True)
    return (usage_context_tokens(message["usage"]) if message is not None else None, path.stem)
