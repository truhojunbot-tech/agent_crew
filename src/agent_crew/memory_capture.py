"""Capture terminal evidence into ADR-001's existing store, never into live context.

This module only writes observations. It does not promote procedures or enable
``AGENT_CREW_ADR001_MEMORY_ENABLED``. Callers decide whether capture is enabled.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

from agent_crew.memory_runtime import (
    MemoryRecord, MemoryScope, SQLiteMemoryStorage, ingest_blackboard_entry,
)
from agent_crew.context_identity import record_context_event

logger = logging.getLogger(__name__)


CANONICAL_PROJECTS = frozenset({
    "alfred", "agent_crew", "quota-ops", "quota-core", "alpha_engine", "halla",
})
_ALIASES = {"agent-crew": "agent_crew", "agent crew": "agent_crew",
            "alpha-engine": "alpha_engine", "alpha engine": "alpha_engine",
            "quota_ops": "quota-ops", "quota ops": "quota-ops",
            "quota_core": "quota-core", "quota core": "quota-core"}


def canonical_project(name: str, *, repo: str = "") -> str:
    """Resolve one owner/bot name; ambiguous Quota requires a repository."""
    token = str(name or "").strip().lower()
    if token in CANONICAL_PROJECTS:
        return token
    if token in _ALIASES:
        return _ALIASES[token]
    if token == "quota":
        parts = [part.lower().removesuffix(".git")
                 for part in urlparse(str(repo or "")).path.split("/") if part]
        matches = {part for part in parts if part in {"quota-ops", "quota-core"}}
        if len(matches) == 1:
            return matches.pop()
    raise ValueError(f"unknown or ambiguous memory project: {name!r} (repo={repo!r})")


def capture_task_outcome(storage: SQLiteMemoryStorage, *, project: str, repo: str,
                         task_id: str, status: str, summary: str,
                         verdict: str = "", pr_number: int | None = None) -> list[MemoryRecord]:
    """Idempotent raw outcome and optional decision/failure evidence."""
    canonical = canonical_project(project, repo=repo)
    if not task_id or status not in {"completed", "failed", "needs_human"}:
        raise ValueError("task outcome requires a terminal task id and status")
    value = {"task_id": task_id, "status": status, "summary": (summary or "")[:600],
             "verdict": verdict or "", "pr_number": pr_number,
             "source_ref": f"crew-task:{canonical}:{task_id}"}
    layers = ["episodic", "decision"]
    if status in {"failed", "needs_human"}:
        layers.append("failure_pattern")
    records = [MemoryRecord(layer, f"task:{task_id}:{layer}", value,
                            MemoryScope(project=canonical))
               for layer in layers]
    for record in records:
        storage.put(record)
    return records


def capture_blackboard_result(storage: SQLiteMemoryStorage, frontmatter: dict) -> MemoryRecord:
    """Map the Blackboard bot name, then use the existing ingest function."""
    source = str(frontmatter.get("repo") or frontmatter.get("link") or "")
    mapped = {**frontmatter, "source_from": frontmatter.get("from", ""),
              "from": canonical_project(frontmatter.get("from", ""), repo=source)}
    return ingest_blackboard_entry(storage, mapped)


def capture_episode(storage: SQLiteMemoryStorage, episode: dict, *, project: str,
                    repo: str = "") -> list[MemoryRecord]:
    """Import one procedural_memory JSONL episode as evidence, never a rule."""
    canonical = canonical_project(project, repo=repo)
    task_id = str(episode.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("episode has no task_id")
    outcome = str(episode.get("outcome") or "")
    value = {"task_id": task_id, "status": outcome,
             "summary": str(episode.get("summary") or "")[:600],
             "source_ref": f"episodes.jsonl:{canonical}:{task_id}"}
    layers = ["episodic", "decision"]
    if episode.get("verdict"):
        value["verdict"] = episode["verdict"]
    if outcome.startswith("failed") or outcome == "needs_human":
        layers.append("failure_pattern")
    records = [MemoryRecord(layer, f"episode:{task_id}:{layer}", value,
                            MemoryScope(project=canonical))
               for layer in layers]
    for record in records:
        storage.put(record)
    return records


def capture_result_best_effort(db_path: str, task_id: str, result) -> None:
    """Observe a committed result on any transport without changing its answer."""
    shadow_db = os.getenv("AGENT_CREW_SHADOW_MEMORY_DB", "").strip()
    if not shadow_db or result.status not in {"completed", "failed", "needs_human"}:
        return
    events_path = str(Path(db_path).resolve().parent / "context_events.jsonl")
    try:
        path = Path(shadow_db).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        with sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT project,context FROM tasks WHERE task_id=?",
                             (task_id,)).fetchone()
        context = json.loads(row[1] or "{}") if row else {}
        records = capture_task_outcome(
            SQLiteMemoryStorage(str(path)), project=row[0] if row else "",
            repo=str(context.get("repo") or context.get("target_repo") or ""),
            task_id=task_id, status=result.status, summary=result.summary,
            verdict=result.verdict or "", pr_number=result.pr_number,
        )
        record_context_event(events_path, "shadow_memory_capture",
                             task_id=task_id, outcome="stored",
                             layers=[record.layer for record in records])
    except Exception as exc:
        logger.warning("shadow memory capture failed for %s: %s", task_id, exc)
        try:
            record_context_event(events_path, "shadow_memory_capture",
                                 task_id=task_id, outcome="rejected",
                                 error_type=type(exc).__name__, reason=str(exc)[:200])
        except Exception:
            logger.exception("shadow memory capture telemetry failed for %s", task_id)
