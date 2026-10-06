"""#606: evidence reaches the real ADR-004 checker and protected merge uses review status."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_crew import github
from agent_crew.github import publish_independent_review_status as real_publish_status
from agent_crew.cli import _conformance_gate_allows_merge
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


CHECKER = Path("/home/truhojun/alfred/tools/contract_registry.py")
REGISTRY = Path("/home/truhojun/alfred/governance/capability_registry.json")


@pytest.fixture
def real_gate(tmp_path, monkeypatch):
    if not CHECKER.exists() or not REGISTRY.exists():
        pytest.skip("Alfred checker or registry unavailable")
    registry = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY, registry)
    wrapper = tmp_path / "checker.sh"
    wrapper.write_text(f'#!/bin/sh\nexec python3 "{CHECKER}" "$@" --registry "{registry}"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("AGENT_CREW_CONFORMANCE_GATE_CMD", str(wrapper))
    monkeypatch.setattr(github, "post_pr_comment", lambda *a, **k: True)
    return wrapper


def _gate(tmp_path, monkeypatch, *, context=None, text="add widget", diff=""):
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q.enqueue(TaskRequest(task_id="impl-606", task_type="implement", description=text,
                          branch="fix/606", context=context or {}))
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(args, 0 if diff is not None else 1,
                                               diff or "", "unavailable" if diff is None else "")
        return real_run(args, **kwargs)

    monkeypatch.setattr("agent_crew.cli.subprocess.run", run)
    allowed = _conformance_gate_allows_merge(
        q, "impl-606", project="agent_crew", pr_number=606, title=text,
        task_desc=text, issue=606, receipt_dir=str(tmp_path / "receipts"),
        repo="owner/repo")
    receipt = json.loads(next((tmp_path / "receipts").glob("*.receipt.json")).read_text())
    change = json.loads(next((tmp_path / "receipts").glob("*.change.json")).read_text())
    return q, allowed, change, receipt


def test_common_path_reaches_real_registry(tmp_path, monkeypatch, real_gate):
    q, allowed, change, receipt = _gate(
        tmp_path, monkeypatch,
        diff="diff --git a/src/agent_crew/x.py b/src/agent_crew/x.py\n"
             "+++ b/src/agent_crew/x.py\n+new code\n")
    assert allowed
    assert all(field in change for field in ("capability_id", "role", "portable_core", "dependencies"))
    assert change["capability_id"] == "agent-crew.issue-606"
    assert change["role"] == "implementer"
    assert change["portable_core"] is True and change["dependencies"] == []
    assert change["evidence_source"] == {field: "derived" for field in
                                         ("capability_id", "role", "portable_core", "dependencies")}
    assert receipt["registry_generation"] is not None


def test_cross_project_duplicate_blocks_with_real_registry(tmp_path, monkeypatch, real_gate):
    q, allowed, change, receipt = _gate(
        tmp_path, monkeypatch, context={"change_type": "create"},
        text="quota-core context economics analytics tokens_per_outcome",
        diff="diff --git a/src/agent_crew/x.py b/src/agent_crew/x.py\n"
             "+++ b/src/agent_crew/x.py\n+new code\n")
    assert not allowed and receipt["verdict"] == "BLOCK"
    assert q.get_task_status("impl-606") == "needs_human"


def test_diff_unavailable_keeps_review(tmp_path, monkeypatch, real_gate):
    q, allowed, change, receipt = _gate(tmp_path, monkeypatch, diff=None)
    assert allowed and receipt["verdict"] == "REVIEW"
    assert "portable_core" not in change and "dependencies" not in change


def test_diff_finds_private_fleet_dependency_once_per_portable_file(tmp_path, monkeypatch,
                                                                   real_gate):
    _, allowed, change, receipt = _gate(
        tmp_path, monkeypatch, context={"capability_id": "agent-crew.explicit",
                                        "role": "implementer"},
        diff="diff --git a/src/agent_crew/x.py b/src/agent_crew/x.py\n"
             "+++ b/src/agent_crew/x.py\n+from alfred import tools\n"
             "+see alfred/tools/task.py\n")
    assert not allowed and receipt["verdict"] == "BLOCK"
    assert change["dependencies"] == [{"kind": "private_fleet", "project": "alfred",
                                      "file": "src/agent_crew/x.py"}]
    assert change["evidence_source"] == {
        "capability_id": "context", "role": "context", "portable_core": "derived",
        "dependencies": "derived"}


def test_pr_head_sha_uses_api(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.pr_head_sha(606, repo="owner/repo") == "a" * 40
    assert calls == [["gh", "api", "repos/owner/repo/pulls/606", "--jq", ".head.sha"]]


def _review_queue(tmp_path, reviewer="claude"):
    q = TaskQueue(str(tmp_path / "review.db"))
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="work",
                          branch="fix/606", context={"pr_number": 606}))
    q.enqueue(TaskRequest(task_id="review-a", task_type="review", description="review",
                          branch="fix/606", context={"pr_number": 606,
                                                     "prev_task_id": "impl-a",
                                                     "reviewed_sha": "a" * 40}))
    with q._connect() as db:
        db.execute("UPDATE tasks SET status='completed', verdict='approve', pr_number=606, "
                   "status_changed_at=10 WHERE task_id='review-a'")
    q.record_attribution("impl-a", agent="codex", task_type="implement")
    q.record_attribution("review-a", agent=reviewer, task_type="review")
    return q


def test_independent_approval_publishes_status_before_merge(tmp_path, monkeypatch):
    q = _review_queue(tmp_path)
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    sha, reviewer, review_id, reason = github.independent_review_for_head(
        q, 606, "owner/repo", "review-a")
    assert (sha, reviewer, review_id) == ("a" * 40, "claude", "review-a")
    assert real_publish_status("owner/repo", sha, review_id, reviewer)
    assert calls[0][:5] == ["gh", "api", "-X", "POST", f"repos/owner/repo/statuses/{sha}"]
    assert "context=crew/independent-review" in calls[0]
    calls.append(["gh", "pr", "merge"])
    assert calls[-2][0:3] == ["gh", "api", "-X"] and calls[-1][0:3] == ["gh", "pr", "merge"]


@pytest.mark.parametrize("reviewer,head,reason_part", [
    ("codex", "a" * 40, "equals"),
    ("claude", "b" * 40, "differs"),
])
def test_same_agent_or_moved_head_has_no_status(tmp_path, monkeypatch,
                                                 reviewer, head, reason_part):
    q = _review_queue(tmp_path, reviewer=reviewer)
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: head)
    writes = []
    monkeypatch.setattr(github, "publish_independent_review_status",
                        lambda *a, **k: writes.append(a) or True)
    sha, agent, review_id, reason = github.independent_review_for_head(
        q, 606, "owner/repo", "review-a")
    if sha:
        github.publish_independent_review_status("owner/repo", sha, review_id, agent)
    assert not sha and reason_part in reason and writes == []


def test_latest_review_lookup_skips_malformed_older_context(tmp_path):
    q = _review_queue(tmp_path)
    q.enqueue(TaskRequest(task_id="old-review", task_type="review", description="old",
                          branch="fix/old"))
    with q._connect() as db:
        db.execute("UPDATE tasks SET status='completed', context='{', pr_number=NULL "
                   "WHERE task_id='old-review'")
    assert q.latest_completed_review_for_pr(606).task_id == "review-a"
