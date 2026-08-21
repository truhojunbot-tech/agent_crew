"""Regression/acceptance tests for issue #202 — durable context identity +
lifecycle telemetry.

Covers the acceptance criteria directly:
  - two sequential tasks sharing a resumed context share context_id and
    increasing session_task_index
  - a fresh/new context creates a new generation/context_id deterministically
    (task.context["context_reset"])
  - retry/fallback lineage is reconstructable from durable attribution rows
  - context/task lifecycle survives an Agent Crew restart (new create_app()
    instance against the same db_path)
  - Agent Crew works with no external consumer of context_events.jsonl
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.context_identity import detect_context_compaction, extract_claude_session_id
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _worktree_state(tmp_path):
    wt_claude = tmp_path / "claude"
    wt_codex = tmp_path / "codex"
    wt_gemini = tmp_path / "gemini"
    for wt in (wt_claude, wt_codex, wt_gemini):
        wt.mkdir()
        (wt / ".git").mkdir()
    state = {
        "worktrees": {
            "claude": str(wt_claude),
            "codex": str(wt_codex),
            "gemini": str(wt_gemini),
        }
    }
    return state, {"claude": wt_claude, "codex": wt_codex, "gemini": wt_gemini}


def _task_payload(task_id: str, task_type: str = "test", context: dict = None) -> dict:
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": f"Task {task_id}",
        "branch": "main",
        "priority": 3,
        "context": context or {},
        "project": "test_project",
    }


def _make_fake_subprocess(stdout_by_agent: dict = None):
    """Returns a fake asyncio.create_subprocess_exec that writes each
    agent's configured stdout text into the dispatch log, mirroring how the
    real dispatcher pipes subprocess output into log_f."""
    stdout_by_agent = stdout_by_agent or {}

    async def fake_subprocess(*args, **kwargs):
        cmd0 = str(args[0]) if args else ""
        if "gemini" in cmd0 or cmd0.endswith("/agy") or cmd0 == "agy":
            agent = "gemini"
        elif "codex" in cmd0:
            agent = "codex"
        else:
            agent = "claude"
        stdout = kwargs.get("stdout")
        text = stdout_by_agent.get(agent, "")
        if stdout is not None and text:
            stdout.write(text.encode())
            stdout.flush()
        proc = MagicMock()
        proc.returncode = 1  # no result submitted by the fake agent — exercises the failure path
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=1)
        proc.pid = 999999
        return proc

    return fake_subprocess


def _attribution_row(tmp_db, task_id):
    return TaskQueue(tmp_db).get_attribution(task_id)


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# DB-level: TaskQueue.get_or_create_context
# ---------------------------------------------------------------------------

def test_u_i202_get_or_create_context_first_call_is_fresh(tmp_db):
    q = TaskQueue(tmp_db)
    info = q.get_or_create_context(project="p", agent="claude", worktree_path="/wt/claude", role="implementer", task_id="t1")
    assert info["context_policy"] == "fresh"
    assert info["context_generation"] == 1
    assert info["session_task_index"] == 1
    assert info["previous_task_id"] is None


def test_u_i202_get_or_create_context_second_call_resumes(tmp_db):
    q = TaskQueue(tmp_db)
    first = q.get_or_create_context(project="p", agent="claude", worktree_path="/wt/claude", role="implementer", task_id="t1")
    second = q.get_or_create_context(project="p", agent="claude", worktree_path="/wt/claude", role="implementer", task_id="t2")
    assert second["context_policy"] == "resume"
    assert second["context_id"] == first["context_id"]
    assert second["context_generation"] == first["context_generation"]
    assert second["session_task_index"] == 2
    assert second["previous_task_id"] == "t1"


def test_u_i202_force_reset_bumps_generation_and_mints_new_context_id(tmp_db):
    q = TaskQueue(tmp_db)
    first = q.get_or_create_context(project="p", agent="claude", worktree_path="/wt/claude", role="implementer", task_id="t1")
    second = q.get_or_create_context(project="p", agent="claude", worktree_path="/wt/claude", role="implementer", task_id="t2")
    third = q.get_or_create_context(
        project="p", agent="claude", worktree_path="/wt/claude", role="implementer",
        task_id="t3", force_reset=True,
    )
    assert third["context_policy"] == "fresh"
    assert third["context_id"] != first["context_id"]
    assert third["context_generation"] == second["context_generation"] + 1
    assert third["session_task_index"] == 1
    assert third["previous_task_id"] == "t2"  # boundary carries the last task of the old generation


def test_u_i202_different_worktree_is_a_different_context_even_same_role(tmp_db):
    """Agent != Role != Context: an agent_override into a different agent's
    worktree must NOT share context with that role's normal worktree."""
    q = TaskQueue(tmp_db)
    reviewer_default = q.get_or_create_context(
        project="p", agent="codex", worktree_path="/wt/codex", role="reviewer", task_id="t1"
    )
    reviewer_overridden_to_gemini = q.get_or_create_context(
        project="p", agent="gemini", worktree_path="/wt/gemini", role="reviewer", task_id="t2"
    )
    assert reviewer_overridden_to_gemini["context_id"] != reviewer_default["context_id"]


