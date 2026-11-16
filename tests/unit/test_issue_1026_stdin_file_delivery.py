"""#1026: the dispatch prompt reaches stdin from a private file, not a late pipe write."""
import asyncio
import json
import os
import stat
import sys
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

MESSAGE = "Full prompt: " + "large task 日本語\n" * 20000


def _dispatch(tmp_path, monkeypatch, unused_tcp_port, *, body, block_loop_s=0.0,
              context=None):
    """Dispatch one claude implement task to a fake CLI running ``body``."""
    wt = tmp_path / "wt"
    wt.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "capture.json"
    db = str(tmp_path / "tasks.db")
    binary = bin_dir / "claude"
    binary.write_text(f'''#!{sys.executable}
import json, os, stat, sys, time, sqlite3
from pathlib import Path
CAPTURE = Path({str(capture)!r})
DB = {db!r}
def record(**extra):
    st = os.fstat(0)
    CAPTURE.write_text(json.dumps({{"argv": sys.argv, "path": os.readlink("/proc/self/fd/0"),
                                    "mode": stat.S_IMODE(st.st_mode), **extra}}))
def complete():
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE tasks SET status='completed' WHERE task_id='t1026'")
{body}
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"role_agents": {"implementer": "claude",
        "reviewer": "codex", "tester": "script"}, "worktrees": {"claude": str(wt)}}))
    monkeypatch.setattr(server, "_format_task_message", lambda *a, **k: MESSAGE)
    if block_loop_s:
        real_exec = asyncio.create_subprocess_exec

        async def blocking_exec(*cmd, **kwargs):
            proc = await real_exec(*cmd, **kwargs)
            time.sleep(block_loop_s)  # the event loop stalls right after spawn
            return proc

        monkeypatch.setattr(server.asyncio, "create_subprocess_exec", blocking_exec)
    app = server.create_app(db_path=db, state_path=str(state), project="demo",
                            port=unused_tcp_port, watchdog_disabled=True,
                            anomaly_disabled=True)
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="t1026", task_type="implement", description="1026",
                              branch="main", project="demo", context=context or {}))
    task = queue.dequeue(role="implementer")
    with TestClient(app):
        asyncio.run(app.state.dispatch_task(task, "implementer"))
    recorded = json.loads(capture.read_text()) if capture.exists() else None
    return recorded, queue, tmp_path / "dispatch_prompts"


def test_late_reader_under_blocked_loop_gets_full_prompt(tmp_path, monkeypatch, unused_tcp_port):
    recorded, queue, prompt_dir = _dispatch(
        tmp_path, monkeypatch, unused_tcp_port, block_loop_s=3.5,
        body="time.sleep(4)\nrecord(stdin=sys.stdin.buffer.read().decode())\ncomplete()")
    assert recorded is not None, "CLI never ran"
    assert len(MESSAGE.encode()) > 200_000
    assert recorded["stdin"] == MESSAGE
    assert all(len(arg.encode()) < 128 * 1024 for arg in recorded["argv"])
    assert recorded["mode"] == 0o600
    assert recorded["path"].startswith(str(prompt_dir) + os.sep)
    assert not os.path.exists(recorded["path"])
    assert queue.get_task("t1026").status == "completed"


def test_prompt_file_removed_after_nonzero_exit(tmp_path, monkeypatch, unused_tcp_port):
    recorded, queue, prompt_dir = _dispatch(tmp_path, monkeypatch, unused_tcp_port,
                                            body="record()\nsys.exit(3)")
    assert recorded["mode"] == 0o600
    assert recorded["path"].startswith(str(prompt_dir) + os.sep)
    assert not os.path.exists(recorded["path"])
    assert queue.get_task("t1026").status == "failed"


def test_prompt_file_removed_after_timeout_kill(tmp_path, monkeypatch, unused_tcp_port):
    recorded, queue, prompt_dir = _dispatch(tmp_path, monkeypatch, unused_tcp_port,
                                            body="record()\ntime.sleep(60)",
                                            context={"dispatch_timeout_s": 2})
    assert recorded["mode"] == 0o600
    assert recorded["path"].startswith(str(prompt_dir) + os.sep)
    assert not os.path.exists(recorded["path"])
    assert queue.get_task("t1026").status == "timed_out"


def test_closed_transport_write_yields_typed_reason(tmp_path):
    log = tmp_path / "child.log"
    log.write_bytes(b"")

    class ClosedStdin:
        def write(self, data):
            raise RuntimeError(
                "unable to perform operation on <WriteUnixTransport closed=True>")

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class Proc:
        stdin = ClosedStdin()
        returncode = 1

        async def wait(self):
            await asyncio.sleep(0.05)
            return 1

    reason, _ = asyncio.run(server._wait_for_dispatch_activity(
        Proc(), str(log), 0, hard_timeout_s=5, idle_timeout_s=5,
        on_progress=lambda: None, input_data=b"x"))
    assert reason == "stdin_closed_before_write"


def test_stdin_closed_reason_fails_task_as_infra(tmp_path, monkeypatch, unused_tcp_port):
    async def closed(proc, *a, **k):
        await proc.wait()
        return "stdin_closed_before_write", 0.0

    monkeypatch.setattr(server, "_wait_for_dispatch_activity", closed)
    recorded, queue, prompt_dir = _dispatch(tmp_path, monkeypatch, unused_tcp_port,
                                            body="record()")
    assert recorded["path"].startswith(str(prompt_dir) + os.sep)
    task = queue.get_task("t1026")
    assert task.status == "failed"
    assert task.error_info["reason"] == "stdin_closed_before_write"
    assert task.error_info["failure_class"] == "infra"
    assert not os.path.exists(recorded["path"])
