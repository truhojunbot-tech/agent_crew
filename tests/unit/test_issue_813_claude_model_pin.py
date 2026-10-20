"""The Claude model pin follows context identity and stays out of server env."""

import asyncio
import json

from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


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
    assert "--continue" not in seen[4][0]
    assert unpinned["context_policy"] == "fresh"


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
