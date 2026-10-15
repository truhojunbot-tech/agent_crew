"""#776: provider model discovery and zero-turn config failures."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def test_agy_models_discovers_newest_flash_and_keeps_configured_tier(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=(
            "Gemini 3.7 Flash (High)\nGemini 3.8 Flash (High)\n"
            "Gemini 3.8 Flash (Medium)\nGemini 3.8 Flash (Low)\n"), stderr="")
    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", (0.0, ()), raising=False)
    monkeypatch.setenv("AGENT_CREW_GEMINI_MODEL", "Gemini 3.7 Flash (Medium)")
    assert server._resolve_gemini_model() == "Gemini 3.8 Flash (Medium)"
    assert server._resolve_gemini_model() == "Gemini 3.8 Flash (Medium)"
    assert calls == [["agy", "models"]]


def test_agy_models_expands_compact_tiers_and_accepts_listed_nonflash_pin(monkeypatch):
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout="Available: Gemini 3.8 Flash (High|Medium|Low), Gemini 3.8 Pro (High)\n",
        stderr=""))
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", (0.0, ()), raising=False)
    monkeypatch.setenv("AGENT_CREW_GEMINI_MODEL", "Gemini 3.7 Flash (Medium)")
    assert server._resolve_gemini_model() == "Gemini 3.8 Flash (Medium)"
    monkeypatch.setenv("AGENT_CREW_GEMINI_MODEL", "Gemini 3.8 Pro (High)")
    assert server._resolve_gemini_model() == "Gemini 3.8 Pro (High)"


def test_agy_error_json_with_zero_turns_is_config_error():
    payload = json.dumps({"status": "ERROR", "num_turns": 0,
                          "error": "invalid model selection"})
    assert server._provider_zero_turn_config_error("gemini", payload)
    assert not server._provider_zero_turn_config_error(
        "gemini", json.dumps({"status": "ERROR", "num_turns": 2}))
    assert server._provider_zero_turn_config_error(
        "claude", json.dumps({"type": "result", "is_error": True, "num_turns": 0}))


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
