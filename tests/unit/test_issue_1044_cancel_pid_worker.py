"""#1044: `crew task cancel` must stop a server-owned ``pid:`` worker.

The CLI used to cancel DB-direct, which can only interrupt a tmux pane; the
crew server owns ``pid:`` dispatch workers and never noticed the row had moved.
No real process is signalled here: ``os.killpg`` is always monkeypatched.
"""

import json
import signal
import subprocess
import urllib.error
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import cancel_task_with_signal, create_app


class _FakeProc:
    def __init__(self, pid=5151):
        self.pid = pid
        self.returncode = None


@pytest.fixture
def project(tmp_path, monkeypatch, unused_tcp_port):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CANCEL_KILL_GRACE", "30")
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    db = str(project_dir / "tasks.db")
    state_path = project_dir / "state.json"
    state_path.write_text(json.dumps({"project": "demo", "db": db, "port": unused_tcp_port,
                                      "pane_ids": ["%101"]}))
    signalled = []
    monkeypatch.setattr("agent_crew.server.os.killpg",
                        lambda pid, sig: signalled.append((pid, sig)))
    app = create_app(db, state_path=str(state_path), port=unused_tcp_port, push_fn=lambda *_: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    yield {"db": db, "base": str(tmp_path), "app": app, "signalled": signalled,
           "port": unused_tcp_port}
    for timer in list(app.state.cancel_kill_timers.values()):
        timer.cancel()
    app.state.cancel_kill_timers.clear()


def _running(db, task_id, *, target):
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="work"))
    assert queue.dequeue(role="implementer") is not None
    queue.record_dispatch(task_id, channel="claude_p" if target.startswith("pid:")
                          else "tmux_pane", agent="claude", target=target)
    if target.startswith("%"):
        queue.set_push_at(task_id, pane_id=target)
    return queue


def _route_urlopen_to(client, port):
    """Serve the CLI's urllib DELETE from the in-process app."""
    def _urlopen(request, timeout=None):  # noqa: ARG001 - urlopen's signature
        url = request.full_url.replace(f"http://127.0.0.1:{port}", "")
        response = client.request(request.get_method(), url, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "",
                                         {}, _Body(response.content))
        return _Response(response.status_code, response.content)
    return _urlopen


class _Body:
    def __init__(self, content):
        self._content = content

    def read(self):
        return self._content

    def close(self):
        pass


class _Response(_Body):
    def __init__(self, status, content):
        super().__init__(content)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _cancel_cli(base, task_id):
    return CliRunner().invoke(crew, ["task", "cancel", task_id, "--project", "demo",
                                     "--base", base, "--allow-cross-project"])


def test_cli_cancel_with_server_up_goes_via_delete_and_sigterms_pid_worker(project):
    app = project["app"]
    with TestClient(app) as client:
        _running(project["db"], "pid-task", target="pid:5151")
        app.state.active_dispatch_processes["pid-task"] = _FakeProc(5151)
        with patch("urllib.request.urlopen", _route_urlopen_to(client, project["port"])):
            result = _cancel_cli(project["base"], "pid-task")
    assert result.exit_code == 0, result.output
    assert "via server" in result.output
    assert "worker_termination=sigterm_sent" in result.output
    assert project["signalled"] == [(5151, signal.SIGTERM)]
    assert TaskQueue(project["db"]).get_task_status("pid-task") == "cancelled"


def test_db_direct_cancel_with_active_slot_is_stopped_by_next_tick_exactly_once(project):
    app = project["app"]
    with TestClient(app):
        queue = _running(project["db"], "ext-task", target="pid:5151")
        app.state.active_dispatch_processes["ext-task"] = _FakeProc(5151)
        assert app.state.stop_externally_terminated_workers() == {}
        assert project["signalled"] == []

        cancel_task_with_signal(queue, "ext-task", state_path=None, pane_map=None,
                                events_path=str(project["db"]) + ".events",
                                out_of_process=True)
        first = app.state.stop_externally_terminated_workers()
        second = app.state.stop_externally_terminated_workers()
    assert first == {"ext-task": "sigterm_sent"}
    assert second == {}
    assert project["signalled"] == [(5151, signal.SIGTERM)]


def test_tick_does_not_resignal_a_worker_the_delete_already_stopped(project):
    app = project["app"]
    with TestClient(app) as client:
        _running(project["db"], "del-task", target="pid:5151")
        app.state.active_dispatch_processes["del-task"] = _FakeProc(5151)
        assert client.delete("/tasks/del-task").json()["worker_termination"] == "sigterm_sent"
        assert app.state.stop_externally_terminated_workers() == {}
    assert project["signalled"] == [(5151, signal.SIGTERM)]


def test_completed_row_waits_for_grace_before_stop(project, monkeypatch):
    app = project["app"]
    with TestClient(app):
        queue = _running(project["db"], "done-task", target="pid:5151")
        app.state.active_dispatch_processes["done-task"] = _FakeProc(5151)
        import sqlite3
        with sqlite3.connect(project["db"]) as c:
            c.execute("UPDATE tasks SET status='completed' WHERE task_id='done-task'")
        clock = [1e9]
        monkeypatch.setattr("agent_crew.server.time.monotonic", lambda: clock[0])
        # A worker that just posted its own result gets time to exit by itself.
        assert app.state.stop_externally_terminated_workers() == {}
        clock[0] += 29
        assert app.state.stop_externally_terminated_workers() == {}
        clock[0] += 2
        assert app.state.stop_externally_terminated_workers() == {"done-task": "sigterm_sent"}
    assert queue.get_task_status("done-task") == "completed"
    assert project["signalled"] == [(5151, signal.SIGTERM)]


def test_pane_worker_keeps_c_c_path_via_server(project):
    app = project["app"]
    with TestClient(app) as client:
        _running(project["db"], "pane-task", target="%101")
        with patch("agent_crew.server._pane_alive_for_push", side_effect=[True, False]), \
             patch("agent_crew.server.subprocess.run") as run, \
             patch("urllib.request.urlopen", _route_urlopen_to(client, project["port"])):
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            result = _cancel_cli(project["base"], "pane-task")
    assert any(c.args[0] == ["tmux", "send-keys", "-t", "%101", "C-c"]
               for c in run.call_args_list)
    assert "signal=pane_exited" in result.output
    assert result.exit_code == 0, result.output
    assert project["signalled"] == []


def test_server_down_fallback_reports_server_owned_worker_not_signalled(
        tmp_path, unused_tcp_port, monkeypatch):
    signalled = []
    monkeypatch.setattr("agent_crew.server.os.killpg",
                        lambda pid, sig: signalled.append((pid, sig)))
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    db = str(project_dir / "tasks.db")
    (project_dir / "state.json").write_text(json.dumps({
        "project": "demo", "db": db, "port": unused_tcp_port}))
    _running(db, "down-task", target="pid:5151")
    result = _cancel_cli(str(tmp_path), "down-task")
    assert "unreachable" in result.output and "DB-direct" in result.output
    assert "server_owned_worker_not_signalled" in result.output
    assert result.exit_code != 0
    assert signalled == []
    assert TaskQueue(db).get_task_status("down-task") == "cancelled"
