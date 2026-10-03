"""Missing implement refs need an actionable hold and a revisable result."""

import types
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted


BASE = "a" * 40
NEW = "b" * 40
ADVANCED = "c" * 40
DETAIL = "result named no commit, branch or pr_number; task branch 'main' is still at the dispatch base"


def _git(argv, **_kwargs):
    if argv[3] == "rev-parse":
        sha = NEW if "origin/feature" in argv[-1] or argv[-1].startswith(NEW) else BASE
        return MagicMock(returncode=0, stdout=sha + "\n", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


def test_missing_refs_have_distinct_detail(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", _git)
    task = TaskRequest("impl-missing", "implement", "change", branch="main",
                       context={"worktree_base_sha": BASE})
    ok, detail = verify_implement_artifact(
        task, TaskResult("impl-missing", "completed", "done"), repo_cwd="/repo")
    assert (ok, detail) == (False, DETAIL)


def test_advanced_base_branch_without_reported_refs_is_held(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    calls = []

    def advanced_main(argv, **kwargs):
        calls.append(argv)
        if argv[3] == "rev-parse":
            return MagicMock(returncode=0, stdout=ADVANCED + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", advanced_main)
    task = TaskRequest("impl-advanced", "implement", "change", branch="main",
                       context={"worktree_base_sha": BASE, "base_branch": "main",
                                "crew_run_branch": False})
    result = TaskResult("impl-advanced", "completed", "work on another PR")
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert detail.startswith("result named no commit, branch or pr_number")
    assert "main" in detail
    assert result.commit == "", "the unrelated origin/main head must not become artifact evidence"
    assert not any(call[3] == "merge-base" for call in calls)


def test_unavailable_base_branch_without_reported_refs_still_asks_for_refs(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run",
                        lambda *_a, **_k: MagicMock(returncode=1, stdout="", stderr="offline"))
    task = TaskRequest("impl-offline", "implement", "change", branch="main",
                       context={"worktree_base_sha": BASE, "base_branch": "main"})
    ok, detail = verify_implement_artifact(
        task, TaskResult("impl-offline", "completed", "done"), repo_cwd="/repo")
    assert ok is False
    assert detail.startswith("result named no commit, branch or pr_number")


@pytest.mark.parametrize("context", [
    {"worktree_base_sha": BASE, "base_branch": "main", "crew_run_branch": True},
    {"worktree_base_sha": BASE, "base_branch": "main", "crew_run_branch": False},
])
def test_non_base_task_branch_can_still_derive_origin_head(monkeypatch, context):
    from agent_crew.pipeline import verify_implement_artifact

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", _git)
    task = TaskRequest("impl-feature", "implement", "change", branch="feature",
                       context=context)
    result = TaskResult("impl-feature", "completed", "done")
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")
    assert ok is True, detail
    assert result.commit == NEW


def test_explicit_pr_branch_and_commit_still_pass(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", _git)
    task = TaskRequest("impl-reported", "implement", "change", branch="main",
                       context={"worktree_base_sha": BASE, "base_branch": "main"})
    result = TaskResult("impl-reported", "completed", "done",
                        branch="feature", commit=NEW, pr_number=5810)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")
    assert ok is True, detail


def test_explicit_base_commit_keeps_existing_detail(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", _git)
    task = TaskRequest("impl-base", "implement", "change", branch="main",
                       context={"worktree_base_sha": BASE})
    ok, detail = verify_implement_artifact(
        task, TaskResult("impl-base", "completed", "done", commit=BASE),
        repo_cwd="/repo")
    assert (ok, detail) == (False, "reported commit is the dispatch base (no new artifact)")


@pytest.fixture
def hermetic_cea(monkeypatch):
    from agent_crew.cea import signed_receipt, wiring

    original = TaskQueue.__init__

    def queue_init(self, db_path, **kwargs):
        kwargs["cea_providers"] = dict(WIRED)
        original(self, db_path, **kwargs)

    monkeypatch.setattr(TaskQueue, "__init__", queue_init)
    monkeypatch.setattr(wiring, "install_from_env", lambda *a, **k: types.SimpleNamespace(
        providers=dict(WIRED), authority=None, mode="shadow", statuses=()))
    monkeypatch.setattr(signed_receipt, "load_public", lambda *_: object())
    monkeypatch.setattr(signed_receipt, "verify", lambda *_a, task_id, **_k: (True, task_id))


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("advanced_base", [False, True])
def test_held_missing_ref_can_be_corrected_by_same_worker(
        tmp_path, monkeypatch, hermetic_cea, mode, advanced_base):
    from agent_crew.server import create_app

    def git_head(argv, **kwargs):
        if advanced_base and argv[3] == "rev-parse" and argv[-1] == "origin/main^{commit}":
            return MagicMock(returncode=0, stdout=ADVANCED + "\n", stderr="")
        return _git(argv, **kwargs)

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", git_head)
    monkeypatch.setattr("agent_crew.pipeline.pr_is_actionable",
                        lambda *_a, **_k: (True, "open"))
    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-missing", "implement", "change", branch="main",
                              project="demo", context=admitted({
                                  "worktree_base_sha": BASE, "base_branch": "main",
                                  "crew_run_branch": False})))
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", mode)
    app = create_app(db, pane_map={"reviewer": "%crew-test-reviewer"},
                     worktree_map={"implementer": str(worktree)},
                     watchdog_disabled=True, anomaly_disabled=True,
                     push_fn=lambda *_a, **_k: None)
    with TestClient(app) as client:
        claim = client.get("/tasks/next", params={"role": "implementer", "agent": "claude"})
        assert claim.status_code == 200, claim.text
        nonce = claim.json()["dispatch_nonce"]
        started = client.post("/tasks/impl-missing/start",
                              json={"nonce": nonce, "presenter": "claude"})
        assert started.json()["go"] is True, started.text
        body = {"task_id": "impl-missing", "status": "completed", "summary": "done",
                "executor_binding": {"nonce": nonce, "presenter": "claude"}}
        held = client.post("/tasks/impl-missing/result", json=body)
        assert held.status_code == 200, held.text
        held_detail = (DETAIL if not advanced_base else
                       "result named no commit, branch or pr_number; "
                       "task branch 'main' is the dispatch base branch")
        assert held.json() == {
            "status": "ok", "task_id": "impl-missing", "held": "no_artifact",
            "reason": "no_artifact", "detail": held_detail,
            "missing": ["branch", "commit", "pr_number"],
            "resend": "POST the result again with branch, full commit SHA and pr_number",
        }
        assert next(t for t in queue.list_tasks() if t.task_id == "impl-missing").status == "failed"
        assert not [t for t in queue.list_tasks() if t.task_type == "review"]
        if mode == "enforce":
            wrong_worker = client.post("/tasks/impl-missing/result", json={
                **body, "branch": "feature", "commit": NEW, "pr_number": 5806,
                "executor_binding": {"nonce": "different", "presenter": "claude"},
            })
            assert wrong_worker.status_code == 409
        corrected = client.post("/tasks/impl-missing/result", json={
            **body, "branch": "feature", "commit": NEW, "pr_number": 5806})
        assert corrected.status_code == 200, corrected.text
        assert corrected.json().get("held") is None, corrected.text
    assert next(t for t in queue.list_tasks() if t.task_id == "impl-missing").status == "completed"
    reviews = [t for t in queue.list_tasks() if t.task_type == "review"]
    if mode == "shadow":
        assert len(reviews) == 1
        assert reviews[0].branch == "feature"
        assert reviews[0].context["reviewed_sha"] == NEW
    else:
        # This fixture's CEA snapshot cannot admit a new review intent; the
        # corrected implement result itself must still be accepted in enforce.
        assert not reviews


def test_other_no_artifact_receipt_stays_consumed_under_enforce(
        tmp_path, monkeypatch, hermetic_cea):
    from agent_crew.server import create_app

    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-base", "implement", "change", branch="main",
                              project="demo", context=admitted({"worktree_base_sha": BASE})))
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", "enforce")
    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", _git)
    app = create_app(db, worktree_map={"implementer": str(worktree)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        claim = client.get("/tasks/next", params={"role": "implementer", "agent": "claude"})
        nonce = claim.json()["dispatch_nonce"]
        start = client.post("/tasks/impl-base/start",
                            json={"nonce": nonce, "presenter": "claude"})
        assert start.json()["go"] is True
        body = {"task_id": "impl-base", "status": "completed", "summary": "done",
                "commit": BASE,
                "executor_binding": {"nonce": nonce, "presenter": "claude"}}
        held = client.post("/tasks/impl-base/result", json=body)
        assert held.status_code == 200
        assert held.json()["detail"] == "reported commit is the dispatch base (no new artifact)"
        corrected = client.post("/tasks/impl-base/result", json={
            **body, "branch": "feature", "commit": NEW, "pr_number": 5806})
        assert corrected.status_code == 409
        assert "RECEIPT_CONSUMED" in corrected.text
    assert next(t for t in queue.list_tasks() if t.task_id == "impl-base").status == "failed"


def test_other_no_artifact_hold_does_not_request_missing_refs(tmp_path, monkeypatch):
    from agent_crew.server import create_app

    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-base", "implement", "change", branch="main",
                              context={"worktree_base_sha": BASE}))
    monkeypatch.setattr("agent_crew.server.verify_implement_artifact",
                        lambda *_a, **_k: (False, "reported commit is the dispatch base (no new artifact)"))
    app = create_app(db, watchdog_disabled=True, anomaly_disabled=True, worktree_map={})
    with TestClient(app) as client:
        response = client.post("/tasks/impl-base/result", json={
            "task_id": "impl-base", "status": "completed", "summary": "done", "commit": BASE})
    assert response.status_code == 200, response.text
    assert response.json()["held"] == "no_artifact"
    assert "missing" not in response.json()
    assert "resend" not in response.json()


def test_generated_implement_protocol_requires_structured_refs():
    from agent_crew.instructions import generate

    protocol = generate("implementer", "agent_crew", 8105, agent="codex")
    assert "are **REQUIRED**" in protocol
    assert "`branch`, full pushed `commit`" in protocol
    assert "`pr_number`" in protocol
    assert "resend the result for the same task" in protocol
