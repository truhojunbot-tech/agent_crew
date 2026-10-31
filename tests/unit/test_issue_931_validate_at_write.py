"""Malformed task data is rejected at ingress and cannot weaken read guards."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew import testing_policy
from agent_crew.cea.store import ensure_schema
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import (MalformedStoredRow, TaskQueue, _result_to_json)
from agent_crew.server import create_app


def _task(task_id, task_type="implement", **context):
    return TaskRequest(task_id=task_id, task_type=task_type, description="fixture",
                       branch=f"branch/{task_id}", context={"risk_tier": 1, **context})


def _corrupt(queue, task_id, **values):
    conn = queue._connect()
    try:
        for name, value in values.items():
            conn.execute(f"UPDATE tasks SET {name}=? WHERE task_id=?", (value, task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("value", ["2", None, True, -1, 1.5])
def test_fix_round_is_validated_at_write(value):
    with pytest.raises(ValueError, match="fix_round"):
        _task("bad-round", fix_round=value)


def test_context_and_result_payloads_are_validated_at_write():
    with pytest.raises(ValueError, match="context"):
        _task("bad-json", items={"not-json"})
    with pytest.raises(ValueError, match="commit"):
        TaskResult(task_id="x", status="completed", summary="done", commit="HEAD")
    with pytest.raises(ValueError, match="findings"):
        TaskResult(task_id="x", status="completed", summary="done",
                   findings=[{"severity": "HIGH"}])
    with pytest.raises(TypeError):
        _result_to_json(object())


def test_http_rejects_invalid_context_with_422(tmp_path):
    app = create_app(str(tmp_path / "tasks.db"), project="demo", pane_map={},
                     worktree_map={}, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "bad-http", "task_type": "implement", "description": "fixture",
            "branch": "feature", "context": {"risk_tier": 1, "fix_round": "1"}},
            headers={"X-Agent-Crew-Project": "demo"})
    assert response.status_code == 422


def test_stored_context_blocks_read_and_busy_check(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("bad-context"))
    _corrupt(queue, "bad-context", context="{bad")
    with pytest.raises(MalformedStoredRow):
        queue.get_task_context("bad-context")
    with pytest.raises(MalformedStoredRow):
        queue.get_task("bad-context")
    queue.enqueue(_task("discussion", "discuss", agent="codex"))
    _corrupt(queue, "discussion", context="{bad", status="in_progress")
    with pytest.raises(MalformedStoredRow):
        queue.has_discuss_in_progress_for_agent("codex")


def test_bad_stored_fix_round_cannot_reset_the_cap(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("round", fix_round=2))
    _corrupt(queue, "round", context=json.dumps({"risk_tier": 1, "fix_round": "broken"}))
    with pytest.raises(MalformedStoredRow, match="fix_round"):
        queue.get_task("round")


def test_head_lookup_error_cannot_become_an_empty_pin(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    monkeypatch.setattr("agent_crew.github.pr_head_sha",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(MalformedStoredRow, match="head lookup failed"):
        queue._resolve_review_head(_task("head", "review"),
                                   {"repo": "owner/repo", "pr_number": 931})


def test_bad_stored_review_cannot_be_skipped_by_dedup(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("standing", "review", pr_number=931,
                        reviewed_sha="a" * 40))
    _corrupt(queue, "standing", context="{bad")
    incoming = _task("incoming", "review", pr_number=931,
                     reviewed_sha="a" * 40)
    with pytest.raises(MalformedStoredRow):
        queue.enqueue(incoming)
    assert queue.get_task("incoming") is None


def test_bad_findings_cannot_retain_a_verdict(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("review", "review", reviewed_sha="a" * 40))
    _corrupt(queue, "review", status="completed", verdict="request_changes",
             pr_number=931, findings="{bad")
    with pytest.raises(MalformedStoredRow):
        queue.standing_request_changes_review(pr_number=931, reviewed_sha="a" * 40)


def test_cloud_legacy_migration_runs_once(tmp_path):
    path = str(tmp_path / "tasks.db")
    queue = TaskQueue(path)
    queue.enqueue(_task("cloud"))
    legacy = {"risk_tier": 1, "cloud_launched_at": 100.0,
              "cloud_usd": 2.0, "cloud_cost_finalized_at": 150.0}
    _corrupt(queue, "cloud", context=json.dumps(legacy), status="completed",
             status_changed_at=200.0)
    conn = queue._connect()
    conn.execute("DELETE FROM task_metadata_migrations WHERE name='legacy_cloud_cost_v1'")
    conn.commit()
    conn.close()
    upgraded = TaskQueue(path)
    context = upgraded.get_task_context("cloud")
    assert context["cloud_ended_at"] == 200.0
    assert context["cloud_usd"] is None
    assert context["cloud_cost_overlaps"] == []
    assert TaskQueue(path).get_task_context("cloud") == context


def test_legacy_dict_findings_are_migrated_once_and_listable(tmp_path):
    path = str(tmp_path / "tasks.db")
    queue = TaskQueue(path)
    queue.enqueue(_task("legacy-review", "review"))
    old = [{"issue": "code_quality: name it", "canary_reused_from": "rev-old"},
           {"severity": "HIGH", "file": "src/a.py", "line": 3,
            "title": "Bug", "detail": "fix it"}]
    _corrupt(queue, "legacy-review", findings=json.dumps(old))
    conn = queue._connect()
    conn.execute("DELETE FROM task_metadata_migrations WHERE name='legacy_dict_findings_v1'")
    conn.commit()
    conn.close()
    upgraded = TaskQueue(path)
    findings = upgraded.list_tasks()[0].findings
    assert len(findings) == 2
    assert all(isinstance(item, str) for item in findings)
    assert "rev-old" in findings[0]
    assert TaskQueue(path).list_tasks()[0].findings == findings


def test_cea_alter_error_other_than_duplicate_column_propagates():
    class BrokenConnection:
        def execute(self, sql):
            if sql.startswith("ALTER TABLE"):
                raise sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        ensure_schema(BrokenConnection())


def test_bad_testing_scope_is_rejected():
    with pytest.raises(ValueError, match="targeted"):
        testing_policy._coerce({"targeted": "pytest tests"}, "fixture")
