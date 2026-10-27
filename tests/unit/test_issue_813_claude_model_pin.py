"""The Claude model pin follows context identity and stays out of server env."""

import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.telemetry import TaskTelemetry


IDENTITY_KEYS = {
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_SESSION_NAME",
}


def _harness(tmp_path, monkeypatch, unused_tcp_port):
    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "worktrees": {"claude": str(wt)},
        "role_agents": {"implementer": "claude", "reviewer": "codex", "tester": "gemini"},
    }))
    db = str(tmp_path / "tasks.db")
    seen = []

    async def fake_exec(*cmd, **kwargs):
        seen.append((list(cmd), kwargs["env"]))

        class Process:
            pid = 1
            returncode = 0

            async def communicate(self):
                return b"", b""

            async def wait(self):
                return 0

        return Process()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLAUDE_CONTEXT_SCOPE", "worktree")
    monkeypatch.setattr(sv.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sv, "claude_context_exceeds_cap", lambda *a, **kw: (False, {}))
    app = sv.create_app(
        db, pane_map={}, port=unused_tcp_port, state_path=str(state), project="p",
        watchdog_disabled=True, anomaly_disabled=True,
    )
    return app, TaskQueue(db), seen


def _dispatch(app, queue, task_id):
    queue.enqueue(TaskRequest(task_id, "implement", "do it", branch="main"))
    task = queue.dequeue(role="implementer")
    assert task and task.task_id == task_id
    asyncio.run(app.state.dispatch_task(task, "implementer"))
    return queue.get_attribution(task_id)


def test_claude_model_pin_and_model_change_reset_once(tmp_path, monkeypatch, unused_tcp_port):
    monkeypatch.delenv("AGENT_CREW_CLAUDE_MODEL", raising=False)
    app, queue, seen = _harness(tmp_path, monkeypatch, unused_tcp_port)
    with TestClient(app):
        first = _dispatch(app, queue, "impl-813-a")
        second = _dispatch(app, queue, "impl-813-b")
        assert "--model" not in seen[0][0]
        assert seen[1][0] == [
            "claude", "-p", seen[1][0][2], "--continue",
            "--dangerously-skip-permissions", "--verbose", "--output-format", "stream-json",
        ]
        assert first["model"] == second["model"] == ""

        monkeypatch.setenv("AGENT_CREW_CLAUDE_MODEL", "claude-fable-5-1")
        changed = _dispatch(app, queue, "impl-813-c")
        same = _dispatch(app, queue, "impl-813-d")
        monkeypatch.delenv("AGENT_CREW_CLAUDE_MODEL")
        unpinned = _dispatch(app, queue, "impl-813-e")

    assert "--model" in seen[2][0]
    assert seen[2][0][seen[2][0].index("--model") + 1] == "claude-fable-5-1"
    assert "--continue" not in seen[2][0]
    assert "--continue" in seen[3][0]
    assert changed["context_policy"] == "fresh"
    assert same["context_policy"] == "resume"
    assert changed["context_generation"] == second["context_generation"] + 1
    assert same["context_generation"] == changed["context_generation"]
    assert changed["model"] == same["model"] == "claude-fable-5-1"
    assert "--model" not in seen[4][0]
    assert "--continue" in seen[4][0]
    assert unpinned["context_policy"] == "resume"


