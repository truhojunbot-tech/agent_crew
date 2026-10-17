"""#750: accepted results emit the configured quota contract asynchronously."""
import json
import threading
import time
import subprocess

from fastapi.testclient import TestClient

from agent_crew import contract_emit
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app, _record_exit_response_telemetry


def test_result_post_triggers_one_emit_for_project(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CONTRACT_EMIT_CMD", "emitter --project {project}")
    projects = []
    monkeypatch.setattr(contract_emit, "_emit", lambda command, project: projects.append(project))
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("discuss-contract", "discuss", "discuss", project="agent_crew"))
    app = create_app(db, project="agent_crew", pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/discuss-contract/result",
                               json={"task_id": "discuss-contract", "status": "completed",
                                     "summary": "done"},
                               headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 200, response.text
    assert contract_emit.wait_idle("agent_crew", timeout=2)
    assert projects == ["agent_crew"]


def test_exit_telemetry_reemits_after_post_result_receipt_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CONTRACT_EMIT_CMD", "emitter --project {project}")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("post-result", "implement", "work", project="agent_crew"))
    queue.record_attribution("post-result", agent="claude")
    queue.submit_result("post-result", TaskResult("post-result", "completed", "done"))
    before = json.loads(queue.get_tokenomics_shadow_receipt("post-result")["economics_json"])
    assert before.get("output_tokens") is None
    emitted = []
    def capture(_command, project):
        receipt = queue.get_tokenomics_shadow_receipt("post-result")
        emitted.append((project, json.loads(receipt["economics_json"])["output_tokens"]))
    monkeypatch.setattr(contract_emit, "_emit", capture)
    tail = json.dumps({"type": "result", "usage": {"output_tokens": 7}})
    _record_exit_response_telemetry(queue, "post-result", "claude", tail,
                                    project="agent_crew")
    assert contract_emit.wait_idle("agent_crew", timeout=2)
    assert emitted == [("agent_crew", 7)]


def test_unset_command_does_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_CONTRACT_EMIT_CMD", raising=False)
    calls = []
    monkeypatch.setattr(contract_emit, "_emit", lambda *args: calls.append(args))
    assert contract_emit.schedule_emit("agent_crew") is False
    assert calls == []


def test_configured_command_receives_project_without_shell(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]["AGENT_CREW_CONTRACT_PROJECT"],
                      kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    contract_emit._emit("publisher --contract-out /tmp/{project}.json", "agent_crew")
    assert calls == [(["publisher", "--contract-out", "/tmp/agent_crew.json"],
                      "agent_crew", 120)]


def test_slow_failure_is_nonblocking_and_bursts_coalesce(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CONTRACT_EMIT_CMD", "emitter --project {project}")
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def slow_failure(command, project):
        calls.append(project)
        entered.set()
        release.wait(timeout=2)
        raise RuntimeError("emitter failed")
    monkeypatch.setattr(contract_emit, "_emit", slow_failure)
    start = time.perf_counter()
    assert contract_emit.schedule_emit("agent_crew")
    assert time.perf_counter() - start < 0.05
    assert entered.wait(timeout=2)
    start = time.perf_counter()
    for _ in range(10):
        contract_emit.schedule_emit("agent_crew")
    assert time.perf_counter() - start < 0.05
    assert contract_emit.in_flight("agent_crew") == 1
    release.set()
    assert contract_emit.wait_idle("agent_crew", timeout=2)
    assert calls == ["agent_crew", "agent_crew"]


def test_slow_emitter_adds_less_than_50ms_to_http_result(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_CONTRACT_EMIT_CMD", raising=False)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("slow", "discuss", "discuss", project="agent_crew"))
    entered = threading.Event()
    release = threading.Event()
    emitter_threads = []
    def slow_failure(_command, _project):
        emitter_threads.append(threading.current_thread())
        entered.set()
        release.wait(timeout=10)
        raise RuntimeError("emitter unavailable")
    monkeypatch.setattr(contract_emit, "_emit", slow_failure)
    app = create_app(db, project="agent_crew", pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True)
    try:
        with TestClient(app) as client:
            monkeypatch.setenv("AGENT_CREW_CONTRACT_EMIT_CMD", "emitter")
            response = client.post("/tasks/slow/result",
                                   json={"task_id": "slow", "status": "completed",
                                         "summary": "done"},
                                   headers={"X-Agent-Crew-Project": "agent_crew"})
            assert response.status_code == 200, response.text
            assert entered.wait(timeout=5)
            assert not release.is_set()
            assert emitter_threads[0].is_alive()
            assert contract_emit.in_flight("agent_crew") == 1
    finally:
        release.set()
        assert contract_emit.wait_idle("agent_crew", timeout=2)
