"""Read-only row 1.5 shadow measurement and isolated rollback probe (#431)."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from time import perf_counter

from agent_crew.k4_shadow_experiment import CorpusRecord, LexicalProvider
from agent_crew.memory import MemoryItem, MemoryRequest, shadow_retrieve
from agent_crew.memory_runtime import RuntimeMemoryProvider, SQLiteMemoryStorage

PROJECTS = ("alfred", "agent_crew", "quota-ops")
BUILD = "a138976"
TERMINAL = frozenset({"completed", "failed", "needs_human", "timed_out"})
# Known local fixture families and exact IDs from the #430 incident. Keep short
# IDs exact so a real task beginning with "impl-r" or "rev-s" stays in the sample.
TEST_PREFIXES = ("test-", "t-e2e", "t-http-", "t-mcp-", "adm-", "ctl-", "cxc")
TEST_IDS = frozenset({"t-mcp", "done-attempt", "impl-r", "rev-s"})


def read_only(path: Path) -> sqlite3.Connection:
    """The only opening mode used for any live database."""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def is_test_id(task_id: str) -> bool:
    return task_id in TEST_IDS or task_id.startswith(TEST_PREFIXES)


def select_tasks(db: sqlite3.Connection, build: str = BUILD) -> list[dict]:
    """Use claim provenance, never infer a build from wall clock alone."""
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT task_id, project, status, description, context,
           claim_build_commit, result_posted_at FROM tasks
           WHERE claim_build_commit LIKE ? AND status IN ('completed','failed','needs_human','timed_out')
           ORDER BY result_posted_at DESC, task_id""", (build + "%",)).fetchall()
    return [dict(row) for row in rows if not is_test_id(row["task_id"])]


def capture_completeness(tasks: list[dict], rows: list[dict]) -> dict:
    by_task: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row["key"].startswith("task:"):
            task_id = row["key"][5:].rsplit(":", 1)[0]
            by_task.setdefault((row["scope"].get("project", ""), task_id), []).append(row)
    missing, present = [], []
    fill = Counter()
    for task in tasks:
        task_id = task["task_id"]
        project = task["project"]
        matches = by_task.get((project, task_id), [])
        episode = next((row for row in matches if row["layer"] == "episodic"), None)
        decision = next((row for row in matches if row["layer"] == "decision"), None)
        failure = next((row for row in matches if row["layer"] == "failure_pattern"), None)
        expected_failure = task["status"] != "completed"
        if not episode or not decision or (expected_failure and not failure):
            absent = [layer for layer, found in (("episodic", episode), ("decision", decision),
                      ("failure_pattern", failure if expected_failure else True)) if not found]
            missing.append({"project": project, "task_id": task_id,
                            "reason": "missing_capture_layers", "layers": absent})
        if episode:
            present.append(task_id)
            scope = episode["scope"]
            for field in ("task_id", "issue", "provider_session", "context_generation", "worktree"):
                fill[field] += bool(scope.get(field))
    return {"terminal_tasks": len(tasks), "captured_episodic": len(present),
            "complete_tasks": len(tasks) - len(missing), "missing": missing,
            "scope_fill": {field: {"filled": fill[field], "denominator": len(present)}
                for field in ("task_id", "issue", "provider_session", "context_generation", "worktree")}}


def _memory_rows(db: sqlite3.Connection) -> list[dict]:
    return [{"layer": layer, "key": key, "value": json.loads(value),
             "scope": json.loads(scope)} for layer, key, value, scope in db.execute(
                 "SELECT layer,key,value,scope FROM adr001_memory")]


def _issue(task: dict) -> str:
    try:
        value = json.loads(task["context"] or "{}").get("issue", "")
    except (ValueError, AttributeError):
        return ""
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def retrieval_comparison(tasks: list[dict], rows: list[dict], storage: SQLiteMemoryStorage) -> dict:
    corpus = tuple(CorpusRecord(MemoryItem(item_id=r["key"], project=r["scope"].get("project", ""),
                      memory_type=r["layer"], source_ref=r["key"]),
                      text=str(r["value"].get("summary") or r["value"].get("topic") or ""))
                   for r in rows if r["layer"] in {"procedural", "episodic", "decision", "failure_pattern"})
    lexical = LexicalProvider(corpus)
    runtime = RuntimeMemoryProvider(storage)
    samples = []
    for task in tasks:
        issue = _issue(task)
        request = MemoryRequest(project=task["project"], task_id=task["task_id"], issue=issue,
            retrieval_query=task["description"] or "", query_source="task_description", limit=10)
        variants = {}
        for label, provider in (("runtime", runtime), ("k4_lexical", lexical)):
            result = shadow_retrieve(provider, request)
            hits = Counter(item.memory_type for item in result.items)
            ranked_ids = [item.item_id for item in result.items]
            same_task = [i + 1 for i, key in enumerate(ranked_ids) if key.startswith(f"task:{task['task_id']}:")]
            issue_ranks = [i + 1 for i, item in enumerate(result.items)
                 if any(r["key"] == item.item_id and r["scope"].get("issue") == issue
                        for r in rows) and issue]
            variants[label] = {"state": result.state, "hits_by_layer": dict(hits),
                "same_project": sum(item.project == task["project"] for item in result.items),
                "cross_project": sum(item.project != task["project"] for item in result.items),
                "dropped_cross_project": result.dropped_cross_project,
                "same_task_best_rank": min(same_task, default=None),
                "same_issue_best_rank": min(issue_ranks, default=None),
                "latency_ms": round(result.latency_ms, 3), "result_ids": ranked_ids}
        samples.append({"project": task["project"], "task_id": task["task_id"], "issue": issue,
                        "variants": variants})
    return {"sample_count": len(samples), "samples": samples}


