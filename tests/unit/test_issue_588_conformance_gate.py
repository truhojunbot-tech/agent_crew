"""#588: PRE_MERGE ADR-004 conformance gate, enforced before auto-merge.

The real gate CLI lives in the alfred repo; these tests use a fake command
installed through ``AGENT_CREW_CONFORMANCE_GATE_CMD``.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_crew import cli as cli_module
from agent_crew.cli import _conformance_gate_allows_merge, crew
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

# Fake gate: records its argv, writes a receipt with the requested verdict and
# mimics the real CLI's --enforce exit codes (10 for BLOCK, else 0).
_FAKE_GATE = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GATE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
mode = os.environ["FAKE_GATE_MODE"]
if mode == "crash":
    sys.stderr.write("boom")
    sys.exit(2)
change = json.load(open(args[args.index("--input") + 1]))
if mode == "contract":
    valid = (isinstance(change.get("capability_id"), str)
             and change.get("change_type") in ("create", "reuse", "modify")
             and isinstance(change.get("portable_core"), bool)
             and isinstance(change.get("dependencies"), list)
             and isinstance(change.get("role"), str) and bool(change["role"]))
    mode = "ALLOW" if valid else "REVIEW"
with open(args[args.index("--receipt") + 1], "w") as fh:
    json.dump({"verdict": mode, "change": change}, fh)
sys.exit(10 if mode == "BLOCK" and "--enforce" in args else 0)
'''


@pytest.fixture
def gate(tmp_path, monkeypatch):
    script = tmp_path / "fake_gate.py"
    script.write_text(_FAKE_GATE)
    log = tmp_path / "gate.log"
    monkeypatch.setenv("AGENT_CREW_CONFORMANCE_GATE_CMD", f"{sys.executable} {script}")
    monkeypatch.setenv("FAKE_GATE_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


@pytest.fixture
def comments(monkeypatch):
    posted = []
    monkeypatch.setattr("agent_crew.github.post_pr_comment",
                        lambda pr, body, repo=None: posted.append((pr, body, repo)) or True)
    return posted


def _queue(tmp_path, context=None):
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q.enqueue(TaskRequest(task_id="impl-1", task_type="implement",
                          description="add widget\nmore detail", branch="main",
                          context=context or {}))
    return q


def _gate(q, tmp_path, issue=588):
    return _conformance_gate_allows_merge(
        q, "impl-1", project="proj", pr_number=42, title="add widget",
        task_desc="add widget\nmore detail", issue=issue,
        receipt_dir=str(tmp_path / "receipts"), repo="owner/repo")


def _record(q):
    return q.get_task_context("impl-1")["conformance_gate"]


def test_unset_env_makes_no_call(tmp_path, monkeypatch, comments):
    monkeypatch.delenv("AGENT_CREW_CONFORMANCE_GATE_CMD", raising=False)
    q = _queue(tmp_path)
    called = []
    monkeypatch.setattr(cli_module.subprocess, "run", lambda *a, **k: called.append(a))

    assert _gate(q, tmp_path) is True
    assert called == [] and comments == []
    assert "conformance_gate" not in q.get_task_context("impl-1")
    assert not (tmp_path / "receipts").exists()


def test_allow_merges_and_records_receipt(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "ALLOW")
    q = _queue(tmp_path, {"capability_id": "cap.widget"})

    assert _gate(q, tmp_path) is True

    (argv,) = gate()
    assert argv[0] == "capability-conformance-check" and argv[-1] == "--enforce"
    rec = _record(q)
    assert rec["verdict"] == "ALLOW" and rec["merge_allowed"]
    with open(rec["receipt_path"], "rb") as fh:
        body = fh.read()
    assert rec["receipt_sha256"] == hashlib.sha256(body).hexdigest()
    change = json.loads(body)["change"]
    assert change["stage"] == "PRE_MERGE" and change["project"] == "proj"
    assert change["capability_id"] == "cap.widget"
    assert change["owner_evidence"] == {"project": "proj", "ref": "issue#588"}
    assert change["text"].startswith("add widget")
    assert change["change_type"] == "modify"
    assert comments == []
    assert q.get_task_status("impl-1") == "pending"


def test_declared_change_evidence_reaches_gate_contract(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "contract")
    q = _queue(tmp_path, {"capability_id": "cap.widget", "change_type": "create",
                          "portable_core": False, "dependencies": [], "role": "worker"})

    assert _gate(q, tmp_path) is True
    rec = _record(q)
    assert rec["verdict"] == "ALLOW"
    change = json.loads(Path(rec["receipt_path"]).read_text())["change"]
    assert change["portable_core"] is False
    assert change["dependencies"] == []
    assert change["role"] == "worker"
    assert change["change_type"] == "create"


def test_missing_change_evidence_is_not_invented(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "contract")
    q = _queue(tmp_path, {"capability_id": "cap.widget"})

    assert _gate(q, tmp_path) is True
    rec = _record(q)
    assert rec["verdict"] == "REVIEW"
    change = json.loads(Path(rec["receipt_path"]).read_text())["change"]
    assert "portable_core" not in change
    assert "dependencies" not in change
    assert "role" not in change


def test_review_merges_records_receipt_and_comments(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "REVIEW")
    q = _queue(tmp_path)

    assert _gate(q, tmp_path, issue=None) is True

    rec = _record(q)
    assert rec["verdict"] == "REVIEW" and rec["merge_allowed"]
    change = json.load(open(rec["receipt_path"]))["change"]
    assert "capability_id" not in change
    assert change["owner_evidence"]["ref"] == "pr#42"
    ((pr, body, repo),) = comments
    assert pr == 42 and repo == "owner/repo"
    assert "REVIEW" in body and rec["receipt_sha256"] in body


@pytest.mark.parametrize("mode", ["crash", "EVIDENCE_UNAVAILABLE"])
def test_gate_error_or_unavailable_is_review_and_merges(tmp_path, monkeypatch, gate, comments, mode):
    monkeypatch.setenv("FAKE_GATE_MODE", mode)
    q = _queue(tmp_path)

    assert _gate(q, tmp_path) is True
    rec = _record(q)
    assert rec["verdict"] == "REVIEW" and rec["error"]
    assert len(comments) == 1


def test_gate_timeout_is_review(tmp_path, monkeypatch, gate, comments):
    q = _queue(tmp_path)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setattr(cli_module.subprocess, "run", _timeout)

    assert _gate(q, tmp_path) is True
    rec = _record(q)
    assert rec["verdict"] == "REVIEW" and "timeout" in rec["error"]
    assert json.load(open(rec["receipt_path"]))["verdict"] == "REVIEW"


def test_malformed_command_is_review_and_merges(tmp_path, monkeypatch, comments):
    monkeypatch.setenv("AGENT_CREW_CONFORMANCE_GATE_CMD", "'")
    merges, q, impl_id, _ = _run_auto_merge(tmp_path, monkeypatch)
    assert len(merges) == 1
    rec = q.get_task_context(impl_id)["conformance_gate"]
    assert rec["verdict"] == "REVIEW"
    assert "No closing quotation" in rec["error"]
    assert Path(rec["receipt_path"]).exists()
    assert len(comments) == 1


def test_unwritable_receipt_location_is_review_and_merges(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "ALLOW")
    q = _queue(tmp_path)
    blocked_path = tmp_path / "file-instead-of-directory"
    blocked_path.write_text("occupied")

    assert _conformance_gate_allows_merge(
        q, "impl-1", project="proj", pr_number=42, title="add widget",
        task_desc="add widget", issue=588, receipt_dir=str(blocked_path),
        repo="owner/repo") is True
    rec = _record(q)
    assert rec["verdict"] == "REVIEW"
    assert Path(rec["receipt_path"]).exists()
    assert len(comments) == 1
    assert gate() == []


def test_block_does_not_merge_and_needs_human(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "BLOCK")
    q = _queue(tmp_path)

    assert _gate(q, tmp_path) is False
    rec = _record(q)
    assert rec["verdict"] == "BLOCK" and rec["exit_code"] == 10 and not rec["merge_allowed"]
    assert q.get_task_status("impl-1") == "needs_human"
    assert rec["receipt_path"] in q.get_result("impl-1").summary


# --- end to end through `crew run --auto-merge` -----------------------------

def _run_auto_merge(tmp_path, monkeypatch):
    def fake_result(_q, task_id):
        if task_id.startswith("review"):
            return TaskResult(task_id=task_id, status="completed", summary="ok",
                              verdict="approve", findings=[])
        return TaskResult(task_id=task_id, status="completed", summary="done",
                          pr_number=42)
    monkeypatch.setattr(TaskQueue, "get_result", fake_result)
    monkeypatch.setattr("agent_crew.github.check_gh_installed", lambda: True)
    merges = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd[:3] == ["gh", "pr", "merge"]:
            merges.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    db = tmp_path / "tasks.db"
    result = CliRunner().invoke(crew, [
        "run", "add widget", "--db", str(db), "--no-tester", "--auto-merge",
        "--repo", "owner/repo", "--issue", "588"])
    assert result.exit_code == 0, result.output
    q = TaskQueue(str(db))
    impl = next(t for t in q.list_tasks() if t.task_type == "implement")
    return merges, q, impl.task_id, result.output


@pytest.mark.parametrize("verdict", ["ALLOW", "REVIEW"])
def test_crew_run_allow_or_review_merges(tmp_path, monkeypatch, gate, comments, verdict):
    monkeypatch.setenv("FAKE_GATE_MODE", verdict)
    merges, q, impl_id, _ = _run_auto_merge(tmp_path, monkeypatch)
    assert len(merges) == 1
    assert q.get_task_context(impl_id)["conformance_gate"]["verdict"] == verdict
    assert len(comments) == (1 if verdict == "REVIEW" else 0)


def test_crew_run_block_does_not_merge(tmp_path, monkeypatch, gate, comments):
    monkeypatch.setenv("FAKE_GATE_MODE", "BLOCK")
    merges, q, impl_id, out = _run_auto_merge(tmp_path, monkeypatch)
    assert merges == []
    assert q.get_task_status(impl_id) == "needs_human"
    assert q.get_task_context(impl_id)["conformance_gate"]["verdict"] == "BLOCK"
    assert "not merging PR #42" in out


def test_crew_run_without_gate_env_merges_without_call(tmp_path, monkeypatch, comments):
    monkeypatch.delenv("AGENT_CREW_CONFORMANCE_GATE_CMD", raising=False)
    merges, q, impl_id, _ = _run_auto_merge(tmp_path, monkeypatch)
    assert len(merges) == 1
    assert "conformance_gate" not in q.get_task_context(impl_id)
    assert comments == []
