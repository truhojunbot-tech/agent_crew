"""G_DT DELIVERY_TOPOLOGY_HAZARD: tmux push only into a pane running an agent CLI.

#373 refuses unknown tasks and foreign panes. It does not refuse an *owned*
pane whose process is `crew-log-viewer` — which is what every owned pane runs
under the dispatcher, while AGENT_CREW_DELIVERY still defaults to `both`.
"""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import _pane_process_kind, create_app


# ---------------------------------------------------------------------------
# The probe: process trees as `ps -e -o pid=,ppid=,args=` reports them. The
# command lines are the ones observed on this host's live panes.
# ---------------------------------------------------------------------------

LOG_VIEWER_TREE = (
    "100 1 bash -c while true; do crew-log-viewer /x/dispatch_implementer.log; sleep 1; done\n"
    "101 100 /home/u/.pyenv/versions/3.12.9/bin/python "
    "/home/u/.pyenv/versions/3.12.9/bin/crew-log-viewer /x/dispatch_implementer.log\n"
)
CLAUDE_NATIVE_TREE = (
    "100 1 /home/u/.local/share/claude/versions/2.1.185 --dangerously-skip-permissions\n"
    "101 100 node /home/u/alfred/node_modules/context-mode/start.mjs\n"
)
CLAUDE_WRAPPER_TREE = (
    "100 1 python3 /home/u/.local/bin/claude --dangerously-skip-permissions\n"
    "101 100 /home/u/.local/share/claude/versions/2.1.280 --dangerously-skip-permissions\n"
)
CODEX_TREE = (
    "100 1 -bash\n"
    "101 100 node /home/u/.local/bin/codex --dangerously-bypass-approvals-and-sandbox\n"
)
GEMINI_TREE = "100 1 -bash\n101 100 node /usr/lib/node_modules/@google/gemini-cli/bin/gemini\n"
SHELL_TREE = "100 1 -bash\n"
MODULE_VIEWER_TREE = "100 1 python3 -m agent_crew.log_viewer /x/dispatch.log\n"
UNRELATED_TREE = "100 1 -bash\n101 100 vim notes.txt\n"


def _fake_run(current_command, tree, *, tmux_rc=0, ps_rc=0, pane_pid="100"):
    def run(argv, *args, **kwargs):
        if argv[0] == "tmux":
            return SimpleNamespace(returncode=tmux_rc, stdout=f"{pane_pid}\t{current_command}\n")
        if argv[0] == "ps":
            return SimpleNamespace(returncode=ps_rc, stdout=tree)
        raise AssertionError(f"unexpected subprocess: {argv}")
    return run


@pytest.mark.real_pane_process_kind
@pytest.mark.parametrize(("current", "tree", "verdict"), [
    ("python", LOG_VIEWER_TREE, "log_viewer"),
    ("python3", MODULE_VIEWER_TREE, "log_viewer"),
    ("2.1.185", CLAUDE_NATIVE_TREE, "agent"),
    # `python3` is the claude quota wrapper here, not a log viewer: the
    # foreground name alone cannot tell them apart.
    ("python3", CLAUDE_WRAPPER_TREE, "agent"),
    ("node", CODEX_TREE, "agent"),
    ("node", GEMINI_TREE, "agent"),
    ("bash", SHELL_TREE, "shell"),
    ("vim", UNRELATED_TREE, "unknown"),
])
def test_probe_classifies_the_pane_process_tree(monkeypatch, current, tree, verdict):
    monkeypatch.setattr("agent_crew.server.subprocess.run", _fake_run(current, tree))
    kind, detail = _pane_process_kind("%101")
    assert kind == verdict
    assert f"current_command={current}" in detail


@pytest.mark.real_pane_process_kind
def test_log_viewer_wins_over_an_agent_in_the_same_tree(monkeypatch):
    """Ambiguity refuses: a viewer in the tree means the block may land in it."""
    tree = CODEX_TREE + "102 100 crew-log-viewer /x/dispatch.log\n"
    monkeypatch.setattr("agent_crew.server.subprocess.run", _fake_run("node", tree))
    assert _pane_process_kind("%101")[0] == "log_viewer"


