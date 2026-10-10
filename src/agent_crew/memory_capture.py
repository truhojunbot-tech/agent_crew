"""Capture terminal evidence into ADR-001's existing store.

This module only writes observations. It does not promote procedures or enable
``AGENT_CREW_ADR001_MEMORY_ENABLED``. Callers decide whether capture is enabled.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import closing, nullcontext
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
    "btc-quant-engine",
    "agent_council", "apify-forge", "claude_autonomous_trader", "ht-8004",
})
_WARNED_UNKNOWN_PROJECTS: set[str] = set()
_ALIASES = {"agent-crew": "agent_crew", "agent crew": "agent_crew",
            "truhojunbot-tech/agent_crew": "agent_crew",
            "alpha-engine": "alpha_engine", "alpha engine": "alpha_engine",
            "quota_ops": "quota-ops", "quota ops": "quota-ops",
            "quota_core": "quota-core", "quota core": "quota-core"}
_REPO_DISAMBIGUATION = {"quota": frozenset({"quota-ops", "quota-core"})}


def _project_catalog() -> tuple[frozenset[str], dict[str, str], dict[str, frozenset[str]]]:
    """Read an optional complete project catalog; preserve fleet defaults if unset/invalid.

    AGENT_CREW_MEMORY_PROJECTS_JSON accepts an object with ``projects`` (list),
    ``aliases`` (name-to-project object), and ``repo_disambiguation``
    (name-to-list-of-projects object). A configured catalog replaces the defaults.
    """
    raw = (os.environ.get("AGENT_CREW_MEMORY_PROJECTS_JSON") or "").strip()
    defaults = (CANONICAL_PROJECTS, _ALIASES, _REPO_DISAMBIGUATION)
    if not raw:
        return defaults
    try:
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("catalog must be an object")
        names = config["projects"]
        aliases = config["aliases"]
        disambiguation = config["repo_disambiguation"]
        if (not isinstance(names, list) or not names or
                not all(isinstance(name, str) and name.strip() for name in names) or
                not isinstance(aliases, dict) or not isinstance(disambiguation, dict)):
            raise ValueError("invalid projects, aliases, or repo_disambiguation")
        projects = frozenset(name.strip().lower() for name in names)
        if not all(isinstance(key, str) and key.strip() and isinstance(value, str)
                   and value.strip().lower() in projects for key, value in aliases.items()):
            raise ValueError("alias target is not a configured project")
        mapped = {key.strip().lower(): value.strip().lower()
                  for key, value in aliases.items()}
        choices = {}
        for key, values in disambiguation.items():
            if (not isinstance(key, str) or not key.strip() or not isinstance(values, list)
                    or not values or not all(isinstance(value, str) and value.strip().lower()
                                              in projects for value in values)):
                raise ValueError("invalid repository disambiguation")
            choices[key.strip().lower()] = frozenset(value.strip().lower()
                                                       for value in values)
        return projects, mapped, choices
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("invalid AGENT_CREW_MEMORY_PROJECTS_JSON; using defaults: %s", exc)
        return defaults


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
    projects, aliases, disambiguation = _project_catalog()
    if token in projects:
        return token
    if token in aliases:
        return aliases[token]
    if token in disambiguation:
        parts = [part.lower().removesuffix(".git")
                 for part in urlparse(str(repo or "")).path.split("/") if part]
        matches = {part for part in parts if part in disambiguation[token]}
        if len(matches) == 1:
            return matches.pop()
    raise ValueError(f"unknown or ambiguous memory project: {name!r} (repo={repo!r})")


def capture_task_outcome(storage: SQLiteMemoryStorage, *, project: str, repo: str,
                         task_id: str, status: str, summary: str,
                         verdict: str = "", pr_number: int | None = None,
                         predecessor_task_ids: tuple[str, ...] = (),
                         issue: str = "", worktree: str = "",
                         provider_session: str = "",
                         context_generation: int | None = None) -> list[MemoryRecord]:
    """Idempotent raw outcome and optional decision/failure evidence."""
    canonical = canonical_project(project, repo=repo)
    if not task_id or status not in {"completed", "failed", "needs_human", "timed_out"}:
        raise ValueError("task outcome requires a terminal task id and status")
    value = {"task_id": task_id, "status": status, "summary": (summary or "")[:600],
             "verdict": verdict or "", "pr_number": pr_number,
             "project": canonical, "predecessor_task_ids": list(predecessor_task_ids),
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


def _task_context(raw: object) -> dict:
    """Legacy task contexts may be malformed; shadow recall must stay optional."""
    try:
        context = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return context if isinstance(context, dict) else {}


def task_lineage(db_path: str, task_id: str) -> tuple[tuple[str, ...], int | None]:
    """Read this task's predecessor chain and earlier terminal tasks on its PR."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=shadow_sqlite_timeout_seconds())) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT rowid, project, context, pr_number FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return (), None
        context = _task_context(row["context"])
        pr_number = row["pr_number"] or context.get("pr_number")
        predecessors: list[str] = []
        seen = {task_id}
        parent = context.get("prev_task_id")
        while isinstance(parent, str) and parent and parent not in seen:
            seen.add(parent)
            previous = db.execute(
                "SELECT project, context FROM tasks WHERE task_id=?", (parent,),
            ).fetchone()
            if previous is None:
                # A declared predecessor that has disappeared is still required
                # context; recall must observe a miss instead of reporting NULL.
                predecessors.append(parent)
                break
            if previous["project"] != row["project"]:
                break
            predecessors.append(parent)
            parent = _task_context(previous["context"]).get("prev_task_id")
        if pr_number:
            # rowid is insertion order; it excludes this task and later work
            # even when several tasks have the same created_at timestamp.
            for previous in db.execute(
                "SELECT task_id FROM tasks WHERE project=? "
                "AND COALESCE(pr_number,CASE WHEN json_valid(context) "
                "THEN json_extract(context,'$.pr_number') END)=? "
                "AND rowid<? AND status IN ('completed','failed','needs_human','timed_out') "
                "ORDER BY rowid DESC",
                (row["project"], pr_number, row["rowid"]),
            ):
                if previous["task_id"] not in seen:
                    seen.add(previous["task_id"])
                    predecessors.append(previous["task_id"])
        return tuple(predecessors), pr_number


