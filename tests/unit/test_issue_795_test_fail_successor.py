"""#795: a server-owned test failure must continue its lineage."""

from dataclasses import asdict

from fastapi.testclient import TestClient

from agent_crew.pipeline import fix_task_id
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    import agent_crew.github as github
    monkeypatch.setattr(github, "pr_state", lambda *args, **kwargs: "open")
    monkeypatch.setattr(github, "pr_head_sha", lambda *args, **kwargs: "a" * 40)
    db = str(tmp_path / "tasks.db")
    notices = []
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    queue = TaskQueue(db)
    task = TaskRequest(
        task_id="test-795", task_type="test", description="test PR",
        branch="agent/795-test-fail-successor", project="agent_crew",
        context={"pr_number": 794, "base_branch": "main", "repo": "owner/repo",
                 "issue": 795, "risk_tier": 2, "external_operation_id": "op-795",
                 "reviewed_sha": "a" * 40, "fix_round": 0,
                 "implementer_agent": "codex", "reviewer_agent": "claude",
                 "agent_override": "gemini"})
    if hasattr(queue, "admit_work_intent"):
        # #793 may merge before this PR; exercise the durable binding too.
        queue.admit_work_intent({
            "external_operation_id": "op-795", "project": "agent_crew",
            "repo": "owner/repo", "issue": 795, "coordinator_identity": "owner",
            "coordinator_session": "session", "priority": 3, "risk_tier": 2,
            "execution_slo_s": 60, "task": asdict(task),
        })
    else:
        queue.enqueue(task, ingress="http.tasks")
    return app, queue, notices


def _post(client, status="failed", findings=None, error_info=None):
    return client.post("/tasks/test-795/result", json={
        "task_id": "test-795", "status": status, "summary": "test outcome",
        "findings": findings or [], "pr_number": 794,
        "error_info": error_info or {},
    })


def _failed_script_result(client):
    return _post(client, error_info={
        "reason": "script_test_new_failures", "final": True,
        "script_test": {"outcome": "FAILED", "reason": "new_failures",
                        "new_failures": ["tests/test_x.py::test_x"]},
    })


def test_script_new_failures_create_one_fix_with_lineage(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = _failed_script_result(client)
    assert response.status_code == 200, response.text
    fix_id = fix_task_id("test-795", 1)
    fixes = [task for task in queue.list_tasks() if task.task_id == fix_id]
    assert len(fixes) == 1
    fix = fixes[0]
    assert fix.task_type == "implement" and fix.branch == "agent/795-test-fail-successor"
    assert "tests/test_x.py::test_x" in fix.description
    assert fix.context["review_findings"] == ["new failure: tests/test_x.py::test_x"]
    assert fix.context["prev_task_id"] == "test-795"
    assert fix.context["pr_number"] == 794 and fix.context["issue"] == 795
    assert fix.context["external_operation_id"] == "op-795"
    assert fix.context["agent_override"] == "codex"
    if hasattr(queue, "get_work_intent"):
        assert queue.get_work_intent("op-795")["task_ids"] == ["test-795", fix_id]
    assert notices == []


def test_llm_test_failure_with_findings_creates_fix(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = _post(client, findings=["tests/test_y.py::test_y failed"])
    assert response.status_code == 200, response.text
    fix = queue.get_task(fix_task_id("test-795", 1))
    assert fix is not None
    assert fix.context["review_findings"] == ["tests/test_y.py::test_y failed"]
    assert notices == []


def test_fix_cap_sends_one_notice_without_successor(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "0")
    app, queue, notices = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = _failed_script_result(client)
    assert response.status_code == 200, response.text
    assert queue.get_task(fix_task_id("test-795", 1)) is None
    assert len(notices) == 1 and "test_failed_no_successor" in notices[0]


def test_not_run_does_not_create_fix_or_notice(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = _post(client, error_info={"final": True})
    assert response.status_code == 200, response.text
    assert queue.get_task(fix_task_id("test-795", 1)) is None
    assert notices == []
