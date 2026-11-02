"""Agy dispatch creates a fresh conversation for every task (#982)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


pytestmark = pytest.mark.usefixtures("legacy_gemini_tester_policy")


def test_agy_conversation_lookup_includes_wal(tmp_path):
    home = tmp_path / ".gemini"
    cache = home / "antigravity-cli" / "cache"
    store = home / "antigravity-cli" / "conversations"
    cache.mkdir(parents=True)
    store.mkdir(parents=True)
    (cache / "last_conversations.json").write_text(json.dumps({"/wt/gemini": "conv-1"}))
    (store / "conv-1.db").write_bytes(b"abc")
    (store / "conv-1.db-wal").write_bytes(b"de")
    assert server.agy_conversation_size("/wt/gemini", home=home) == (5, "conv-1")
    assert server.agy_conversation_size("/wt/other", home=home) == (0, "")


def test_agy_conversation_lookup_without_store_is_empty(tmp_path):
    assert server.agy_conversation_size("/wt/gemini", home=tmp_path) == (0, "")


@pytest.mark.parametrize("agent", ["gemini", "agy"])
def test_two_agy_dispatches_start_fresh_and_bind_distinct_conversations(
        tmp_path, monkeypatch, unused_tcp_port, agent):
    wt = tmp_path / "gemini"
    wt.mkdir()
    (wt / ".git").mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": agent},
        "worktrees": {agent: str(wt)},
    }))
    agy_home = tmp_path / ".gemini"
    mapping = agy_home / "antigravity-cli" / "cache" / "last_conversations.json"
    mapping.parent.mkdir(parents=True)
    monkeypatch.setattr(server, "_agy_home", lambda home=None: agy_home)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    commands = []

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        mapping.write_text(json.dumps({str(wt): f"conv-{len(commands)}"}))

        class Process:
            pid = 1
            returncode = 0

            async def communicate(self):
                return b"", b""

            async def wait(self):
                return 0

        return Process()

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    db = str(tmp_path / "tasks.db")
    app = server.create_app(db_path=db, state_path=str(state), pane_map={},
                            port=unused_tcp_port, watchdog_disabled=True,
                            anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        identities = []
        for number in (1, 2):
            task_id = f"test-{number}"
            queue.enqueue(TaskRequest(task_id=task_id, task_type="test",
                                      branch="main", description="run", context={}))
            task = queue.dequeue(role="tester")
            assert task is not None
            asyncio.run(app.state.dispatch_task(task, "tester"))
            identities.append(queue.get_attribution(task_id))

    assert len(commands) == 2
    assert all(command[0] == "agy" and "--continue" not in command
               and "--resume" not in command for command in commands)
    assert identities[0]["context_id"] != identities[1]["context_id"]
    assert [item["provider_session_id"] for item in identities] == ["conv-1", "conv-2"]
    assert [item["context_policy"] for item in identities] == ["fresh", "fresh"]
