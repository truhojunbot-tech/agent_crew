"""Capture terminal evidence into ADR-001's existing store.

This module only writes observations. It does not promote procedures or enable
``AGENT_CREW_ADR001_MEMORY_ENABLED``. Callers decide whether capture is enabled.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import closing
from pathlib import Path
import sqlite3
from time import perf_counter
from urllib.parse import urlparse

from agent_crew.memory_runtime import (
    MemoryRecord, MemoryScope, SQLiteMemoryStorage, ingest_blackboard_entry,
    shadow_sqlite_timeout_seconds,
)
from agent_crew.context_identity import record_context_event

logger = logging.getLogger(__name__)


CANONICAL_PROJECTS = frozenset({
    "alfred", "agent_crew", "quota-ops", "quota-core", "alpha_engine", "halla",
    "agent_council", "apify-forge", "claude_autonomous_trader", "ht-8004",
})
_WARNED_UNKNOWN_PROJECTS: set[str] = set()
_ALIASES = {"agent-crew": "agent_crew", "agent crew": "agent_crew",
            "alpha-engine": "alpha_engine", "alpha engine": "alpha_engine",
            "quota_ops": "quota-ops", "quota ops": "quota-ops",
            "quota_core": "quota-core", "quota core": "quota-core"}


def _optional_generation(value) -> int | None:
    """Zero is the existing context identity sentinel for an unknown generation."""
    try:
        generation = int(value)
        return generation if generation > 0 else None
    except (TypeError, ValueError):
        return None


def _optional_issue(value) -> str:
    """Only an existing scalar issue identifier can become scope identity."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


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
                         verdict: str = "", pr_number: int | None = None,
                         issue: str = "", worktree: str = "",
                         provider_session: str = "",
                         context_generation: int | None = None) -> list[MemoryRecord]:
    """Idempotent raw outcome and optional decision/failure evidence."""
    canonical = canonical_project(project, repo=repo)
    if not task_id or status not in {"completed", "failed", "needs_human", "timed_out"}:
        raise ValueError("task outcome requires a terminal task id and status")
    value = {"task_id": task_id, "status": status, "summary": (summary or "")[:600],
             "verdict": verdict or "", "pr_number": pr_number,
             "source_ref": f"crew-task:{canonical}:{task_id}"}
    layers = ["episodic", "decision"]
    if status in {"failed", "needs_human", "timed_out"}:
        layers.append("failure_pattern")
    scope = MemoryScope(project=canonical, task_id=task_id, issue=_optional_issue(issue),
                        worktree=str(worktree or ""),
                        provider_session=str(provider_session or ""),
                        context_generation=_optional_generation(context_generation))
    records = [MemoryRecord(layer, f"task:{task_id}:{layer}", value, scope)
               for layer in layers]
    storage.put_many_shadow(records, retire_keys=(f"task:{task_id}:failure_pattern",)
                            if status == "completed" else ())
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
    scope = MemoryScope(project=canonical, task_id=task_id,
                        issue=_optional_issue(episode.get("issue")),
                        worktree=str(episode.get("worktree") or ""),
                        provider_session=str(episode.get("provider_session")
                                             or episode.get("provider_session_id") or ""),
                        context_generation=_optional_generation(episode.get("context_generation")))
    records = [MemoryRecord(layer, f"episode:{task_id}:{layer}", value, scope)
               for layer in layers]
    storage.put_many_shadow(records, retire_keys=(f"episode:{task_id}:failure_pattern",)
                            if outcome == "completed" else ())
    return records


def capture_result_best_effort(db_path: str, task_id: str, result) -> None:
    """Observe a committed result on any transport without changing its answer."""
    events_path = None
    started = 0.0
    try:
        started = perf_counter()
        shadow_db = os.getenv("AGENT_CREW_SHADOW_MEMORY_DB", "").strip()
        if (not shadow_db or os.getenv("AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED", "1").lower()
                in {"0", "false", "no", "off"} or result.status not in {
                    "completed", "failed", "needs_human", "timed_out"}):
            return
        events_path = str(Path(db_path).resolve().parent / "context_events.jsonl")
        path = Path(shadow_db).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        timeout_seconds = shadow_sqlite_timeout_seconds(capture=True)
        with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=timeout_seconds)) as db:
            row = db.execute("SELECT project,context FROM tasks WHERE task_id=?",
                             (task_id,)).fetchone()
            try:
                attribution = db.execute(
                    "SELECT worktree_path,provider_session_id,context_generation "
                    "FROM task_attribution WHERE task_id=?", (task_id,)).fetchone()
            except sqlite3.OperationalError:
                # Older task databases may not have the optional attribution table.
                attribution = None
        context = json.loads(row[1] or "{}") if row else {}
        if not isinstance(context, dict):
            context = {}
        records = capture_task_outcome(
            SQLiteMemoryStorage.existing(str(path)), project=row[0] if row else "",
            repo=str(context.get("repo") or context.get("target_repo") or ""),
            task_id=task_id, status=result.status, summary=result.summary,
            verdict=result.verdict or "", pr_number=result.pr_number,
            issue=context.get("issue") or "",
            worktree=attribution[0] if attribution else "",
            provider_session=attribution[1] if attribution else "",
            context_generation=attribution[2] if attribution else None,
        )
        record_context_event(events_path, "shadow_memory_capture",
                             task_id=task_id, outcome="stored",
                             layers=[record.layer for record in records],
                             latency_ms=round((perf_counter() - started) * 1000, 3),
                             over_budget=(perf_counter() - started) > timeout_seconds)
    except Exception as exc:
        unknown = isinstance(exc, ValueError) and "unknown or ambiguous memory project" in str(exc)
        warning_key = row[0] if unknown and "row" in locals() and row else ""
        if not unknown or warning_key not in _WARNED_UNKNOWN_PROJECTS:
            logger.warning("shadow memory capture failed for %s: %s", task_id, exc)
            if unknown:
                _WARNED_UNKNOWN_PROJECTS.add(warning_key)
        try:
            if events_path:
                record_context_event(events_path, "shadow_memory_capture",
                                     task_id=task_id, outcome="rejected",
                                     error_type=type(exc).__name__, reason=str(exc)[:200],
                                     latency_ms=round((perf_counter() - started) * 1000, 3))
        except Exception:
            logger.exception("shadow memory capture telemetry failed for %s", task_id)
