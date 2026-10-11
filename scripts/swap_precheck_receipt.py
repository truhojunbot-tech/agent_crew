#!/usr/bin/env python3
"""Atomically record the owner precheck that preceded a runtime swap."""

import getpass
import hashlib
import json
import os
import re
import shlex
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _actor(caller_pid: int) -> str:
    configured = os.environ.get("AGENT_CREW_SWAP_ACTOR", "").strip()
    if configured:
        return configured
    try:
        raw = Path(f"/proc/{caller_pid}/cmdline").read_bytes()
        command = shlex.join([part.decode(errors="replace") for part in raw.split(b"\0") if part])
    except OSError:
        command = "unknown"
    return f"{getpass.getuser()}@{socket.gethostname()}:{caller_pid} {command}"


def write_receipt(argv: list[str]) -> Path:
    project, sha, step, rc_text, output_path, caller_pid_text, *command = argv
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", project):
        raise ValueError("invalid project")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
        raise ValueError("invalid target sha")
    if step not in ("preflight", "go", "post") or not command:
        raise ValueError("invalid swap step or precheck command")
    rc = int(rc_text)
    caller_pid = int(caller_pid_text)
    digest = hashlib.sha256()
    with open(output_path, "rb") as output:
        for chunk in iter(lambda: output.read(65536), b""):
            digest.update(chunk)
    directory = Path(os.environ.get("AGENT_CREW_SWAP_RECEIPT_DIR") or
                     Path.home() / ".agent_crew" / project / "swap-precheck-receipts")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    receipt = {
        "project": project,
        "target_sha": sha,
        "step": step,
        "precheck_command": shlex.join(command) if command != ["none"] else "none",
        "rc": rc,
        "output_sha256": digest.hexdigest(),
        "actor": _actor(caller_pid),
        "timestamp_utc": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    path = directory / f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}.json"
    fd, temporary = tempfile.mkstemp(prefix=".swap-precheck-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


if __name__ == "__main__":
    try:
        write_receipt(sys.argv[1:])
    except (OSError, ValueError) as exc:
        print(f"FAIL: swap precheck receipt could not be written: {exc}", file=sys.stderr)
        raise SystemExit(2)
