"""#776: headless Gemini model pin and zero-turn config failures."""
import json

from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.failure_kind import classify_provider_record
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def test_gemini_model_selection_uses_only_pin_or_default(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("model discovery must not run")

    monkeypatch.setattr(server.subprocess, "run", forbidden)
    monkeypatch.delenv("AGENT_CREW_GEMINI_MODEL", raising=False)
    assert server._resolve_gemini_model() == "Gemini 3.8 Flash (Medium)"
    monkeypatch.setenv("AGENT_CREW_GEMINI_MODEL", "Gemini 3.7 Flash (Medium)")
    assert server._resolve_gemini_model() == "Gemini 3.7 Flash (Medium)"


def test_agy_error_json_with_zero_turns_is_config_error():
    payload = json.dumps({"status": "ERROR", "num_turns": 0, "reason": "INVALID_MODEL",
                          "error": "invalid model selection"})
    assert classify_provider_record("gemini", json.loads(payload))["kind"] == "config_error"
    assert classify_provider_record("gemini", {"status": "ERROR", "num_turns": 2})["kind"] == "agent_failed"
    assert classify_provider_record("claude", {"type": "result", "is_error": True,
                                               "num_turns": 0, "code": "INVALID_MODEL"})["kind"] == "config_error"


def test_server_posted_provider_config_error_has_no_retry_and_one_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="test-776-config", task_type="test",
                              description="test", branch="agent/fix", project="proj",
                              context={"repo": "owner/repo", "pr_number": 776,
                                       "reviewed_sha": "a" * 40}))
    assert queue.dequeue(role="tester") is not None
    notices = []
    app = server.create_app(db, pane_map={}, watchdog_disabled=True,
                            anomaly_disabled=True, identity_required=False,
                            coordinator_notify_fn=lambda msg: notices.append(msg) or True)
    with TestClient(app) as client:
        response = client.post("/tasks/test-776-config/result", json={
            "task_id": "test-776-config", "status": "failed", "summary": "agy config error",
            "error_info": {"reason": "config_error", "provider_status": "ERROR",
                           "provider_num_turns": 0}})
    assert response.status_code == 200, response.text
    assert queue.get_task("test-776-config").status == "needs_human"
    assert not any(t.task_type == "implement" for t in queue.list_tasks())
    assert len(notices) == 1
