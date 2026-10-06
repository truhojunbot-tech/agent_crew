"""The server's test-result merge path must use the ADR-004 gate (#615)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew import github
from agent_crew.conformance_gate import _conformance_gate_allows_merge
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


CHECKER = Path("/home/truhojun/alfred/tools/contract_registry.py")
REGISTRY = Path("/home/truhojun/alfred/governance/capability_registry.json")
DIFF = ("diff --git a/src/agent_crew/x.py b/src/agent_crew/x.py\n"
        "+++ b/src/agent_crew/x.py\n+new code\n")


@pytest.fixture
def checker(tmp_path, monkeypatch):
    if not CHECKER.exists() or not REGISTRY.exists():
        pytest.skip("Alfred checker or registry unavailable")
    registry = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY, registry)
    wrapper = tmp_path / "checker.sh"
    wrapper.write_text(f'#!/bin/sh\nexec python3 "{CHECKER}" "$@" --registry "{registry}"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("AGENT_CREW_CONFORMANCE_GATE_CMD", str(wrapper))
    monkeypatch.setenv("AGENT_CREW_CEA_REGISTRY_PATH", str(registry))
    return wrapper


def _submit_test(tmp_path, monkeypatch, *, title, gate_enabled=True,
                 change_type="create", gate_failure="", status_details=None):
    if not gate_enabled:
        monkeypatch.delenv("AGENT_CREW_CONFORMANCE_GATE_CMD", raising=False)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="impl-615", task_type="implement", branch="fix/615",
        description=title, context={"issue": 615, "change_type": change_type}))
    queue.submit_result("impl-615", TaskResult("impl-615", "completed", "done"))
    queue.enqueue(TaskRequest(
        task_id="review-615", task_type="review", branch="fix/615",
        description="review", context={"prev_task_id": "impl-615",
                                       "pr_number": 615, "repo": "owner/repo"}))
    queue.submit_result("review-615", TaskResult(
        "review-615", "completed", "approved", verdict="approve", pr_number=615))
    queue.enqueue(TaskRequest(
        task_id="test-615", task_type="test", branch="fix/615",
        description="test", context={"prev_task_id": "review-615",
                                     "pr_number": 615, "repo": "owner/repo"}))
    calls = []
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "open")
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(github, "independent_review_for_head",
                        lambda *a, **k: ("a" * 40, "claude", "review-615", "ok"))
    def publish_status(*args, **kwargs):
        calls.append("status")
        if status_details is not None:
            status_details.append(args)
        return True

    monkeypatch.setattr(github, "publish_independent_review_status", publish_status)
    monkeypatch.setattr(github, "independent_review_succeeded", lambda *a, **k: True)
    monkeypatch.setattr(github, "merge_pr", lambda *a, **k: calls.append("merge") or True)
    monkeypatch.setattr(github, "post_pr_comment",
                        lambda *a, **k: calls.append("comment") or True)
    real_run = subprocess.run

    def run(args, **kwargs):
        if gate_failure and args and args[0] == "gate-command":
            if gate_failure == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
            if gate_failure == "missing_receipt":
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 2, "", "checker error")
        if args[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(args, 0, DIFF, "")
        if args[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(args, 1, "", "unavailable")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/test-615/result", json={
            "task_id": "test-615", "status": "completed", "summary": "tests passed",
            "pr_number": 615})
    assert response.status_code == 200, response.text
    return queue, calls


def test_server_block_prevents_status_and_merge(tmp_path, monkeypatch, checker):
    queue, calls = _submit_test(
        tmp_path, monkeypatch,
        title="quota-core context economics analytics tokens_per_outcome")
    assert "status" not in calls and "merge" not in calls
    assert queue.get_task_status("impl-615") == "needs_human"
    record = queue.get_task_context("impl-615")["conformance_gate"]
    assert record["verdict"] == "BLOCK" and record["exit_code"] == 10
    assert queue.external_op_get("merge:pr:615")["state"] == "failed"


def test_server_review_comments_and_merges(tmp_path, monkeypatch, checker):
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                change_type="modify")
    assert calls == ["comment", "status", "merge"]
    record = queue.get_task_context("impl-615")["conformance_gate"]
    assert record["verdict"] == "REVIEW"
    assert Path(record["receipt_path"]).parent == tmp_path / "conformance_receipts"
    assert json.loads(Path(record["receipt_path"]).read_text())["verdict"] == "REVIEW"


def test_server_gate_unset_refuses_merge(tmp_path, monkeypatch):
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                gate_enabled=False)
    assert calls == []
    assert queue.get_task_status("impl-615") == "needs_human"
    assert queue.external_op_get("merge:pr:615")["state"] == "failed"
    assert "conformance gate not configured" in queue.external_op_get("merge:pr:615")["last_error"]


@pytest.mark.parametrize("failure", ["error", "timeout", "missing_receipt"])
def test_server_gate_failure_refuses_merge(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("AGENT_CREW_CONFORMANCE_GATE_CMD", "gate-command")
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                gate_failure=failure)
    assert calls == []
    assert queue.get_task_status("impl-615") == "needs_human"
    assert queue.external_op_get("merge:pr:615")["state"] == "failed"


def test_same_head_retry_reuses_receipt_and_comment(tmp_path, monkeypatch, checker):
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                change_type="modify")
    receipt = queue.get_task_context("impl-615")["conformance_gate"]
    assert receipt["head_sha"] == "a" * 40
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("checker reran for the same head"))

    assert _conformance_gate_allows_merge(
        queue, "impl-615", project="agent_crew", pr_number=615,
        title="add widget", task_desc="add widget", issue=615,
        receipt_dir=str(tmp_path / "conformance_receipts"), repo="owner/repo")
    assert calls == ["comment", "status", "merge"]


@pytest.mark.parametrize("setting", ["0", "false", "no", "off"])
def test_server_auto_merge_opt_out_publishes_status_but_skips_merge(
        tmp_path, monkeypatch, checker, setting):
    monkeypatch.setenv("AGENT_CREW_AUTO_MERGE", setting)
    published = []
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                change_type="modify", status_details=published)
    assert calls == ["comment", "status"]
    assert published == [("owner/repo", "a" * 40, "review-615", "claude")]
    op = queue.external_op_get("merge:pr:615")
    assert op["state"] == "skipped" and op["attempt"] == 0


def test_server_auto_merge_unset_still_merges(tmp_path, monkeypatch, checker):
    monkeypatch.delenv("AGENT_CREW_AUTO_MERGE", raising=False)
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                change_type="modify")
    assert calls == ["comment", "status", "merge"]
    assert queue.external_op_get("merge:pr:615")["state"] == "done"


def test_opt_out_publishes_each_approved_head(tmp_path, monkeypatch, checker):
    monkeypatch.setenv("AGENT_CREW_AUTO_MERGE", "0")
    published = []
    queue, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                                change_type="modify", status_details=published)
    assert queue.external_op_get("merge:pr:615")["state"] == "skipped"

    queue.enqueue(TaskRequest(
        task_id="review-615-r1", task_type="review", branch="fix/615",
        description="review new head", context={"prev_task_id": "impl-615",
            "pr_number": 615, "repo": "owner/repo", "reviewed_sha": "b" * 40,
            "allow_duplicate_review": True}))
    queue.submit_result("review-615-r1", TaskResult(
        "review-615-r1", "completed", "approved new head",
        verdict="approve", pr_number=615))
    queue.enqueue(TaskRequest(
        task_id="test-615-r1", task_type="test", branch="fix/615",
        description="test new head", context={"prev_task_id": "review-615-r1",
            "pr_number": 615, "repo": "owner/repo", "allow_duplicate_review": True}))
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: "b" * 40)
    monkeypatch.setattr(github, "independent_review_for_head",
                        lambda *a, **k: ("b" * 40, "claude", "review-615-r1", "ok"))
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/test-615-r1/result", json={
            "task_id": "test-615-r1", "status": "completed",
            "summary": "tests passed", "pr_number": 615})
    assert response.status_code == 200, response.text
    assert published == [
        ("owner/repo", "a" * 40, "review-615", "claude"),
        ("owner/repo", "b" * 40, "review-615-r1", "claude")]
    assert calls == ["comment", "status", "comment", "status"]
    assert queue.external_op_get("merge:pr:615")["state"] == "skipped"

    monkeypatch.delenv("AGENT_CREW_AUTO_MERGE", raising=False)
    queue.enqueue(TaskRequest(
        task_id="test-615-r1-retry", task_type="test", branch="fix/615",
        description="test new head again", context={"prev_task_id": "review-615-r1",
            "pr_number": 615, "repo": "owner/repo", "allow_duplicate_review": True}))
    with TestClient(app) as client:
        response = client.post("/tasks/test-615-r1-retry/result", json={
            "task_id": "test-615-r1-retry", "status": "completed",
            "summary": "tests passed", "pr_number": 615})
    assert response.status_code == 200, response.text
    assert calls[-2:] == ["status", "merge"]
    assert queue.external_op_get("merge:pr:615")["state"] == "done"
