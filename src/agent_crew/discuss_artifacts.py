"""#1025: keep a discuss answer the worker left in its worktree.

A discuss worker may write its answer to a file and report only the path.
Nothing commits that file, and dispatch prep runs ``git reset --hard`` /
``git clean -fd`` in the same worktree before the next task, so the answer
was lost. The result endpoint calls :func:`capture_discuss_result` before it
stores the row: every new or modified file in the dispatching worktree is
copied into ``findings`` as a ``discuss_artifact`` entry. Stored findings
are strings (``queue._stored_findings``), so each entry is a JSON object
encoded as one string; :func:`parse_artifact` reads it back.

Capture is best effort. Errors become warnings on the result and never fail
or hold it.
"""
from __future__ import annotations

import codecs
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

#: Total bytes of file content copied into one result's findings.
DISCUSS_ARTIFACT_CAP_BYTES = 256 * 1024
#: Bytes read per step while hashing and classifying a file.
_READ_CHUNK_BYTES = 64 * 1024
ARTIFACT_KIND = "discuss_artifact"
MISSING_WARNING = "discuss_artifact_missing"

_POINTER_RE = re.compile(r"[\w./~-]+\.(?:md|txt|json)\b")


def _dirty_paths(worktree: str) -> list[str]:
    """New and modified paths relative to ``worktree`` (deletions skipped)."""
    out = subprocess.run(
        ["git", "-C", worktree, "status", "--porcelain=v1", "-z",
         "--untracked-files=all"],
        capture_output=True, timeout=30, check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # the next field is the rename/copy source
        if "D" in xy:
            continue
        paths.append(path)
    return paths


def _read_artifact(full: str, rel: str, budget: int) -> tuple[dict, int]:
    """Return ``(entry, stored bytes)``, keeping at most ``budget`` bytes of text.

    The file is hashed and classified in chunks over its full length.

    Peak memory is one read chunk plus the stored prefix, whatever the file
    size (review of PR #1027: a multi-GB file must not be loaded whole).
    """
    hasher = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    binary = False
    prefix = bytearray()
    size = 0
    with open(full, "rb") as file:
        while True:
            chunk = file.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
            if binary:
                continue
            if b"\0" in chunk:
                binary = True
                continue
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                binary = True
                continue
            if len(prefix) < budget:
                prefix += chunk[:budget - len(prefix)]
    if not binary:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            binary = True
    entry: dict = {"kind": ARTIFACT_KIND, "path": rel, "sha256": hasher.hexdigest()}
    if binary:
        entry["binary"] = True
        return entry, 0
    if size > len(prefix):
        # Cut on a character boundary so the stored text stays valid UTF-8.
        entry["content"] = bytes(prefix).decode("utf-8", "ignore")
        entry["truncated"] = True
    else:
        entry["content"] = bytes(prefix).decode("utf-8")
    return entry, len(prefix)


def capture_worktree_artifacts(worktree: str,
                               cap: int = DISCUSS_ARTIFACT_CAP_BYTES
                               ) -> tuple[list[dict], list[dict]]:
    """Return ``(artifact entries, warnings)`` for the worktree's dirty files."""
    entries: list[dict] = []
    warnings: list[dict] = []
    budget = cap
    for rel in _dirty_paths(worktree):
        full = os.path.join(worktree, rel)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        try:
            entry, stored = _read_artifact(full, rel, budget)
        except OSError as exc:
            warnings.append({"kind": "discuss_artifact_capture_error",
                             "path": rel, "detail": str(exc)[:200]})
            continue
        budget -= stored
        entries.append(entry)
    return entries, warnings


def parse_artifact(finding) -> Optional[dict]:
    """Return the entry if ``finding`` is an encoded ``discuss_artifact``."""
    if not isinstance(finding, str) or not finding.startswith("{"):
        return None
    try:
        value = json.loads(finding)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("kind") == ARTIFACT_KIND else None


def missing_pointers(summary: str, worktree: Optional[str]) -> list[str]:
    """Paths named in ``summary`` that are absent or empty in the worktree."""
    missing: list[str] = []
    for raw in _POINTER_RE.findall(summary or ""):
        path = raw.rstrip(".")
        full = os.path.expanduser(path)
        if not os.path.isabs(full):
            if not worktree:
                missing.append(path)
                continue
            full = os.path.join(worktree, full)
        try:
            empty = not os.path.isfile(full) or os.path.getsize(full) == 0
        except OSError:
            empty = True
        if empty and path not in missing:
            missing.append(path)
    return missing


def capture_discuss_result(result, worktree: Optional[str]):
    """Return ``(result, missing_paths)`` with worktree files in ``findings``.

    ``missing_paths`` is non-empty only when the worker reported no findings,
    named a file in its summary, and that file is missing or empty. The caller
    sends the coordinator notice; the result keeps its status either way.
    """
    original_findings = list(result.findings or [])
    entries: list[dict] = []
    warnings: list[dict] = []
    if worktree and os.path.isdir(worktree):
        try:
            entries, warnings = capture_worktree_artifacts(worktree)
        except Exception as exc:
            logger.exception("discuss artifact capture failed worktree=%s", worktree)
            warnings.append({"kind": "discuss_artifact_capture_error",
                             "detail": str(exc)[:200]})
    missing: list[str] = []
    if not original_findings:
        try:
            missing = missing_pointers(result.summary or "", worktree)
        except Exception:
            logger.exception("discuss artifact pointer check failed")
    if missing:
        warnings.append({"kind": MISSING_WARNING, "paths": missing})
    if not entries and not warnings:
        return result, missing
    encoded = [json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in entries]
    changes: dict = {"findings": original_findings + encoded}
    if warnings:
        info = dict(result.error_info) if isinstance(result.error_info, dict) else {}
        info["warnings"] = list(info.get("warnings") or []) + warnings
        if missing:
            info["warning"] = MISSING_WARNING
        changes["error_info"] = info
    return dataclasses.replace(result, **changes), missing