def sandbox_rollback() -> dict:
    """Three fresh app lifespans, one temporary HOME and task/memory databases."""
    from fastapi.testclient import TestClient
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app
    with tempfile.TemporaryDirectory(prefix="row15-rollback-") as root:
        home = Path(root)
        memory_path = home / "memory.db"
        task_path = home / "tasks.db"
        SQLiteMemoryStorage(str(memory_path))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        keys = ("HOME", "AGENT_CREW_SHADOW_MEMORY_DB", "AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED",
                "AGENT_CREW_SHADOW_MEMORY_ENABLED", "AGENT_CREW_CEA_MODE",
                "AGENT_CREW_CEA_MODE__AGENT_CREW", "AGENT_CREW_PANE_MAP",
                "AGENT_CREW_WORKTREE_SYNC_DISABLED")
        original = {key: os.environ.get(key) for key in keys}
        os.environ.update({"HOME": str(home), "AGENT_CREW_SHADOW_MEMORY_DB": str(memory_path),
                           "AGENT_CREW_CEA_MODE": "off", "AGENT_CREW_CEA_MODE__AGENT_CREW": "off",
                           "AGENT_CREW_PANE_MAP": str(home / "pane_map.json"),
                           "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1"})
        stages = []
        try:
            for index, enabled in enumerate((True, False, True), 1):
                os.environ["AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED"] = str(int(enabled))
                os.environ["AGENT_CREW_SHADOW_MEMORY_ENABLED"] = str(int(enabled))
                task_id = f"row15-sandbox-{index}"
                before = perf_counter()
                app = create_app(str(task_path), pane_map={}, port=free_port, project="agent_crew",
                                 watchdog_disabled=True, anomaly_disabled=True, worktree_map={},
                                 state_path=str(home / "state.json"))
                with TestClient(app) as api:
                    queue = TaskQueue(str(task_path))
                    queue.enqueue(TaskRequest(task_id=task_id, task_type="discuss",
                                              description="sandbox rollback", project="agent_crew"))
                    response = api.post(f"/tasks/{task_id}/result", json={
                        "task_id": task_id, "status": "completed", "summary": "sandbox observation"})
                    status = queue.get_task_status(task_id)
                with closing(sqlite3.connect(memory_path)) as db:
                    captured = db.execute("SELECT count(*) FROM adr001_memory WHERE key LIKE ?",
                                          (f"task:{task_id}:%",)).fetchone()[0]
                stages.append({"enabled": enabled, "http_status": response.status_code,
                               "task_status": status, "capture_rows": captured,
                               "elapsed_ms": round((perf_counter() - before) * 1000, 3)})
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return {"temp_home": True, "free_port_used_for_app_config_only": True, "stages": stages,
                "pass": all(s["http_status"] == 200 and s["task_status"] == "completed" and
                    s["capture_rows"] == (2 if s["enabled"] else 0) for s in stages)}


def run(base: Path, output: Path) -> dict:
    all_tasks = []
    counts = {}
    for project in PROJECTS:
        path = base / project / "tasks.db"
        with closing(read_only(path)) as db:
            tasks = select_tasks(db)
        counts[project] = len(tasks)
        all_tasks.extend(tasks)
    live_memory = base / "memory" / "adr001_memory.db"
    with tempfile.TemporaryDirectory(prefix="row15-snapshot-") as root:
        snapshot = Path(root) / "memory.db"
        with closing(read_only(live_memory)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        with closing(read_only(snapshot)) as db:
            rows = _memory_rows(db)
        storage = SQLiteMemoryStorage.existing(str(snapshot))
        retrieval = retrieval_comparison(all_tasks, rows, storage)
    evidence = {"build_prefix": BUILD, "provenance": "claim_build_commit", "live_db_open_mode": "ro",
        "project_terminal_counts": counts, "memory_rows_snapshot": len(rows),
        "capture_completeness": capture_completeness(all_tasks, rows),
        "retrieval": retrieval, "rollback": sandbox_rollback()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path.home() / ".agent_crew")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.base, args.output)
    print(json.dumps({"terminal": result["capture_completeness"]["terminal_tasks"],
                      "complete": result["capture_completeness"]["complete_tasks"],
                      "rollback_pass": result["rollback"]["pass"]}))
