"""#776: provider model discovery and zero-turn config failures."""
import asyncio
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "missing"])
def test_unavailable_model_discovery_preserves_configured_pin(monkeypatch, failure):
    monkeypatch.setenv("AGENT_CREW_GEMINI_MODEL", "Gemini 3.8 Flash (Medium)")
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", (0.0, ()), raising=False)

    def unavailable(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 5)
        if failure == "missing":
            raise FileNotFoundError("agy")
        return SimpleNamespace(returncode=2, stdout="", stderr="agy unavailable")

    monkeypatch.setattr(server.subprocess, "run", unavailable)
    assert server._resolve_gemini_model() == "Gemini 3.8 Flash (Medium)"


def test_unavailable_model_discovery_without_pin_is_distinct_from_empty_listing(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_GEMINI_MODEL", raising=False)
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", (0.0, ()), raising=False)
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=2, stdout="", stderr="agy unavailable"))
    assert server._resolve_gemini_model() is None

    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", (0.0, ()), raising=False)
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="Claude Sonnet\n", stderr=""))
    assert server._resolve_gemini_model() == ""


def test_model_discovery_does_not_block_dispatch_event_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    worktree = tmp_path / "gemini"
    worktree.mkdir()
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="test-776-slow-models", task_type="test",
                              description="test", branch="agent/fix", project="proj"))
    task = queue.dequeue(role="tester")
    assert task is not None
    app = server.create_app(db, pane_map={}, worktree_map={"tester": str(worktree)},
                            watchdog_disabled=True, anomaly_disabled=True,
                            identity_required=False, coordinator_notify_fn=lambda _: True)
    entered = threading.Event()
    release = threading.Event()

    def slow_resolve():
        entered.set()
        release.wait(2)
        return None

    monkeypatch.setattr(server, "_resolve_gemini_model", slow_resolve)

    async def run():
        dispatch = asyncio.create_task(app.state.dispatch_task(task, "tester", dispatch_agent="gemini"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert not dispatch.done(), "model discovery blocked the event loop"
        finally:
            release.set()
            await dispatch

    with TestClient(app):
        asyncio.run(run())


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