def test_u_i202_same_worktree_shared_across_roles(tmp_db):
    """The flip side: if two different roles' tasks land in the SAME
    (project, agent, worktree) — e.g. via override — they share one
    context, because that's what actually happens at the provider CLI
    level (same cwd, same --continue conversation)."""
    q = TaskQueue(tmp_db)
    tester_task = q.get_or_create_context(
        project="p", agent="gemini", worktree_path="/wt/gemini", role="tester", task_id="t1"
    )
    reviewer_override_task = q.get_or_create_context(
        project="p", agent="gemini", worktree_path="/wt/gemini", role="reviewer", task_id="t2"
    )
    assert reviewer_override_task["context_id"] == tester_task["context_id"]
    assert reviewer_override_task["session_task_index"] == 2


# ---------------------------------------------------------------------------
# Log-parsing heuristics
# ---------------------------------------------------------------------------

def test_u_i202_extract_claude_session_id_found():
    log = '{"type":"system","subtype":"init","session_id":"abcd1234-ef56-7890-abcd-1234567890ab"}\n'
    assert extract_claude_session_id(log) == "abcd1234-ef56-7890-abcd-1234567890ab"


def test_u_i202_extract_claude_session_id_missing():
    assert extract_claude_session_id("no session info here\n") is None
    assert extract_claude_session_id("") is None


def test_u_i202_detect_context_compaction_true():
    assert detect_context_compaction("Notice: conversation compacted to fit context window") is True


def test_u_i202_detect_context_compaction_false():
    assert detect_context_compaction("all good, tests passed") is False


# ---------------------------------------------------------------------------
# Full-dispatch acceptance tests
# ---------------------------------------------------------------------------

def test_u_i202_two_sequential_tasks_share_context_and_increment_index(tmp_db, tmp_path):
    """Acceptance criterion: two sequential tasks using the same resumed
    provider conversation share the same context_id and have increasing
    session_task_index."""
    state, _ = _worktree_state(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    fake_subprocess = _make_fake_subprocess()

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "0",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app) as client:
                    resp = client.post("/tasks", json=_task_payload("test-1"))
                    assert resp.status_code == 201
                    time.sleep(0.5)
                    resp = client.post("/tasks", json=_task_payload("test-2"))
                    assert resp.status_code == 201
                    time.sleep(0.5)

    row1 = _attribution_row(tmp_db, "test-1")
    row2 = _attribution_row(tmp_db, "test-2")
    assert row1 and row2, "both tasks should have attribution rows"
    assert row1["context_id"] == row2["context_id"]
    assert row1["context_policy"] == "fresh"
    assert row2["context_policy"] == "resume"
    assert row2["session_task_index"] > row1["session_task_index"]
    assert row2["previous_task_id"] == "test-1"


def test_u_i202_context_reset_flag_creates_new_generation(tmp_db, tmp_path):
    """Acceptance criterion: a fresh/new provider conversation creates a new
    generation/context identity in a deterministic, observable way."""
    state, _ = _worktree_state(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    fake_subprocess = _make_fake_subprocess()

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "0",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app) as client:
                    client.post("/tasks", json=_task_payload("test-a"))
                    time.sleep(0.5)
                    client.post("/tasks", json=_task_payload("test-b", context={"context_reset": True}))
                    time.sleep(0.5)

    row_a = _attribution_row(tmp_db, "test-a")
    row_b = _attribution_row(tmp_db, "test-b")
    assert row_b["context_policy"] == "fresh"
    assert row_b["context_id"] != row_a["context_id"]
    assert row_b["context_generation"] == row_a["context_generation"] + 1

    events = _read_jsonl(os.path.join(tmp_path.__str__(), "context_events.jsonl"))
    reset_events = [e for e in events if e["event_type"] == "context_reset" and e.get("task_id") == "test-b"]
    assert reset_events, f"expected a context_reset event for test-b, got event types {[e['event_type'] for e in events]}"