def test_provider_child_env_drops_only_claude_session_identity():
    source = {key: "secret" for key in IDENTITY_KEYS}
    source.update({
        "CLAUDE_EFFORT": "high",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "ANTHROPIC_API_KEY": "keep",
    })
    child = sv.worker_environment(source, "/tmp/worker")
    assert all(key not in child for key in IDENTITY_KEYS)
    assert child["CLAUDE_EFFORT"] == "high"
    assert child["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    assert child["ANTHROPIC_API_KEY"] == "keep"
    assert all(key in source for key in IDENTITY_KEYS)


def _redispatch_same_task(app, queue, db, task_id):
    """Re-dispatch ``task_id`` the way the capacity/transient paths do: the
    same row, dequeued again. The fake worker never POSTs a result, so the
    row is ``timed_out`` after each dispatch; roll it back to pending here."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE tasks SET status='pending', claim_source='' WHERE task_id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    task = queue.dequeue(role="implementer")
    assert task and task.task_id == task_id
    asyncio.run(app.state.dispatch_task(task, "implementer"))
    return queue.get_attribution(task_id)


def test_same_task_redispatch_after_model_change_resets_once(tmp_path, monkeypatch, unused_tcp_port):
    """review-2912af95: capacity/transient paths re-dispatch the SAME task_id.

    The prior attribution must be compared even when it belongs to this task,
    otherwise a model change between the two dispatches resumes the old
    conversation under the new model.
    """
    monkeypatch.delenv("AGENT_CREW_CLAUDE_MODEL", raising=False)
    app, queue, seen = _harness(tmp_path, monkeypatch, unused_tcp_port)
    db = str(tmp_path / "tasks.db")
    with TestClient(app):
        first = _dispatch(app, queue, "impl-813-same")
        assert first["context_policy"] == "fresh" and first["model"] == ""

        monkeypatch.setenv("AGENT_CREW_CLAUDE_MODEL", "claude-fable-5-1")
        changed = _redispatch_same_task(app, queue, db, "impl-813-same")
        # Same model again on a third re-dispatch: the reset happens once.
        same = _redispatch_same_task(app, queue, db, "impl-813-same")

    assert changed["model"] == "claude-fable-5-1"
    assert changed["context_policy"] == "fresh"
    assert changed["context_generation"] == first["context_generation"] + 1
    assert "--continue" not in seen[1][0]
    assert seen[1][0][seen[1][0].index("--model") + 1] == "claude-fable-5-1"

    assert same["model"] == "claude-fable-5-1"
    assert same["context_policy"] == "resume"
    assert same["context_generation"] == changed["context_generation"]
    assert "--continue" in seen[2][0]


def test_worktree_settings_pin_applies_on_resume_and_resets_after_change(
    tmp_path, monkeypatch, unused_tcp_port,
):
    monkeypatch.delenv("AGENT_CREW_CLAUDE_MODEL", raising=False)
    app, queue, seen = _harness(tmp_path, monkeypatch, unused_tcp_port)
    settings = tmp_path / "claude" / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "claude-fable-5-1"}))

    with TestClient(app):
        first = _dispatch(app, queue, "impl-813-settings-a")
        same = _dispatch(app, queue, "impl-813-settings-b")
        settings.write_text(json.dumps({"model": "claude-opus-5-1"}))
        changed = _dispatch(app, queue, "impl-813-settings-c")
        resumed = _dispatch(app, queue, "impl-813-settings-d")

    assert first["model"] == same["model"] == "claude-fable-5-1"
    assert same["context_policy"] == "resume"
    assert "--continue" in seen[1][0]
    assert seen[1][0][seen[1][0].index("--model") + 1] == "claude-fable-5-1"
    assert changed["model"] == resumed["model"] == "claude-opus-5-1"
    assert changed["context_policy"] == "fresh"
    assert changed["context_generation"] == same["context_generation"] + 1
    assert "--continue" not in seen[2][0]
    assert seen[2][0][seen[2][0].index("--model") + 1] == "claude-opus-5-1"
    assert resumed["context_policy"] == "resume"
    assert "--continue" in seen[3][0]
    assert seen[3][0][seen[3][0].index("--model") + 1] == "claude-opus-5-1"


def test_unpinned_transcript_model_does_not_reset_context(
    tmp_path, monkeypatch, unused_tcp_port,
):
    monkeypatch.delenv("AGENT_CREW_CLAUDE_MODEL", raising=False)
    app, queue, seen = _harness(tmp_path, monkeypatch, unused_tcp_port)
    with TestClient(app):
        first = _dispatch(app, queue, "impl-813-observed-a")
        queue.record_task_telemetry("impl-813-observed-a", TaskTelemetry(model="claude-opus-5-5"))
        assert queue.get_attribution("impl-813-observed-a")["model"] == "claude-opus-5-5"
        second = _dispatch(app, queue, "impl-813-observed-b")

    assert first["context_policy"] == "fresh"
    assert second["context_policy"] == "resume"
    assert second["context_generation"] == first["context_generation"]
    assert "--continue" in seen[1][0]
    assert "--model" not in seen[1][0]


def test_pinned_transcript_model_keeps_same_pin_on_resume(
    tmp_path, monkeypatch, unused_tcp_port,
):
    monkeypatch.setenv("AGENT_CREW_CLAUDE_MODEL", "claude-opus-5-5")
    app, queue, seen = _harness(tmp_path, monkeypatch, unused_tcp_port)
    with TestClient(app):
        first = _dispatch(app, queue, "impl-813-pinned-a")
        queue.record_task_telemetry("impl-813-pinned-a", TaskTelemetry(model="claude-opus-5-5"))
        assert queue.get_attribution("impl-813-pinned-a")["model"] == "claude-opus-5-5"
        second = _dispatch(app, queue, "impl-813-pinned-b")

    assert first["context_policy"] == "fresh"
    assert second["context_policy"] == "resume"
    assert second["context_generation"] == first["context_generation"]
    assert "--continue" in seen[1][0]
    assert seen[1][0][seen[1][0].index("--model") + 1] == "claude-opus-5-5"