def capture_task_outcome_record(storage: SQLiteMemoryStorage, db_path: str,
                                task_id: str, *, write: bool = True,
                                source_db: sqlite3.Connection | None = None) -> MemoryRecord | None:
    """Store one terminal crew outcome as project-wide, retrievable evidence.

    The task database is read only here. The key identifies the observation,
    so replaying a backfill cannot create duplicate memory rows.
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = (nullcontext(source_db) if source_db is not None else
                  closing(sqlite3.connect(uri, uri=True,
                                          timeout=shadow_sqlite_timeout_seconds())))
    with connection as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT task_id,task_type,description,context,status,project,summary,"
            "verdict,findings,pr_number FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if row is None or row["status"] not in {
                "completed", "failed", "needs_human", "timed_out"}:
            return None
        context = _task_context(row["context"])
        lineage = [row]
        seen = {task_id}
        parent = context.get("prev_task_id")
        while isinstance(parent, str) and parent and parent not in seen:
            seen.add(parent)
            previous = db.execute(
                "SELECT task_id,task_type,description,context,status,project,summary,"
                "verdict,findings,pr_number FROM tasks WHERE task_id=?", (parent,),
            ).fetchone()
            if previous is None or canonical_project(
                    previous["project"] or Path(db_path).parent.name) != canonical_project(
                    row["project"] or Path(db_path).parent.name):
                break
            lineage.append(previous)
            parent = _task_context(previous["context"]).get("prev_task_id")
        root = lineage[-1]
        root_context = _task_context(root["context"])
        issue = _optional_issue(context.get("issue") or root_context.get("issue")
                                or context.get("issue_number") or root_context.get("issue_number"))
        pr_number = row["pr_number"] or context.get("pr_number") or root["pr_number"]
        try:
            pr_number = int(pr_number) if pr_number else None
        except (TypeError, ValueError):
            pr_number = None
        merge_state = ""
        if pr_number:
            try:
                merged = db.execute(
                    "SELECT state FROM external_op WHERE op_key=?",
                    (f"merge:pr:{pr_number}",),
                ).fetchone()
                if merged and merged["state"] == "done":
                    merge_state = "merged"
            except sqlite3.OperationalError:
                pass  # Older crew databases have no merge receipts.
        if row["task_type"] != "review" and merge_state != "merged":
            return None
    project = canonical_project(row["project"] or Path(db_path).parent.name,
                                repo=str(context.get("repo") or ""))
    try:
        findings = json.loads(row["findings"] or "[]")
    except (TypeError, ValueError):
        findings = row["findings"] or []
    value = {
        "kind": "task_outcome", "task_id": task_id, "task_type": row["task_type"],
        "status": row["status"], "description": row["description"],
        "summary": row["summary"] or "", "verdict": row["verdict"] or "",
        "findings": findings, "project": project,
        "lineage_root_task_id": root["task_id"], "issue": issue,
        "pr_number": pr_number, "merge_state": merge_state,
        "pr_title": str(context.get("pr_title") or root_context.get("pr_title") or ""),
        "source_refs": ([f"{project} issue #{issue}"] if issue else []) +
                       ([f"{project} PR #{pr_number}"] if pr_number else []),
        "source_ref": f"crew-task:{project}:{task_id}",
    }
    record = MemoryRecord("episodic", f"task_outcome:{project}:{task_id}", value,
                          MemoryScope(project=project))
    if write:
        storage.put_many_shadow([record])
    return record


def backfill_task_outcomes(storage: SQLiteMemoryStorage,
                           task_dbs: list[str]) -> int:
    """Replay terminal tasks from explicit crew DB paths; return changed rows."""
    changed = 0
    for db_path in task_dbs:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db, closing(
                sqlite3.connect(storage.path)) as memory:
            db.row_factory = sqlite3.Row
            task_ids = [row[0] for row in db.execute(
                "SELECT task_id FROM tasks WHERE status IN "
                "('completed','failed','needs_human','timed_out') ORDER BY rowid")]
            batches: dict[str, list[MemoryRecord]] = {}
            for task_id in task_ids:
                try:
                    source_project = db.execute(
                        "SELECT project FROM tasks WHERE task_id=?", (task_id,),
                    ).fetchone()[0]
                    project = canonical_project(source_project or Path(db_path).parent.name)
                    before = memory.execute(
                        "SELECT value FROM adr001_memory WHERE key=?",
                        (f"task_outcome:{project}:{task_id}",)).fetchone()
                    record = capture_task_outcome_record(storage, db_path, task_id,
                                                         write=False, source_db=db)
                except ValueError as exc:
                    logger.warning("task outcome backfill skipped %s: %s", task_id, exc)
                    continue
                if record is not None and (before is None or json.loads(before[0]) != record.value):
                    changed += 1
                    batch = batches.setdefault(record.scope.project, [])
                    batch.append(record)
                    if len(batch) >= 100:
                        storage.put_many_shadow(batch)
                        batch.clear()
            for batch in batches.values():
                if batch:
                    storage.put_many_shadow(batch)
    return changed


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
            row = db.execute("SELECT project,context,pr_number FROM tasks WHERE task_id=?",
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
        predecessors, lineage_pr_number = task_lineage(db_path, task_id)
        records = capture_task_outcome(
            SQLiteMemoryStorage.existing(str(path)), project=row[0] if row else "",
            repo=str(context.get("repo") or context.get("target_repo") or ""),
            task_id=task_id, status=result.status, summary=result.summary,
            verdict=result.verdict or "",
            pr_number=result.pr_number or (row[2] if row else None) or lineage_pr_number,
            predecessor_task_ids=predecessors,
            issue=context.get("issue") or "",
            worktree=attribution[0] if attribution else "",
            provider_session=attribution[1] if attribution else "",
            context_generation=attribution[2] if attribution else None,
        )
        # A project-wide outcome can be recalled by later tasks. The older
        # task-scoped rows above remain for compatibility and attribution.
        capture_task_outcome_record(SQLiteMemoryStorage.existing(str(path)),
                                    db_path, task_id)
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


def capture_merge_best_effort(db_path: str, review_task_id: str) -> None:
    """Refresh a review outcome after its merge receipt is committed."""
    if not review_task_id:
        return
    try:
        shadow_db = os.getenv("AGENT_CREW_SHADOW_MEMORY_DB", "").strip()
        if (not shadow_db or os.getenv("AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED", "1").lower()
                in {"0", "false", "no", "off"}):
            return
        capture_task_outcome_record(SQLiteMemoryStorage.existing(str(Path(shadow_db).expanduser())),
                                    db_path, review_task_id)
    except Exception:
        logger.exception("shadow merge outcome capture failed for %s", review_task_id)