@pytest.mark.real_pane_process_kind
@pytest.mark.parametrize(("kwargs", "detail"), [
    ({"tmux_rc": 1}, "tmux_probe_failed"),
    ({"pane_pid": ""}, "pane_pid_unavailable"),
    ({"ps_rc": 1}, "ps_failed"),
    ({"pane_pid": "999"}, "pane_pid_999_not_running"),
])
def test_probe_fails_closed_when_it_cannot_see_the_pane(monkeypatch, kwargs, detail):
    monkeypatch.setattr("agent_crew.server.subprocess.run",
                        _fake_run("claude", CLAUDE_NATIVE_TREE, **kwargs))
    assert _pane_process_kind("%101") == ("unknown", detail)


@pytest.mark.real_pane_process_kind
def test_probe_fails_closed_when_tmux_raises(monkeypatch):
    def boom(*_a, **_k):
        raise TimeoutError
    monkeypatch.setattr("agent_crew.server.subprocess.run", boom)
    assert _pane_process_kind("%101") == ("unknown", "tmux_probe_error:TimeoutError")


# ---------------------------------------------------------------------------
# The guard at the push boundary.
# ---------------------------------------------------------------------------

class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, target, message):
        self.calls.append((target, message))


@pytest.fixture
def project_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "owned", "pane_ids": ["%101"]}))
    return path


def _payload(task_id="gdt"):
    return {
        "task_id": task_id, "task_type": "implement", "description": "work",
        "branch": "main", "priority": 3, "context": {}, "project": "owned",
    }


def _status(db, task_id):
    return next(r["status"] for r in TaskQueue(db).list_all_with_status() if r["task_id"] == task_id)


def _app(db, state, push):
    return create_app(db, pane_map={"implementer": "%101"}, state_path=str(state),
                      port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)


@pytest.mark.parametrize(("verdict", "reason"), [
    ("log_viewer", "pane_not_agent_log_viewer"),
    ("shell", "pane_not_agent_shell"),
    ("unknown", "pane_not_agent_unknown"),
])
def test_push_into_owned_non_agent_pane_is_refused_logged_and_counted(
    tmp_db, project_state, monkeypatch, caplog, verdict, reason,
):
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda _pane: (verdict, "current_command=python"))
    push = RecordingPush()
    app = _app(tmp_db, project_state, push)

    with TestClient(app) as client:
        assert client.post("/tasks", json=_payload()).status_code == 201
        health = client.get("/health").json()

    assert push.calls == []
    # Requeued, not failed: under the dispatcher the task is still deliverable.
    assert _status(tmp_db, "gdt") == "pending"
    assert f"resolved=%101 reason={reason}" in caplog.text
    assert "detail=current_command=python refusals=1" in caplog.text
    assert app.state.delivery_guard_refusals == {reason: 1}
    assert health["delivery_guard"]["refusals"] == {reason: 1}
    assert health["delivery_guard"]["delivery"] == "both"


def test_refusals_accumulate_per_reason(tmp_db, project_state, monkeypatch):
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda _pane: ("log_viewer", "current_command=python"))
    app = _app(tmp_db, project_state, RecordingPush())
    TaskQueue(tmp_db).enqueue(TaskRequest(task_id="twice", task_type="implement", description="w"))
    with TestClient(app) as client:
        assert client.app.state.guard_tmx_push("twice", "%101") == ""
        assert client.app.state.guard_tmx_push("twice", "%101") == ""
    assert app.state.delivery_guard_refusals == {"pane_not_agent_log_viewer": 2}


def test_push_into_owned_agent_pane_is_delivered(tmp_db, project_state, monkeypatch):
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda _pane: ("agent", "current_command=2.1.185"))
    push = RecordingPush()
    app = _app(tmp_db, project_state, push)

    with TestClient(app) as client:
        assert client.post("/tasks", json=_payload()).status_code == 201

    assert [c[0] for c in push.calls] == ["%101"]
    assert app.state.delivery_guard_refusals == {}


def test_foreign_pane_is_refused_before_its_process_is_probed(tmp_db, project_state, monkeypatch):
    """#373's ownership refusal stands first: never probe a pane that is not ours."""
    probed = []
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda pane: probed.append(pane) or ("agent", ""))
    app = create_app(tmp_db, pane_map={"implementer": "%999"}, state_path=str(project_state),
                     port=8100, push_fn=RecordingPush(), watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json=_payload())
    assert probed == []


