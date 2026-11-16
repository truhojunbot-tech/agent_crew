"""#1025: a discuss answer left as a worktree file survives the next prep clean."""

import hashlib
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew.discuss_artifacts import DISCUSS_ARTIFACT_CAP_BYTES, parse_artifact
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _git(wt, *args):
    subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True)


@pytest.fixture
def worktree(tmp_path):
    wt = tmp_path / "codex"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("tracked\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-q", "-m", "init")
    return wt


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    notices = []
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    return app, TaskQueue(db), notices


def _dispatch(queue, task_id, worktree, task_type="discuss"):
    """Enqueue, claim and attribute the task the way the dispatcher does."""
    queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                              description="task", branch="main", project="agent_crew",
                              context={"risk_tier": 2, "issue": 1025, "agent": "codex"}),
                  ingress="http.tasks")
    if task_type == "discuss":
        assert queue.dequeue_discuss_for_agent("codex").task_id == task_id
    else:
        conn = queue._connect()
        try:
            conn.execute("UPDATE tasks SET status='in_progress' WHERE task_id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()
    queue.record_attribution(task_id=task_id, project="agent_crew", agent="codex",
                             role="panel", task_type=task_type,
                             worktree_path=str(worktree), status="in_progress")


def _post(app, task_id, summary="done", findings=None, **fields):
    with TestClient(app) as client:
        return client.post(f"/tasks/{task_id}/result", json={
            "task_id": task_id, "status": "completed", "summary": summary,
            "findings": findings or [], **fields})


def _prep_clean(wt):
    _git(wt, "reset", "--hard")
    _git(wt, "clean", "-fd")


def _artifacts(queue, task_id):
    return [e for e in map(parse_artifact, queue.get_result(task_id).findings) if e]


def _missing_notices(notices):
    return [n for n in notices if "kind=discuss_artifact_missing" in n]


def test_untracked_answer_survives_prep_clean(harness, worktree):
    app, queue, notices = harness
    _dispatch(queue, "codex-1025a", worktree)
    answer = "# Cause\n\nprimary-source assessment ✓\n"
    (worktree / "cases").mkdir()
    (worktree / "cases" / "move.md").write_text(answer)

    response = _post(app, "codex-1025a", summary="wrote assessment to cases/move.md")
    assert response.status_code == 200, response.text
    _prep_clean(worktree)
    assert not (worktree / "cases").exists()

    task = queue.get_task("codex-1025a")
    assert task.status == "completed"
    [entry] = _artifacts(queue, "codex-1025a")
    assert entry["path"] == "cases/move.md"
    assert entry["content"] == answer
    assert entry["sha256"] == hashlib.sha256(answer.encode()).hexdigest()
    assert not entry.get("truncated")
    assert _missing_notices(notices) == []


def test_modified_tracked_file_is_captured(harness, worktree):
    app, queue, _ = harness
    _dispatch(queue, "codex-1025b", worktree)
    (worktree / "README.md").write_text("tracked\nedited by worker\n")

    assert _post(app, "codex-1025b").status_code == 200
    _prep_clean(worktree)

    [entry] = _artifacts(queue, "codex-1025b")
    assert entry["path"] == "README.md"
    assert entry["content"] == "tracked\nedited by worker\n"


def test_over_cap_content_is_truncated(harness, worktree):
    app, queue, _ = harness
    _dispatch(queue, "codex-1025c", worktree)
    big = "a" * (DISCUSS_ARTIFACT_CAP_BYTES + 1000)
    (worktree / "big.txt").write_text(big)

    assert _post(app, "codex-1025c").status_code == 200

    [entry] = _artifacts(queue, "codex-1025c")
    assert entry["truncated"] is True
    assert len(entry["content"].encode()) == DISCUSS_ARTIFACT_CAP_BYTES
    assert entry["sha256"] == hashlib.sha256(big.encode()).hexdigest()


def test_binary_file_records_path_and_hash_only(harness, worktree):
    app, queue, _ = harness
    _dispatch(queue, "codex-1025d", worktree)
    blob = b"\x89PNG\x00\x01\xff"
    (worktree / "chart.png").write_bytes(blob)

    assert _post(app, "codex-1025d").status_code == 200

    [entry] = _artifacts(queue, "codex-1025d")
    assert entry["path"] == "chart.png"
    assert entry["sha256"] == hashlib.sha256(blob).hexdigest()
    assert "content" not in entry


def test_missing_pointer_warns_once_and_stays_completed(harness, worktree):
    app, queue, notices = harness
    _dispatch(queue, "codex-1025e", worktree)

    response = _post(app, "codex-1025e",
                     summary="DART-only report written to cases/regime/h2a.md")
    assert response.status_code == 200, response.text

    task = queue.get_task("codex-1025e")
    assert task.status == "completed"
    stored = queue.get_result("codex-1025e")
    assert stored.error_info["warning"] == "discuss_artifact_missing"
    assert {"kind": "discuss_artifact_missing",
            "paths": ["cases/regime/h2a.md"]} in stored.error_info["warnings"]
    [notice] = _missing_notices(notices)
    assert "task=codex-1025e" in notice and "cases/regime/h2a.md" in notice


def test_pointer_with_findings_does_not_warn(harness, worktree):
    app, queue, notices = harness
    _dispatch(queue, "codex-1025f", worktree)

    assert _post(app, "codex-1025f", summary="see cases/x.md",
                 findings=["the full answer"]).status_code == 200

    assert not (queue.get_result("codex-1025f").error_info or {}).get("warning")
    assert _missing_notices(notices) == []


def test_implement_result_gets_no_artifact_entries(harness, worktree, monkeypatch):
    app, queue, notices = harness
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.pr_head_sha", lambda *a, **k: "a" * 40)
    _dispatch(queue, "impl-1025g", worktree, task_type="implement")
    (worktree / "notes.md").write_text("scratch")

    _post(app, "impl-1025g", summary="wrote notes.md", branch="agent/x",
          commit="b" * 40, pr_number=1)

    result = queue.get_result("impl-1025g")
    findings = result.findings if result is not None else []
    assert not [f for f in findings if parse_artifact(f)]
    assert _missing_notices(notices) == []


def test_discuss_task_block_tells_worker_to_use_findings():
    from agent_crew.server import _format_task_message
    task = TaskRequest(task_id="codex-1025h", task_type="discuss", description="topic")
    assert "full answer in findings" in _format_task_message(task, 8105)
    impl = TaskRequest(task_id="impl-1025h", task_type="implement", description="x")
    assert "full answer in findings" not in _format_task_message(impl, 8105)
