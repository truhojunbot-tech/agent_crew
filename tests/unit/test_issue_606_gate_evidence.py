"""#606: evidence reaches the real ADR-004 checker and protected merge uses review status."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_crew import github
from agent_crew.cea import wiring
from agent_crew.github import publish_independent_review_status as real_publish_status
from agent_crew.cli import _conformance_gate_allows_merge, _registry_capability_for_paths
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
    monkeypatch.delenv("AGENT_CREW_CAPABILITY_REGISTRY", raising=False)
    monkeypatch.delenv("AGENT_CREW_CEA_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("AGENT_CREW_CEA_CAPABILITY_REGISTRY", raising=False)
    monkeypatch.setattr(github, "post_pr_comment", lambda *a, **k: True)
    return wrapper


def _gate(tmp_path, monkeypatch, *, context=None, text="add widget", diff="",
          issue_body=None):
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q.enqueue(TaskRequest(task_id="impl-606", task_type="implement", description=text,
                          branch="fix/606", context=context or {}))
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(args, 0 if diff is not None else 1,
                                               diff or "", "unavailable" if diff is None else "")
        if args[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(args, 0 if issue_body is not None else 1,
                                               issue_body or "", "unavailable" if issue_body is None else "")
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


@pytest.mark.parametrize("added_lines", [
    pytest.param("self_source", id="detector-source"),
    pytest.param('if ("/alfred/" in added or "alfred/tools" in added or\n'
                 '    "import alfred" in added or "from alfred" in added):',
                 id="original-612-lines"),
    pytest.param('# This change mentions /home/u/alfred/tools/a.py', id="comment"),
])
def test_detector_source_and_comments_are_not_dependencies(tmp_path, monkeypatch,
                                                            real_gate, added_lines):
    if added_lines == "self_source":
        added_lines = (Path(__file__).parents[2] / "src/agent_crew/conformance_gate.py").read_text()
    diff = ("diff --git a/src/agent_crew/conformance_gate.py "
            "b/src/agent_crew/conformance_gate.py\n"
            "+++ b/src/agent_crew/conformance_gate.py\n"
            + "".join("+" + line + "\n" for line in added_lines.splitlines()))
    _, _, change, receipt = _gate(tmp_path, monkeypatch, diff=diff)
    assert change["dependencies"] == []
    assert receipt["reason"] != "PORTABLE_CORE_DEPENDENCY"


@pytest.mark.parametrize("added_line", [
    "from alfred.tools import x",
    'P = "/home/u/alfred/tools/a.py"',
])
def test_real_private_dependency_blocks(tmp_path, monkeypatch, real_gate, added_line):
    diff = ("diff --git a/src/agent_crew/x.py b/src/agent_crew/x.py\n"
            "+++ b/src/agent_crew/x.py\n+" + added_line + "\n")
    _, allowed, change, receipt = _gate(tmp_path, monkeypatch, diff=diff)
    assert change["dependencies"] == [
        {"kind": "private_fleet", "project": "alfred", "file": "src/agent_crew/x.py"}]
    assert not allowed and receipt["verdict"] == "BLOCK"
    assert receipt["reason"] == "PORTABLE_CORE_DEPENDENCY"


def _single_path_registry(tmp_path, monkeypatch, lifecycle="active"):
    registry = tmp_path / "registry.json"
    data = json.loads(registry.read_text())
    record = next(r for r in data["records"]
                  if r["capability_id"] == "agent-crew.durable-attribution-lifecycle-stream")
    record["lifecycle_state"] = lifecycle
    data["records"] = [record]
    registry.write_text(json.dumps(data))
    monkeypatch.setenv("AGENT_CREW_CEA_REGISTRY_PATH", str(registry))
    return record


_CONTEXT_DIFF = ("diff --git a/src/agent_crew/context_identity.py "
                 "b/src/agent_crew/context_identity.py\n"
                 "+++ b/src/agent_crew/context_identity.py\n+improve context identity\n")


def test_registry_path_yields_declared_capability_and_real_allow(tmp_path, monkeypatch,
                                                                 real_gate):
    record = _single_path_registry(tmp_path, monkeypatch)
    _, allowed, change, receipt = _gate(
        tmp_path, monkeypatch, text="Improve durable attribution lifecycle",
        diff=_CONTEXT_DIFF)
    assert change["capability_id"] == record["capability_id"]
    assert change["evidence_source"]["capability_id"] == "registry_path"
    assert "src/agent_crew/context_identity.py" in change["text"]
    assert allowed and receipt["verdict"] == "ALLOW", receipt


def test_registry_path_uses_cea_wiring_default(tmp_path, monkeypatch, real_gate):
    record = _single_path_registry(tmp_path, monkeypatch)
    e8_registry = tmp_path / "e8_registry.json"
    e8_registry.write_text(json.dumps({"generation": 1, "capabilities": []}))
    monkeypatch.setenv("AGENT_CREW_CAPABILITY_REGISTRY", str(e8_registry))
    monkeypatch.delenv("AGENT_CREW_CEA_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("AGENT_CREW_CEA_CAPABILITY_REGISTRY", raising=False)
    monkeypatch.setattr(wiring, "DEFAULT_REGISTRY_PATH", str(tmp_path / "registry.json"))

    _, allowed, change, receipt = _gate(
        tmp_path, monkeypatch, text="Improve durable attribution lifecycle",
        diff=_CONTEXT_DIFF)
    assert change["capability_id"] == record["capability_id"]
    assert change["evidence_source"]["capability_id"] == "registry_path"
    assert allowed and receipt["verdict"] == "ALLOW", receipt


@pytest.mark.parametrize("label", ["Capability", "CAPABILITY_ID"])
def test_issue_declared_capability_takes_precedence(tmp_path, monkeypatch, real_gate,
                                                   label):
    record = _single_path_registry(tmp_path, monkeypatch)
    _, _, change, _ = _gate(
        tmp_path, monkeypatch, issue_body=f"Summary\n{label}: {record['capability_id']}\n",
        diff=_CONTEXT_DIFF)
    assert change["capability_id"] == record["capability_id"]
    assert change["evidence_source"]["capability_id"] == "issue"


def test_withdrawn_registry_path_stays_synthetic_and_review(tmp_path, monkeypatch,
                                                            real_gate):
    _single_path_registry(tmp_path, monkeypatch, lifecycle="withdrawn")
    _, allowed, change, receipt = _gate(tmp_path, monkeypatch, diff=_CONTEXT_DIFF)
    assert change["capability_id"] == "agent-crew.issue-606"
    assert change["evidence_source"]["capability_id"] == "derived"
    assert allowed and receipt["verdict"] == "REVIEW", receipt


def test_registry_env_unset_keeps_synthetic_capability(tmp_path, monkeypatch, real_gate):
    monkeypatch.setattr(wiring, "DEFAULT_REGISTRY_PATH", str(tmp_path / "missing.json"))
    _, _, change, _ = _gate(tmp_path, monkeypatch, diff=_CONTEXT_DIFF)
    assert change["capability_id"] == "agent-crew.issue-606"
    assert change["evidence_source"]["capability_id"] == "derived"


def test_context_capability_precedes_issue_and_registry(tmp_path, monkeypatch, real_gate):
    _single_path_registry(tmp_path, monkeypatch)
    _, _, change, _ = _gate(
        tmp_path, monkeypatch, context={"capability_id": "agent-crew.explicit"},
        issue_body="capability: agent-crew.issue-declared", diff=_CONTEXT_DIFF)
    assert change["capability_id"] == "agent-crew.explicit"
    assert change["evidence_source"]["capability_id"] == "context"


def test_registry_path_score_and_tie(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"records": [
        {"capability_id": "agent-crew.two-files", "owning_project": "agent-crew",
         "lifecycle_state": "active", "implementation_location": "pkg/{a.py,b.py}"},
        {"capability_id": "agent-crew.one-file", "owning_project": "agent_crew",
         "lifecycle_state": "active", "evidence": [{"path": "a.py"}]},
    ]}))
    assert _registry_capability_for_paths(
        "agent_crew", {"src/pkg/a.py", "src/pkg/b.py"}, str(path)) == "agent-crew.two-files"
    assert _registry_capability_for_paths("agent_crew", {"src/pkg/a.py"}, str(path)) == ""


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


def test_independent_approval_publishes_status(tmp_path, monkeypatch):
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
    assert len(calls) == 1


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


def _server_review_fixture(tmp_path, monkeypatch, *, reviewer, head, publish=True,
                           move_after_publication=False):
    """Submit a real HTTP review result through server._auto_merge_pr."""
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    db = str(tmp_path / "server.db")
    q = TaskQueue(db)
    q.enqueue(TaskRequest(task_id="impl-server", task_type="implement", description="work",
                          branch="fix/606", context={"pr_number": 606}))
    q.enqueue(TaskRequest(task_id="review-server", task_type="review", description="review",
                          branch="fix/606", context={"pr_number": 606,
                                                     "repo": "owner/repo",
                                                     "no_tester": True,
                                                     "prev_task_id": "impl-server",
                                                     "reviewed_sha": "a" * 40}))
    q.record_attribution("impl-server", agent="codex", task_type="implement")
    q.record_attribution("review-server", agent=reviewer, task_type="review")
    calls = []
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.conformance_gate._conformance_gate_allows_merge",
                        lambda *a, **k: True)
    heads = iter(["a" * 40, head]) if move_after_publication else None
    monkeypatch.setattr(github, "pr_head_sha",
                        lambda *a, **k: next(heads, head) if heads else head)
    monkeypatch.setattr(github, "post_review_comment", lambda **k: True)
    monkeypatch.setattr(github, "publish_independent_review_status",
                        lambda *a, **k: calls.append("status") or publish)
    monkeypatch.setattr(github, "independent_review_succeeded", lambda *a, **k: True)
    monkeypatch.setattr(github, "head_checks_state", lambda *a, **k: ("none", ""))
    monkeypatch.setattr(github, "merge_pr", lambda *a, **k: calls.append("merge") or True)
    app = create_app(db_path=db, pane_map={}, push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/review-server/result", json={
            "task_id": "review-server", "status": "completed",
            "summary": "Reviewed the PR head and found no blocking changes.",
            "verdict": "approve", "findings": [], "pr_number": 606})
    assert response.status_code == 200, response.text
    return calls, q.external_op_get("merge:pr:606")


@pytest.mark.parametrize("reviewer,head,reason_part", [
    ("codex", "a" * 40, "equals"),
    ("claude", "b" * 40, "differs"),
])
def test_server_refuses_nonindependent_or_moved_review(tmp_path, monkeypatch,
                                                       reviewer, head, reason_part):
    calls, op = _server_review_fixture(
        tmp_path, monkeypatch, reviewer=reviewer, head=head,
        move_after_publication=(head != "a" * 40))
    assert calls == []
    assert op["state"] == "failed" and reason_part in op["last_error"]


def test_server_publish_failure_blocks_merge(tmp_path, monkeypatch):
    calls, op = _server_review_fixture(tmp_path, monkeypatch, reviewer="claude",
                                       head="a" * 40, publish=False)
    assert calls == ["status"]
    assert op["state"] == "failed" and "publish failed" in op["last_error"]