def test_inspect_only_guard_does_not_require_an_agent(tmp_db, project_state, monkeypatch):
    """The watchdog's busy/timeout probe must still see a crashed pane, or the
    task in it would never time out."""
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda _pane: ("shell", "current_command=bash"))
    app = _app(tmp_db, project_state, RecordingPush())
    TaskQueue(tmp_db).enqueue(TaskRequest(task_id="inspect", task_type="implement", description="w"))
    with TestClient(app) as client:
        assert client.app.state.guard_tmx_push("inspect", "%101", require_agent=False) == "%101"
    assert app.state.delivery_guard_refusals == {}


def test_watchdog_reminder_is_refused_without_requeueing_the_running_task(
    tmp_db, project_state, monkeypatch, caplog,
):
    """A reminder is a push as well. The task it nudges is already running, so
    the refusal withholds the text and leaves the task where it is."""
    kinds = iter([("agent", "current_command=claude")])
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda _pane: next(kinds, ("log_viewer", "current_command=python")))
    push = RecordingPush()
    app = create_app(tmp_db, pane_map={"implementer": "%101"}, state_path=str(project_state),
                     port=8100, push_fn=push, pane_busy_fn=lambda _pane: False,
                     reminder_seconds=300.0, timeout_seconds=10_000.0,
                     watchdog_disabled=True, anomaly_disabled=True)

    with TestClient(app) as client:
        client.post("/tasks", json=_payload("running"))
        assert len(push.calls) == 1            # delivered while the agent was there
        TaskQueue(tmp_db).bump_activity("running", ts=1000.0)
        result = app.state.watchdog_tick(now=1400.0)

    assert result["reminded"] == []
    assert len(push.calls) == 1
    assert _status(tmp_db, "running") == "in_progress"
    assert app.state.delivery_guard_refusals == {"pane_not_agent_log_viewer": 1}
    assert "task_id=running target=%101 resolved=%101 reason=pane_not_agent_log_viewer" in caplog.text


# ---------------------------------------------------------------------------
# Fix round 1 (review of addc29e, P1): a refused pane must not hot-loop the
# oldest task claim→requeue and starve the tasks queued behind it.
# ---------------------------------------------------------------------------

def _panes(kinds):
    """Probe answering per pane: {"%101": "agent", "%201": "log_viewer"}."""
    return lambda pane: (kinds[pane], f"current_command=probe-{pane}")


def _two_pane_app(db, tmp_path, push):
    state = tmp_path / "two-pane-state.json"
    state.write_text(json.dumps({"project": "owned", "pane_ids": ["%101", "%201"]}))
    # `codex` is an agent_override target living in its own owned pane.
    return create_app(db, pane_map={"implementer": "%101", "codex": "%201"},
                      state_path=str(state), port=8100, push_fn=push,
                      watchdog_disabled=True, anomaly_disabled=True)


def _context(db, task_id):
    return TaskQueue(db).get_task_context(task_id)


def test_blocked_oldest_task_does_not_starve_a_later_same_role_task(
    tmp_db, tmp_path, monkeypatch,
):
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        _panes({"%101": "agent", "%201": "log_viewer"}))
    monkeypatch.setenv("AGENT_CREW_STALE_PENDING_SECONDS", "0")
    push = RecordingPush()
    app = _two_pane_app(tmp_db, tmp_path, push)
    blocked = {**_payload("blocked"), "priority": 1, "context": {"agent_override": "codex"}}

    with TestClient(app) as client:
        client.post("/tasks", json=blocked)                   # refused on %201
        client.post("/tasks", json={**_payload("later"), "priority": 3})
        for tick in range(20):                                # the stale-pending loop
            app.state.watchdog_tick(now=time.time() + 60 + tick)

    assert [target for target, _ in push.calls] == ["%101"]   # `later` was delivered
    assert "later" in push.calls[0][1]
    assert _status(tmp_db, "later") == "in_progress"
    # No hot loop: twenty re-dispatch passes, one refusal — the backoff held.
    assert _context(tmp_db, "blocked")["push_refusals"] == {"%201": 1}
    assert _status(tmp_db, "blocked") == "pending"
    assert app.state.delivery_guard_refusals == {"pane_not_agent_log_viewer": 1}