def test_u_i202_provider_fallback_event_and_retry_lineage(tmp_db, tmp_path):
    """Acceptance criterion: retry and provider-fallback lineage can be
    reconstructed from durable records."""
    state, _ = _worktree_state(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    fake_subprocess = _make_fake_subprocess()

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "0",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app) as client:
                    # review role's default agent is codex; override to gemini
                    # is a provider_fallback relative to that default.
                    resp = client.post("/tasks", json=_task_payload(
                        "review-1", task_type="review", context={"agent_override": "gemini"},
                    ))
                    assert resp.status_code == 201
                    time.sleep(0.5)

    row = _attribution_row(tmp_db, "review-1")
    assert row["agent"] == "gemini"

    events = _read_jsonl(os.path.join(tmp_path.__str__(), "context_events.jsonl"))
    fallback_events = [e for e in events if e["event_type"] == "provider_fallback" and e.get("task_id") == "review-1"]
    assert fallback_events, f"expected a provider_fallback event, got {[e['event_type'] for e in events]}"
    assert fallback_events[0]["from_agent"] == "codex"
    assert fallback_events[0]["to_agent"] == "gemini"

    # retry_of / fallback_of lineage: submitted directly against attribution
    # since it's populated from task.context at dispatch time regardless of
    # how the follow-up task was created.
    q = TaskQueue(tmp_db)
    from agent_crew.protocol import TaskRequest
    q.enqueue(TaskRequest(
        task_id="retry-review-1-ab12", task_type="review", description="retry",
        context={"retry_attempt": 1, "original_task_id": "review-1"},
    ))
    q.enqueue(TaskRequest(
        task_id="fallback-review-1-cd34", task_type="review", description="fallback",
        context={"fallback_from_task_id": "review-1", "original_task_id": "review-1"},
    ))
    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "0",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app2 = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app2):
                    time.sleep(0.6)

    retry_row = _attribution_row(tmp_db, "retry-review-1-ab12")
    fallback_row = _attribution_row(tmp_db, "fallback-review-1-cd34")
    assert retry_row["retry_of"] == "review-1"
    assert fallback_row["fallback_of"] == "review-1"


def test_u_i202_context_survives_restart(tmp_db, tmp_path):
    """Acceptance criterion: context/task lifecycle survives Agent Crew
    restart because the relevant metadata is persisted."""
    state, _ = _worktree_state(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    fake_subprocess = _make_fake_subprocess()
    env = {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "0",
    }

    with patch.dict(os.environ, env):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app1 = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app1) as client:
                    client.post("/tasks", json=_task_payload("before-restart"))
                    time.sleep(0.5)

    # Simulate a server restart: brand new create_app() (fresh in-process
    # _seen_context_keys_this_process set), same db_path/state_path.
    with patch.dict(os.environ, env):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app2 = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app2) as client:
                    client.post("/tasks", json=_task_payload("after-restart"))
                    time.sleep(0.5)

    row_before = _attribution_row(tmp_db, "before-restart")
    row_after = _attribution_row(tmp_db, "after-restart")
    assert row_before and row_after
    assert row_before["context_id"] == row_after["context_id"], (
        "context identity must survive an Agent Crew restart"
    )
    assert row_after["session_task_index"] > row_before["session_task_index"]

    events = _read_jsonl(os.path.join(tmp_path.__str__(), "context_events.jsonl"))
    recovered = [e for e in events if e["event_type"] == "context_recovered" and e.get("task_id") == "after-restart"]
    assert recovered, (
        f"expected a context_recovered event on the first dispatch of the new "
        f"process, got event types {[e['event_type'] for e in events]}"
    )


def test_u_i202_works_with_no_external_consumer(tmp_db, tmp_path):
    """Acceptance criterion: existing workflows continue to work when no
    external analytics consumer exists — this just means dispatch must not
    fail/raise because of any of the new telemetry code."""
    state, _ = _worktree_state(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    fake_subprocess = _make_fake_subprocess()

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "0",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(
                    db_path=tmp_db, pane_map={}, port=0, state_path=str(state_file),
                    watchdog_disabled=True, anomaly_disabled=True,
                )
                with TestClient(app) as client:
                    resp = client.post("/tasks", json=_task_payload("plain-1"))
                    assert resp.status_code == 201
                    time.sleep(0.5)

    tasks = TaskQueue(tmp_db).list_tasks()
    assert any(t.task_id == "plain-1" for t in tasks)
