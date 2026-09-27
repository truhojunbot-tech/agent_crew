"""Regression coverage for inherited shadow-memory configuration (#430)."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent_crew.memory_capture import capture_result_best_effort
from agent_crew.memory_runtime import SQLiteMemoryStorage
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import worker_environment


def test_worker_environment_drops_server_memory_controls():
    source = {
        "AGENT_CREW_SHADOW_MEMORY_DB": "/sentinel/live.db",
        "AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED": "1",
        "AGENT_CREW_ADR001_MEMORY_ENABLED": "1",
        "AGENT_CREW_ADR001_FUTURE_SWITCH": "1",
        "AGENT_CREW_OTHER": "keep",
    }
    child = worker_environment(source, "/tmp/worker")
    assert child["AGENT_CREW_OTHER"] == "keep"
    assert child["TELEGRAM_STATE_DIR"] == "/tmp/worker/.telegram"
    assert not any(key.startswith(("AGENT_CREW_SHADOW_MEMORY_", "AGENT_CREW_ADR001_"))
                   for key in child)


def test_capture_child_cannot_reach_inherited_sentinel(tmp_path):
    if os.getenv("AGENT_CREW_TEST_SHADOW_CHILD") != "1":
        pytest.skip("only run in the hostile-environment child")
    assert not any(key.startswith(("AGENT_CREW_SHADOW_MEMORY_", "AGENT_CREW_ADR001_"))
                   for key in os.environ)
    assert os.environ["HOME"] != os.environ["AGENT_CREW_TEST_ORIGINAL_HOME"]
    task_db = str(tmp_path / "tasks.db")
    queue = TaskQueue(task_db)
    queue.enqueue(TaskRequest(task_id="shadow-sentinel", task_type="implement",
                              description="capture-producing fixture", project="agent_crew"))
    result = type("Result", (), {"status": "completed", "summary": "done",
                                   "verdict": None, "pr_number": None})()
    capture_result_best_effort(task_db, "shadow-sentinel", result)


def test_inherited_live_style_memory_db_remains_untouched(tmp_path):
    sentinel = tmp_path / "sentinel.db"
    SQLiteMemoryStorage(str(sentinel))
    with sqlite3.connect(sentinel) as db:
        db.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        db.execute("INSERT INTO sentinel VALUES ('untouched')")
    before = sentinel.read_bytes()
    env = dict(os.environ, AGENT_CREW_SHADOW_MEMORY_DB=str(sentinel),
               AGENT_CREW_SHADOW_MEMORY_ENABLED="1",
               AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED="1",
               AGENT_CREW_ADR001_MEMORY_ENABLED="1",
               AGENT_CREW_TEST_SHADOW_CHILD="1",
               AGENT_CREW_TEST_ORIGINAL_HOME=os.environ["HOME"])
    root = Path(__file__).resolve().parents[2]
    # HOME isolation hides user-site packages from the child interpreter.
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"),
                                          str(Path(pytest.__file__).resolve().parent.parent)))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(Path(__file__)), "-k",
         "test_capture_child_cannot_reach_inherited_sentinel"],
        cwd=root, env=env, text=True, capture_output=True, timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert sentinel.read_bytes() == before
    with sqlite3.connect(sentinel) as db:
        assert db.execute("SELECT value FROM sentinel").fetchall() == [("untouched",)]
