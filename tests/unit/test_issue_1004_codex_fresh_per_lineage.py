"""#1004: codex resumes only inside a task lineage.

The context is keyed on project::agent::worktree, so a reviewer pane with one
codex worktree resumed ONE provider session across every review task for days
(~205K context re-sent per turn). A task of another lineage now starts fresh;
fix rounds of the same lineage keep resuming.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

SESSION = "11111111-2222-3333-4444-555555555555"


def _dispatch(tmp_path, monkeypatch, port, *, agent, second, first=None,
              lineage_error=False):
    """Dispatch ``first`` (if given) then ``second`` into one ``agent`` worktree.

    Returns (last argv, context identity, context events for ``second``)."""
    worktree = tmp_path / agent
    worktree.mkdir()
    (worktree / ".git").mkdir()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "worktrees": {agent: str(worktree)},
        "role_agents": {"implementer": agent, "reviewer": agent, "tester": "script"},
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
    # (4) compares against claude's worktree scope: its #713 lineage reset is
    # a separate, pre-existing policy this change must not alter either way.
    monkeypatch.setenv("AGENT_CREW_CLAUDE_CONTEXT_SCOPE", "worktree")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "_resolve_pr_head_branch", lambda *_a, **_kw: "agent/1004")
    monkeypatch.setattr(server, "_prepare_worktree_for_task", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_stash_dirty_worktree", lambda *_a, **_kw: "")
    monkeypatch.setattr(server, "codex_session_for_cwd", lambda *_a, **_kw: SESSION)
    monkeypatch.setattr(server, "codex_context_exceeds_cap",
                        lambda *_a, **_kw: (False, {"provider": "codex", "bytes": 0}))
    monkeypatch.setattr(server, "claude_context_exceeds_cap",
                        lambda *_a, **_kw: (False, {"provider": "claude", "bytes": 0}))

    app = server.create_app(db_path=db_path, state_path=str(state_path), pane_map={},
                            port=port, project="p", watchdog_disabled=True,
                            anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db_path)
        for task, role in ([first] if first else []) + [second]:
            queue.enqueue(task)
            if lineage_error and task is second[0]:
                def _fail(*_a, **_kw):
                    raise RuntimeError("lineage lookup failed")
                monkeypatch.setattr(TaskQueue, "task_lineage_root_id", _fail)
            asyncio.run(app.state.dispatch_task(queue.dequeue(role=role), role))
        identity = queue.peek_context_identity("p", agent, str(worktree))

    event_path = tmp_path / "context_events.jsonl"
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    return spawned[-1], identity, [e for e in events
                                   if e.get("task_id") == second[0].task_id]


def _review(task_id, impl_id):
    return TaskRequest(
        task_id=task_id, task_type="review", description="review",
        branch="agent/1004", project="p",
        context={"prev_task_id": impl_id, "lineage_root_task_id": impl_id,
                 "risk_tier": 1},
    ), "reviewer"


def _fix(task_id, review_id, impl_id):
    return TaskRequest(
        task_id=task_id, task_type="implement", description="fix",
        branch="agent/1004", project="p",
        context={"prev_task_id": review_id, "lineage_root_task_id": impl_id,
                 "fix_round": 1, "risk_tier": 1},
    ), "implementer"


def test_review_of_another_lineage_starts_fresh(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch(
        tmp_path, monkeypatch, unused_tcp_port, agent="codex",
        first=_review("review-a", "impl-a"), second=_review("review-b", "impl-b"))
    assert cmd[:2] == ["codex", "exec"]
    assert "resume" not in cmd and SESSION not in cmd
    assert identity["context_generation"] == 2
    # (5) recorded like the cap reset, with the lineage cause.
    assert any(e["event_type"] == "context_reset" and e.get("tripped_by") == "lineage"
               for e in events)


def test_fix_round_of_the_same_lineage_resumes(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch(
        tmp_path, monkeypatch, unused_tcp_port, agent="codex",
        first=_review("review-a", "impl-a"),
        second=_fix("fix-review-a-r1", "review-a", "impl-a"))
    assert cmd[:4] == ["codex", "exec", "resume", SESSION]
    assert identity["context_generation"] == 1
    assert not any(e["event_type"] == "context_reset" for e in events)


def test_lineage_lookup_failure_fails_closed_to_fresh(tmp_path, monkeypatch,
                                                      unused_tcp_port, caplog):
    """An unverifiable lineage must not authorize reusing another task's session
    (review-impl-1004a-r0): even a same-lineage fix round starts fresh."""
    cmd, identity, events = _dispatch(
        tmp_path, monkeypatch, unused_tcp_port, agent="codex",
        first=_review("review-a", "impl-a"),
        second=_fix("fix-review-a-r1", "review-a", "impl-a"), lineage_error=True)
    assert cmd[:2] == ["codex", "exec"]
    assert "resume" not in cmd and SESSION not in cmd
    assert identity["context_generation"] == 2
    assert any(e["event_type"] == "context_reset" for e in events)
    assert "lineage lookup failed" in caplog.text


def test_first_task_in_a_worktree_is_fresh(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch(
        tmp_path, monkeypatch, unused_tcp_port, agent="codex",
        second=_review("review-a", "impl-a"))
    assert cmd[:2] == ["codex", "exec"] and "resume" not in cmd
    assert identity["context_generation"] == 1
    assert any(e["event_type"] == "context_created" for e in events)


def test_claude_is_unchanged(tmp_path, monkeypatch, unused_tcp_port):
    cmd, identity, events = _dispatch(
        tmp_path, monkeypatch, unused_tcp_port, agent="claude",
        first=_review("review-a", "impl-a"), second=_review("review-b", "impl-b"))
    assert cmd[0] == "claude" and "--continue" in cmd
    assert identity["context_generation"] == 1
    assert not any(e["event_type"] == "context_reset" for e in events)


def test_lineage_root_falls_back_to_the_task_itself(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                              description="d", branch="b", project="p", context={}))
    queue.enqueue(_review("review-a", "impl-a")[0])
    assert queue.task_lineage_root_id("impl-a") == "impl-a"
    assert queue.task_lineage_root_id("review-a") == "impl-a"
    assert queue.task_lineage_root_id("gone") == "gone"
