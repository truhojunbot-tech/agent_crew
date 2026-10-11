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
    if not isinstance(config, dict) or not isinstance(config.get("memory_db"), str):
        raise ValueError("memory ingest config requires memory_db")
    if not isinstance(config.get("task_dbs", []), list) or not isinstance(config.get("repos", {}), dict):
        raise ValueError("memory ingest config requires task_dbs list and repos object")
    interval = config.get("interval_seconds", 900)
    if not isinstance(interval, (int, float)) or not 60 <= interval <= 3600:
        raise ValueError("memory ingest interval_seconds must be between 60 and 3600")
    return config


def run_configured_ingest(path: str) -> tuple[int, int] | None:
    """Capture terminal tasks and the latest published docs; skip if another run owns the lock."""
    config = load_config(path)
    memory_db = str(Path(config["memory_db"]).expanduser())
    Path(memory_db).parent.mkdir(parents=True, exist_ok=True)
    try:
        with ingest_lock(memory_db + ".ingest.lock"):
            storage = SQLiteMemoryStorage(memory_db, busy_timeout_seconds=5.0)
            task_changed = backfill_task_outcomes(
                storage, [str(Path(db).expanduser()) for db in config.get("task_dbs", [])])
            repos = [(repo, str(Path(root).expanduser()))
                     for repo, root in config.get("repos", {}).items()]
            for repo, root in repos:
                # A dispatcher checkout can remain detached on an earlier task.
                # Refresh remote refs before scanning the published default branch.
                if subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                                  capture_output=True, timeout=5).returncode == 0:
                    subprocess.run(["git", "-C", root, "fetch", "--quiet", "origin"],
                                   capture_output=True, timeout=30, check=True)
            explicit = {repo: set(paths) for repo, paths in
                        config.get("explicit_files", {}).items()}
            historic = {repo: [tuple(item) for item in items] for repo, items in
                        config.get("historic_files", {}).items()}
            doc_changed = backfill_document_pointers(storage, repos, explicit, historic)
            logger.info("memory ingest: tasks=%s documents=%s", task_changed, doc_changed)
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
