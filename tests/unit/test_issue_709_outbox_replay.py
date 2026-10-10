"""Paused cascades replay on resume or boot, with stale work excluded (#709)."""

import sqlite3
import time

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest

from agent_crew import cli, pause
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setattr(pause, "GLOBAL_PAUSE_FILE", str(tmp_path / "global-pause.json"))
    project_dir = tmp_path / "testproj"
    project_dir.mkdir()
    db = project_dir / "tasks.db"
    queue = TaskQueue(str(db))
    queue.enqueue(TaskRequest(
        task_id="impl-709", task_type="implement", description="implement",
        branch="agent/709-test", project="testproj",
        context={"risk_tier": 2}))
    assert queue.dequeue(role="implementer") is not None
    queue.set_stop_epoch(True)
    queue.submit_result("impl-709", TaskResult(
        task_id="impl-709", status="completed", summary="done"))
    assert queue.outbox_get("impl-709")["state"] == "pending"
    return queue, project_dir


def _app(queue):
    return create_app(queue._db_path, project="testproj", pane_map={},
                      worktree_map={}, watchdog_disabled=True,
                      anomaly_disabled=True)


def _resume(queue):
    epoch = queue.get_stop_epoch()["epoch"]
    assert queue.resume_stop(generation=epoch + 1, who="owner:test",
                             decision_id="D-51-9")["resumed"]


def _reviews(queue):
    return [task for task in queue.list_tasks() if task.task_type == "review"]


def test_startup_replays_pending_result_once(pending):
    queue, _ = pending
    _resume(queue)
    with TestClient(_app(queue)) as client:
        assert len(_reviews(queue)) == 1
        assert queue.outbox_get("impl-709")["state"] == "applied"
        assert client.post("/admin/replay-suppressed").json()["replayed"] == []
    with TestClient(_app(queue)):
        assert len(_reviews(queue)) == 1


def test_resume_notifies_replay_and_creates_successor(pending, tmp_path, monkeypatch):
    queue, _ = pending
    monkeypatch.setattr(cli, "_writable_queue", lambda *a, **k: queue)
    with TestClient(_app(queue)) as client:
        calls = []

        def replay(base, project):
            calls.append((base, project))
            return client.post("/admin/replay-suppressed").json()

        monkeypatch.setattr(cli, "_replay_outbox_on_server", replay, raising=False)
        result = CliRunner().invoke(cli.crew, [
            "resume", "testproj", "--base", str(tmp_path),
            "--generation", str(queue.get_stop_epoch()["epoch"] + 1),
            "--decision-id", "D-51-9"])
        assert result.exit_code == 0, result.output
        assert calls == [(str(tmp_path), "testproj")]
        assert len(_reviews(queue)) == 1
        assert queue.outbox_get("impl-709")["state"] == "applied"
        assert client.post("/admin/replay-suppressed").json()["replayed"] == []


def test_old_pending_row_expires_at_startup(pending, monkeypatch):
    queue, _ = pending
    monkeypatch.setenv("AGENT_CREW_OUTBOX_REPLAY_MAX_AGE_S", "60")
    with sqlite3.connect(queue._db_path) as db:
        db.execute("UPDATE cascade_outbox SET created_at=? WHERE parent_task_id='impl-709'",
                   (time.time() - 61,))
    _resume(queue)
    with TestClient(_app(queue)) as client:
        assert not _reviews(queue)
        assert queue.outbox_get("impl-709")["state"] == "expired"
        assert client.get("/health").json()["outbox"] == {"pending": 0, "expired": 1}


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_terminal_pr_expires_without_replay(pending, monkeypatch, state):
    queue, _ = pending
    queue.patch_context("impl-709", {"pr_number": 709, "repo": "owner/repo"})
    from agent_crew import github
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: state)
    _resume(queue)
    with TestClient(_app(queue)):
        assert not _reviews(queue)
        assert queue.outbox_get("impl-709")["state"] == "expired"


def test_cancelled_parent_expires_without_replay(pending):
    queue, _ = pending
    with sqlite3.connect(queue._db_path) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE task_id='impl-709'")
    _resume(queue)
    with TestClient(_app(queue)):
        assert not _reviews(queue)
        assert queue.outbox_get("impl-709")["state"] == "expired"


def test_superseded_parent_expires_without_replay(pending):
    queue, _ = pending
    queue.patch_context("impl-709", {"superseded_by": "impl-new"})
    _resume(queue)
    with TestClient(_app(queue)):
        assert not _reviews(queue)
        assert queue.outbox_get("impl-709")["state"] == "expired"


def test_unknown_pr_state_leaves_row_pending(pending, monkeypatch):
    queue, _ = pending
    queue.patch_context("impl-709", {"pr_number": 709, "repo": "owner/repo"})
    from agent_crew import github
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "unknown")
    _resume(queue)
    with TestClient(_app(queue)):
        assert not _reviews(queue)
        assert queue.outbox_get("impl-709")["state"] == "pending"


def test_resume_server_request_uses_project_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_read_state", lambda base, project: {"port": 8105})
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok","replayed":[]}'

    def urlopen(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(),
                    project=request.get_header("X-agent-crew-project"), timeout=timeout)
        return Response()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert cli._replay_outbox_on_server(str(tmp_path), "testproj")["status"] == "ok"
    assert seen == {"url": "http://127.0.0.1:8105/admin/replay-suppressed",
                    "method": "POST", "project": "testproj", "timeout": 300}
