"""#713 part A: Claude resumes only inside the task lineage by default."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _dispatch_pair(tmp_path, monkeypatch, unused_tcp_port, *, related=False,
                   scope=None, lineage_error=False, cap_overlap=False):
    worktree = tmp_path / "claude"
    worktree.mkdir()
    (worktree / ".git").mkdir()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "worktrees": {"claude": str(worktree)},
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": "gemini"},
    }))
    db_path = str(tmp_path / "tasks.db")
    spawned = []

    async def fake_exec(*cmd, **_kwargs):
        spawned.append(list(cmd))

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
    if scope is None:
        monkeypatch.delenv("AGENT_CREW_CLAUDE_CONTEXT_SCOPE", raising=False)
    else:
        monkeypatch.setenv("AGENT_CREW_CLAUDE_CONTEXT_SCOPE", scope)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "_resolve_pr_head_branch", lambda *_a, **_kw: "agent/713")
    monkeypatch.setattr(server, "_prepare_worktree_for_task", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_stash_dirty_worktree", lambda *_a, **_kw: "")
    cap_checks = 0

    def cap_check(*_args, **_kwargs):
        nonlocal cap_checks
        cap_checks += 1
        if cap_overlap and cap_checks == 2:
            return True, {"provider": "claude", "bytes": 0, "cap_mb": 64,
                          "context_tokens": 100, "cap_tokens": 50,
                          "tripped_by": "tokens"}
        return False, {"provider": "claude", "bytes": 0}

    monkeypatch.setattr(server, "claude_context_exceeds_cap", cap_check)

    app = server.create_app(db_path=db_path, state_path=str(state_path), pane_map={},
                            port=unused_tcp_port, project="p", watchdog_disabled=True,
                            anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db_path)
        seed = TaskRequest(
            task_id="review-seed", task_type="review", description="first review",
            branch="agent/713", project="p",
            context={"pr_number": 713, "risk_tier": 1},
        )
        queue.enqueue(seed)
        asyncio.run(app.state.dispatch_task(queue.dequeue(role="reviewer"), "reviewer"))
        if related:
            queue.enqueue(TaskRequest(
                task_id="fix-review-seed-r1", task_type="implement", description="fix",
                branch="agent/713", project="p",
                context={"prev_task_id": "review-seed", "pr_number": 713,
                         "fix_round": 1, "risk_tier": 1},
            ))
            queue.submit_result("fix-review-seed-r1", TaskResult(
                task_id="fix-review-seed-r1", status="completed", summary="fixed",
                pr_number=713,
            ))
        target_context = {"pr_number": 713 if related else 714, "risk_tier": 1}
        if related:
            target_context["prev_task_id"] = "fix-review-seed-r1"
        queue.enqueue(TaskRequest(
            task_id="review-next", task_type="review", description="second review",
            branch="agent/713", project="p", context=target_context,
        ))
        if lineage_error:
            monkeypatch.setattr(server, "task_lineage",
                                lambda *_a: (_ for _ in ()).throw(RuntimeError("lookup failed")))
        asyncio.run(app.state.dispatch_task(queue.dequeue(role="reviewer"), "reviewer"))
        identity = queue.peek_context_identity("p", "claude", str(worktree))

    event_path = tmp_path / "context_events.jsonl"
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    return spawned[-1], identity, [e for e in events if e.get("task_id") == "review-next"]


def test_unrelated_previous_task_starts_fresh(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch_pair(tmp_path, monkeypatch, unused_tcp_port)
    assert cmd[0] == "claude" and "--continue" not in cmd
    assert identity["context_generation"] == 2
    assert any(e["event_type"] == "context_reset" and e.get("tripped_by") == "lineage"
               for e in events)


def test_cap_and_lineage_overlap_preserves_cap_cause(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch_pair(tmp_path, monkeypatch, unused_tcp_port,
                                          cap_overlap=True)
    assert "--continue" not in cmd
    assert identity["context_generation"] == 2
    assert any(e["event_type"] == "context_reset" and e.get("tripped_by") == "lineage"
               for e in events)
    assert any(e["event_type"] == "provider_context_capped"
               and e.get("tripped_by") == "tokens" for e in events)


def test_review_after_fix_in_same_lineage_resumes(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch_pair(tmp_path, monkeypatch, unused_tcp_port, related=True)
    assert "--continue" in cmd
    assert identity["context_generation"] == 1
    assert not any(e["event_type"] == "context_reset" for e in events)


def test_worktree_scope_keeps_existing_resume(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, _ = _dispatch_pair(tmp_path, monkeypatch, unused_tcp_port,
                                      scope="worktree")
    assert "--continue" in cmd
    assert identity["context_generation"] == 1


def test_lineage_lookup_error_keeps_resume(tmp_path, monkeypatch, unused_tcp_port, caplog):
    cmd, identity, _ = _dispatch_pair(tmp_path, monkeypatch, unused_tcp_port,
                                      lineage_error=True)
    assert "--continue" in cmd
    assert identity["context_generation"] == 1
    assert "lookup failed" in caplog.text


def test_last_task_peek_is_read_only(tmp_db):
    queue = TaskQueue(tmp_db)
    assert queue.peek_context_last_task_id("p", "claude", "/wt") == ""
    first = queue.get_or_create_context("p", "claude", "/wt", task_id="first")
    assert queue.peek_context_last_task_id("p", "claude", "/wt") == "first"
    assert queue.peek_context_identity("p", "claude", "/wt")["context_generation"] == 1
    second = queue.get_or_create_context("p", "claude", "/wt", task_id="second")
    assert second["session_task_index"] == first["session_task_index"] + 1
