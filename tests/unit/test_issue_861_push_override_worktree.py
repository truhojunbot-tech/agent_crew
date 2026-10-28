"""#861: the push path prepared the ROLE's worktree for an `agent_override` task.

apify-forge, 2026-10-27: the role map was `implementer→claude, reviewer→codex`
and both live tasks carried overrides (review→claude, implement→codex). At
11:23:11 the dispatcher pinned the claude worktree at the review's
`reviewed_sha`; at 11:23:34 `_try_push_next` dequeued `impl-543efd2a`
(override codex, pane free) and prepared `worktree_map["implementer"]` — the
CLAUDE worktree — checking it out at master's tip and writing `AGENTS.md` into
it, 23 s into the running review. The pane-busy check looked at the codex pane,
so nothing saw that the worktree it was about to move belonged to another one.
"""
from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew.queue import TaskQueue
from agent_crew.protocol import TaskRequest

ROLE_TO_AGENT = {"implementer": "claude", "reviewer": "codex", "tester": "gemini"}
SHA = "7f79f5f3b21773c772245c4ae08fec8462da70d2"


def _payload(task_id, task_type, override):
    return {
        "task_id": task_id, "task_type": task_type, "description": "work",
        "branch": "fix/99-x", "priority": 3, "project": "",
        "context": {"risk_tier": 1, "agent_override": override},
    }


def _app(tmp_db, tmp_path, monkeypatch, push_calls, prepared, protocols,
         role_to_agent=ROLE_TO_AGENT):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setattr(server, "_WORKTREE_SYNC_DISABLED", False)
    monkeypatch.setattr(server, "_load_role_to_agent", lambda _p: dict(role_to_agent))

    def fake_prepare(wt, task_id, branch, role, task_context=None, task_type=""):
        prepared.append((task_id, wt))
        return SHA

    def fake_protocol(role, wt, *a, **k):
        protocols.append((role, wt))
        return True

    monkeypatch.setattr(server, "_prepare_worktree_for_task", fake_prepare)
    monkeypatch.setattr(server, "_ensure_role_protocol", fake_protocol)
    monkeypatch.setattr(server, "_restore_task_wip", lambda *a, **k: "")
    wts = {agent: str(tmp_path / agent) for agent in ROLE_TO_AGENT.values()}
    worktree_map = {role: wts[agent] for role, agent in role_to_agent.items() if agent in wts}
    app = server.create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100", "reviewer": "%101", "tester": "%102",
                  "claude": "%100", "codex": "%101", "gemini": "%102"},
        worktree_map=worktree_map, port=8100,
        push_fn=lambda pane, text: push_calls.append((pane, text)),
        watchdog_disabled=True, anomaly_disabled=True,
    )
    return app, wts


def test_override_task_is_prepared_in_the_override_agents_worktree(tmp_db, tmp_path, monkeypatch):
    # A review is running in the claude worktree (override claude).
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(**_payload("review-running", "review", "claude")))
    assert queue.dequeue(role="reviewer").task_id == "review-running"

    push_calls, prepared, protocols = [], [], []
    app, wts = _app(tmp_db, tmp_path, monkeypatch, push_calls, prepared, protocols)
    with TestClient(app) as client:
        assert client.post("/tasks", json=_payload("impl-543efd2a", "implement", "codex")).status_code == 201

    touched = [wt for _, wt in prepared] + [wt for _, wt in protocols]
    assert wts["claude"] not in touched, (
        "the implement task (override codex) moved the claude worktree a running review is pinned to")
    assert prepared == [("impl-543efd2a", wts["codex"])]
    assert protocols == [("implementer", wts["codex"])]
    assert push_calls and push_calls[0][0] == "%101"


def test_task_without_override_still_uses_its_role_worktree(tmp_db, tmp_path, monkeypatch):
    push_calls, prepared, protocols = [], [], []
    app, wts = _app(tmp_db, tmp_path, monkeypatch, push_calls, prepared, protocols)
    payload = _payload("impl-plain", "implement", "")
    payload["context"].pop("agent_override")
    with TestClient(app) as client:
        assert client.post("/tasks", json=payload).status_code == 201

    assert prepared == [("impl-plain", wts["claude"])]
    assert protocols == [("implementer", wts["claude"])]
    assert push_calls and push_calls[0][0] == "%100"


def test_override_agent_without_a_role_worktree_is_not_prepared_in_another_agents_tree(
        tmp_db, tmp_path, monkeypatch):
    # Review of PR #880: gemini has a pane but no role (tester is the script
    # tester), so no worktree is gemini's. The resolver then falls back to
    # `worktree_map["implementer"]` — claude's — while the push goes to the
    # gemini pane. Refuse before prep instead of guessing a worktree.
    role_to_agent = {"implementer": "claude", "reviewer": "codex", "tester": "script"}
    push_calls, prepared, protocols = [], [], []
    app, wts = _app(tmp_db, tmp_path, monkeypatch, push_calls, prepared, protocols,
                    role_to_agent=role_to_agent)
    with TestClient(app) as client:
        assert client.post("/tasks", json=_payload("impl-gemini", "implement", "gemini")).status_code == 201
        task = client.get("/tasks/impl-gemini").json()

    assert prepared == [] and protocols == [], (
        f"a gemini override touched another agent's worktree: {prepared + protocols}")
    assert push_calls == []
    assert task["status"] == "pending"
