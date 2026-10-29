"""Configured, serialized callers for the ADR-001 backfills."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from agent_crew.memory_capture import backfill_document_pointers, backfill_task_outcomes
from agent_crew.memory_runtime import SQLiteMemoryStorage

logger = logging.getLogger(__name__)


@contextmanager
def ingest_lock(path: str):
    """One writer across servers sharing a memory database."""
    with open(path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_config(path: str) -> dict:
    config = json.loads(Path(path).expanduser().read_text())
    if (not isinstance(config, dict) or not isinstance(config.get("memory_db"), str)
            or not config["memory_db"].strip()):
        raise ValueError("memory ingest config requires memory_db")
    task_dbs = config.get("task_dbs", [])
    repos = config.get("repos", {})
    if (not isinstance(task_dbs, list) or
            any(not isinstance(db, str) or not db.strip() for db in task_dbs) or
            not isinstance(repos, dict) or
            any(not isinstance(repo, str) or not repo.strip() or
                not isinstance(root, str) or not root.strip()
                for repo, root in repos.items())):
        raise ValueError("memory ingest config requires task_dbs list and repos object")
    explicit = config.get("explicit_files", {})
    historic = config.get("historic_files", {})
    if (not isinstance(explicit, dict) or
            any(not isinstance(repo, str) or not isinstance(paths, list) or
                any(not isinstance(item, str) for item in paths)
                for repo, paths in explicit.items()) or
            not isinstance(historic, dict) or
            any(not isinstance(repo, str) or not isinstance(items, list) or
                any(not isinstance(item, (list, tuple)) or len(item) != 2 or
                    any(not isinstance(part, str) for part in item) for item in items)
                for repo, items in historic.items())):
        raise ValueError("memory ingest explicit_files and historic_files must contain paths")
    interval = config.get("interval_seconds", 900)
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not 60 <= interval <= 3600):
        raise ValueError("memory ingest interval_seconds must be between 60 and 3600")
    return config


def run_configured_ingest(path: str) -> tuple[int, int] | None:
    """Capture terminal tasks and the latest published docs; skip if another run owns the lock."""
    config = load_config(path)
    memory_db = str(Path(config["memory_db"]).expanduser())
    storage = SQLiteMemoryStorage.existing(memory_db)
    try:
        with ingest_lock(memory_db + ".ingest.lock"):
            task_changed = 0
            failed_task_dbs = []
            for db in config.get("task_dbs", []):
                task_db = str(Path(db).expanduser())
                try:
                    task_changed += backfill_task_outcomes(storage, [task_db])
                except Exception:
                    failed_task_dbs.append(task_db)
                    logger.exception("memory ingest: task DB %s failed", task_db)
            repos = [(repo, str(Path(root).expanduser()))
                     for repo, root in config.get("repos", {}).items()]
            explicit = {repo: set(paths) for repo, paths in
                        config.get("explicit_files", {}).items()}
            historic = {repo: [tuple(item) for item in items] for repo, items in
                        config.get("historic_files", {}).items()}
            doc_changed = 0
            failed_repos = []
            for repo, root in repos:
                try:
                    # A dispatcher checkout can remain detached on an earlier task.
                    # Refresh remote refs before scanning the published default branch.
                    if subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                                      capture_output=True, timeout=5).returncode == 0:
                        subprocess.run(["git", "-C", root, "fetch", "--quiet", "origin"],
                                       capture_output=True, timeout=30, check=True)
                    doc_changed += backfill_document_pointers(
                        storage, [(repo, root)], explicit, historic)
                except Exception:
                    failed_repos.append(repo)
                    logger.exception("memory ingest: repo %s failed", repo)
            logger.info("memory ingest: tasks=%s documents=%s failed_task_dbs=%s failed_repos=%s",
                        task_changed, doc_changed, failed_task_dbs, failed_repos)
            return task_changed, doc_changed
    except BlockingIOError:
        logger.info("memory ingest: another server owns the lock")
        return None


def _run_best_effort(path: str) -> None:
    try:
        run_configured_ingest(path)
    except Exception:
        logger.exception("memory ingest failed; next scheduled run may retry")


async def periodic_ingest(path: str, interval_seconds: float) -> None:
    while True:
        await asyncio.to_thread(_run_best_effort, path)
        await asyncio.sleep(interval_seconds)


def launch_postmerge_ingest(path: str) -> threading.Thread:
    worker = threading.Thread(target=_run_best_effort, args=(path,), daemon=True,
                              name="agent-crew-memory-ingest")
    worker.start()
    return worker