def test_persistent_non_agent_pane_ends_the_task_as_needs_human_after_n(
    tmp_db, project_state, monkeypatch, caplog,
):
    monkeypatch.setattr("agent_crew.server._pane_process_kind",
                        lambda _pane: ("log_viewer", "current_command=python"))
    monkeypatch.setattr("agent_crew.server._PUSH_REFUSAL_BACKOFF_S", 0.0)
    monkeypatch.setattr("agent_crew.server._PUSH_REFUSAL_MAX", 3)
    monkeypatch.setenv("AGENT_CREW_STALE_PENDING_SECONDS", "0")
    push = RecordingPush()
    app = _app(tmp_db, project_state, push)

    with TestClient(app) as client:
        client.post("/tasks", json=_payload("stuck"))
        for tick in range(10):
            app.state.watchdog_tick(now=time.time() + 60 + tick)

    assert push.calls == []
    row = next(r for r in TaskQueue(tmp_db).list_all_with_status() if r["task_id"] == "stuck")
    assert row["status"] == "needs_human"
    # needs_human rows keep the reason in summary (queue persists error_info
    # only for failed/timed_out); the refusal record itself is in the context.
    import sqlite3
    with sqlite3.connect(tmp_db) as conn:
        summary = conn.execute("SELECT summary FROM tasks WHERE task_id='stuck'").fetchone()[0]
    assert summary == "push_refused_pane_not_agent_log_viewer"
    ctx = _context(tmp_db, "stuck")
    assert ctx["push_refusals"] == {"%101": 3}                        # bounded: stops at N
    assert ctx["push_refusal_reason"] == "pane_not_agent_log_viewer"
    assert app.state.delivery_guard_refusals == {"pane_not_agent_log_viewer": 3}
    assert "tmux push refused 3 times task_id=stuck" in caplog.text


def test_transient_non_agent_pane_delivers_once_the_agent_is_back(
    tmp_db, project_state, monkeypatch,
):
    kinds = {"%101": "shell"}
    monkeypatch.setattr("agent_crew.server._pane_process_kind", _panes(kinds))
    monkeypatch.setattr("agent_crew.server._PUSH_REFUSAL_BACKOFF_S", 0.0)
    monkeypatch.setenv("AGENT_CREW_STALE_PENDING_SECONDS", "0")
    push = RecordingPush()
    app = _app(tmp_db, project_state, push)

    with TestClient(app) as client:
        client.post("/tasks", json=_payload("flaky"))
        assert push.calls == [] and _status(tmp_db, "flaky") == "pending"
        kinds["%101"] = "agent"                               # the agent CLI restarted
        app.state.watchdog_tick(now=time.time() + 60)

    assert [target for target, _ in push.calls] == ["%101"]
    assert _status(tmp_db, "flaky") == "in_progress"
    assert _context(tmp_db, "flaky")["push_refusals"] == {"%101": 1}   # the history stays


def test_backoff_binds_only_the_push_path_not_other_consumers(tmp_db):
    """The dispatcher and MCP dequeue without skip_deferred: a pane's refusal
    never holds the task back from a consumer that does not use that pane."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="d", task_type="implement", description="w"))
    q.dequeue(role="implementer")
    assert q.defer_push_delivery("d", "%101", "pane_not_agent_log_viewer",
                                 max_refusals=3, backoff_s=600) == 1
    assert q.dequeue(role="implementer", skip_deferred=True) is None
    assert q.dequeue(role="implementer").task_id == "d"


def test_defer_backoff_doubles_per_refusal_on_the_same_pane(tmp_db, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("agent_crew.queue.time.time", lambda: clock[0])
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="b", task_type="implement", description="w"))
    waits = []
    for _ in range(3):
        q.dequeue(role="implementer")
        q.defer_push_delivery("b", "%101", "r", max_refusals=10, backoff_s=30)
        waits.append(q.get_task_context("b")["push_not_before"] - clock[0])
    assert waits == [30, 60, 120]
    assert q.defer_push_delivery("b", "%101", "r", max_refusals=10, backoff_s=30) is None  # pending
