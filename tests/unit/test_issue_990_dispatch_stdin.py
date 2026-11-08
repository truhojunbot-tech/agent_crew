"""Real exec boundary: large prompts never enter Claude/Codex argv."""
import asyncio
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.mark.parametrize("agent,resume", [("codex", False), ("codex", True), ("claude", False)])
def test_large_dispatch_prompt_uses_stdin(tmp_path, monkeypatch, agent, resume, unused_tcp_port):
    wt = tmp_path / "wt"
    wt.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "capture.json"
    db = str(tmp_path / "tasks.db")
    binary = bin_dir / agent
    binary.write_text(f'''#!{sys.executable}
import json, sys, sqlite3
from pathlib import Path
Path({str(capture)!r}).write_text(json.dumps({{"argv": sys.argv, "stdin": sys.stdin.read()}}))
with sqlite3.connect({db!r}) as conn:
    conn.execute("UPDATE tasks SET status='completed' WHERE task_id='large'")
print("fake CLI completed", flush=True)
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CODEX_CONTEXT_SCOPE", "worktree")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"role_agents": {"implementer": agent,
        "reviewer": "claude" if agent == "codex" else "codex", "tester": "script"},
        "worktrees": {agent: str(wt)}}))
    message = "Full prompt: " + "large task 日本語\n" * 20000
    monkeypatch.setattr(server, "_format_task_message", lambda *a, **k: message)
    monkeypatch.setattr(server, "codex_session_for_cwd", lambda *a, **k: "session-990" if resume else "")
    app = server.create_app(db_path=db, state_path=str(state), project="demo", port=unused_tcp_port,
        watchdog_disabled=True, anomaly_disabled=True)
    queue = TaskQueue(db)
    if resume:
        queue.get_or_create_context("demo", agent, str(wt), task_id="seed")
    # #1004: codex resumes only inside the seed's lineage.
    queue.enqueue(TaskRequest(task_id="large", task_type="implement", description="large prompt",
                              branch="main", project="demo",
                              context={"lineage_root_task_id": "seed"} if resume else {}))
    task = queue.dequeue(role="implementer")
    with TestClient(app):
        asyncio.run(app.state.dispatch_task(task, "implementer"))
    assert capture.exists(), "CLI never started (possible E2BIG)"
    recorded = json.loads(capture.read_text())
    assert len(message.encode()) > 200_000
    assert recorded["stdin"] == message
    assert all(len(arg.encode()) < 128 * 1024 for arg in recorded["argv"])
    assert queue.get_task("large").status == "completed"
    if agent == "codex":
        assert recorded["argv"][-1] == "-"
        assert ("resume" in recorded["argv"]) == resume
        if resume:
            assert "session-990" in recorded["argv"]
    else:
        assert recorded["argv"][1:3] == ["-p", "--dangerously-skip-permissions"]


@pytest.mark.parametrize("exits_early", [False, True])
def test_stdin_backpressure_and_early_exit_keep_timeout_handling(tmp_path, exits_early):
    log = tmp_path / "child.log"

    async def run():
        with log.open("wb") as stream:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "pass" if exits_early else "import time; time.sleep(30)",
                stdin=asyncio.subprocess.PIPE, stdout=stream, stderr=stream,
            )
        try:
            reason, _ = await server._wait_for_dispatch_activity(
                proc, str(log), 0, hard_timeout_s=0.2, idle_timeout_s=5,
                on_progress=lambda: None, input_data=b"x" * 1_000_000,
            )
            assert reason == (None if exits_early else "dispatcher_timeout")
            assert proc.stdin.is_closing()
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    asyncio.run(run())
