"""#374 — the completion artifact is checked against the task's declared contract.

Owner ruling alfred#51 c5776940407 §4: not a global relaxation, an explicit
contract — `commit`, `rebase`, `report` (+ `review`); `none` refused.

The rebase cases run real git against a local bare "origin", because #374 is
a statement about ancestry, and a mocked `merge-base` cannot show it.
"""
from __future__ import annotations

import hashlib
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew.pipeline import (
    artifact_gate_applies,
    declared_artifact_kind,
    verify_implement_artifact,
    verify_task_artifact,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _run(cwd, *args):
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                          "-c", "init.defaultBranch=main", *args],
                         cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _commit(repo, name, text):
    (repo / name).write_text(text)
    _run(repo, "add", name)
    _run(repo, "commit", "-q", "-m", name)
    return _run(repo, "rev-parse", "HEAD")


@pytest.fixture
def rebased(tmp_path):
    """#374 case 1, rebuilt: a feature branch dispatched at `base`, then
    rebased by the worker onto a main that moved, and force-pushed.

    `verifier` is the server's checkout: cloned before the rebase, so the
    rewritten commit is not local until something fetches it (#374 case 2).
    """
    origin = tmp_path / "origin.git"
    _run(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _run(tmp_path, "clone", "-q", str(origin), str(work))
    _commit(work, "a.txt", "a")
    _run(work, "push", "-q", "origin", "HEAD:main")
    _run(work, "checkout", "-q", "-b", "feat")
    base = _commit(work, "feat.txt", "feature")
    _run(work, "push", "-q", "origin", "feat")
    verifier = tmp_path / "verifier"
    _run(tmp_path, "clone", "-q", str(origin), str(verifier))

    _run(work, "checkout", "-q", "main")
    main_tip = _commit(work, "b.txt", "main moved")
    _run(work, "push", "-q", "origin", "main")
    _run(work, "checkout", "-q", "feat")
    _run(work, "rebase", "-q", "main")
    rebased_sha = _run(work, "rev-parse", "HEAD")
    _run(work, "push", "-q", "--force", "origin", "feat")
    return {"verifier": str(verifier), "work": work, "base": base,
            "main_tip": main_tip, "commit": rebased_sha}


def _task(context, task_type="implement", branch="feat"):
    return TaskRequest("t-374", task_type, "d", branch=branch, context=context)


def _result(**kw):
    kw.setdefault("summary", "done")
    return TaskResult("t-374", "completed", **kw)


# ---------------------------------------------------------------------------
# The reproduction.
# ---------------------------------------------------------------------------

def test_374_reproduction_commit_gate_rejects_a_correct_rebase(rebased):
    """Unchanged #353 rule: a correct rebase can never pass it."""
    task = _task({"worktree_base_sha": rebased["base"]})
    ok, detail = verify_implement_artifact(
        task, _result(branch="feat", commit=rebased["commit"]), repo_cwd=rebased["verifier"])
    assert (ok, detail) == (False, "reported commit does not descend from the dispatch base")


def test_374_declared_rebase_now_passes(rebased):
    task = _task({"worktree_base_sha": rebased["base"], "artifact_kind": "rebase",
                  "rebase_onto": "main"})
    ok, detail = verify_task_artifact(
        task, _result(branch="feat", commit=rebased["commit"]), repo_cwd=rebased["verifier"])
    assert ok, detail
    assert "rebased onto origin/main" in detail


def test_rebase_derives_the_commit_from_the_pushed_branch(rebased):
    task = _task({"artifact_kind": "rebase", "rebase_onto": "main"})
    result = _result(branch="feat")
    ok, _ = verify_task_artifact(task, result, repo_cwd=rebased["verifier"])
    assert ok and result.commit == rebased["commit"]


@pytest.mark.parametrize(("context", "result_kw", "detail"), [
    ({"artifact_kind": "rebase"}, {}, "rebase contract requires context.rebase_onto"),
    ({"artifact_kind": "rebase", "rebase_onto": "no-such"}, {},
     "rebase target origin/no-such unavailable"),
    ({"artifact_kind": "rebase", "rebase_onto": "--upload-pack=x"}, {},
     "rebase target or branch is not a plain ref name"),
    ({"artifact_kind": "rebase", "rebase_onto": "main"}, {"branch": "main"},
     "rebased branch is the rebase target"),
    ({"artifact_kind": "rebase", "rebase_onto": "main"}, {"branch": "unpushed"},
     "rebased branch origin/unpushed is not pushed"),
])
def test_rebase_contract_fails_closed(rebased, context, result_kw, detail):
    result_kw = {"branch": "feat", "commit": rebased["commit"], **result_kw}
    ok, got = verify_task_artifact(_task(context), _result(**result_kw),
                                   repo_cwd=rebased["verifier"])
    assert (ok, got) == (False, detail)


def test_rebase_that_rewrote_nothing_is_not_an_artifact(rebased):
    task = _task({"artifact_kind": "rebase", "rebase_onto": "main"})
    ok, detail = verify_task_artifact(
        task, _result(branch="feat", commit=rebased["main_tip"]), repo_cwd=rebased["verifier"])
    assert (ok, detail) == (False, "reported commit is the origin/main tip (no artifact)")


def test_rebase_rejects_a_commit_that_is_not_on_the_target(rebased):
    """The pre-rebase commit is still in the object store — but not on main."""
    _run(rebased["verifier"], "fetch", "-q", "origin")
    task = _task({"artifact_kind": "rebase", "rebase_onto": "main"})
    ok, detail = verify_task_artifact(
        task, _result(branch="feat", commit=rebased["base"]), repo_cwd=rebased["verifier"])
    assert not ok
    assert detail in ("reported commit is not on the pushed branch origin/feat",
                      "reported commit does not descend from origin/main")


# ---------------------------------------------------------------------------
# report — #374's third case: a read-only investigation.
# ---------------------------------------------------------------------------

REPORT = "# SEV-0 W2 Duplication Inventory\n\n3 duplicates found.\n"
REPORT_SHA = hashlib.sha256(REPORT.encode()).hexdigest()


def test_374_third_case_report_task_passes_without_a_commit():
    task = _task({"worktree_base_sha": "a" * 40, "artifact_kind": "report",
                  "report_format": "markdown"})
    ok, detail = verify_task_artifact(
        task, _result(artifact={"body": REPORT, "sha256": REPORT_SHA}), repo_cwd="/nonexistent")
    assert ok, detail
    assert REPORT_SHA in detail


def test_the_third_case_still_fails_undeclared():
    """Without a declared contract the default is untouched: no commit, no pass."""
    task = _task({"worktree_base_sha": "a" * 40})
    assert artifact_gate_applies(task, _result())
    ok, _ = verify_task_artifact(task, _result(), repo_cwd="/nonexistent")
    assert not ok


@pytest.mark.parametrize(("context", "artifact", "detail"), [
    ({}, None, "report contract requires result.artifact {body|path, sha256}"),
    ({}, {"body": REPORT}, "report artifact requires a 64-hex sha256"),
    ({}, {"body": REPORT, "sha256": "0" * 64}, "report sha256 does not match its content"),
    ({}, {"body": "   \n", "sha256": hashlib.sha256(b"   \n").hexdigest()}, "report is empty"),
    ({}, {"sha256": REPORT_SHA}, "report artifact requires a non-empty body or a path"),
    ({"report_format": "json"}, {"body": REPORT, "sha256": REPORT_SHA},
     "json report does not parse"),
    ({"report_format": "markdown"},
     {"body": "no heading", "sha256": hashlib.sha256(b"no heading").hexdigest()},
     "markdown report has no heading"),
    ({"report_format": "pdf"}, {"body": REPORT, "sha256": REPORT_SHA},
     "unsupported report_format 'pdf'"),
    ({}, {"path": "../etc/passwd", "sha256": REPORT_SHA},
     "report path requires the commit it was written at"),
])
def test_report_contract_fails_closed(context, artifact, detail):
    task = _task({"artifact_kind": "report", **context})
    assert verify_task_artifact(task, _result(artifact=artifact), repo_cwd="/x") == (False, detail)


def test_report_by_path_is_read_from_git_not_the_filesystem(rebased):
    work = rebased["work"]
    commit = _commit(work, "report.md", REPORT)
    _run(work, "push", "-q", "origin", "feat")
    task = _task({"artifact_kind": "report", "report_format": "markdown"})
    ok, detail = verify_task_artifact(
        task, _result(branch="feat", commit=commit,
                      artifact={"path": "report.md", "sha256": REPORT_SHA}),
        repo_cwd=rebased["verifier"])
    assert ok, detail
    bad = verify_task_artifact(
        task, _result(branch="feat", commit=commit,
                      artifact={"path": "/etc/passwd", "sha256": REPORT_SHA}),
        repo_cwd=rebased["verifier"])
    assert bad == (False, "report path must be repository-relative")


# ---------------------------------------------------------------------------
# review, commit, and undeclared/unsupported.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("result_kw", "context", "expected"), [
    ({"verdict": "approve", "pr_number": 7}, {}, (True, "review verdict approve")),
    ({"verdict": "approve"}, {"pr_number": 7}, (True, "review verdict approve")),
    ({"verdict": "request_changes", "findings": ["fix x"], "pr_number": 7}, {},
     (True, "review verdict request_changes")),
    ({}, {"pr_number": 7}, (False, "review contract requires verdict approve or request_changes")),
    ({"verdict": "approve"}, {}, (False, "review contract requires the reviewed pr_number")),
    ({"verdict": "request_changes", "findings": [" "], "pr_number": 7}, {},
     (False, "request_changes review has no findings")),
])
def test_review_contract(result_kw, context, expected):
    task = _task({"artifact_kind": "review", **context}, task_type="review")
    assert verify_task_artifact(task, _result(**result_kw), repo_cwd="") == expected


def test_declared_commit_uses_the_unchanged_353_verifier():
    seen = []

    def verifier(task, result, *, repo_cwd):
        seen.append(repo_cwd)
        return True, "353"

    task = _task({"artifact_kind": "commit", "worktree_base_sha": "a" * 40})
    assert verify_task_artifact(task, _result(), repo_cwd="/r", commit_verifier=verifier) == (
        True, "353")
    assert seen == ["/r"]


def test_declared_commit_without_a_base_is_refused():
    """Declaring a contract never skips the check the way 'base absent' does."""
    task = _task({"artifact_kind": "commit"})
    assert artifact_gate_applies(task, _result())
    assert verify_task_artifact(task, _result(branch="feat"), repo_cwd="/r") == (
        False, "missing base or branch")


@pytest.mark.parametrize("kind", ["none", "", "Commits", "skip"])
def test_unsupported_or_empty_contract_is_refused(kind):
    task = _task({"artifact_kind": kind, "worktree_base_sha": "a" * 40})
    assert artifact_gate_applies(task, _result())
    ok, detail = verify_task_artifact(task, _result(), repo_cwd="/r")
    assert not ok and detail.startswith("unsupported artifact_kind")


@pytest.mark.parametrize(("task_type", "context", "applies"), [
    ("implement", {"worktree_base_sha": "a" * 40}, True),      # #353, unchanged
    ("implement", {}, False),                                    # #353, unchanged
    ("review", {}, False),                                       # unchanged
    ("review", {"artifact_kind": "review"}, True),               # declared → checked
    ("test", {"artifact_kind": "report"}, True),
])
def test_when_the_gate_applies(task_type, context, applies):
    assert artifact_gate_applies(_task(context, task_type=task_type), _result()) is applies
    failed = TaskResult("t-374", "failed", "x")
    assert artifact_gate_applies(_task(context, task_type=task_type), failed) is False


def test_declared_kind_is_normalised_but_not_guessed():
    assert declared_artifact_kind(_task({})) is None
    assert declared_artifact_kind(_task({"artifact_kind": " Report "})) == "report"
    assert declared_artifact_kind(_task({"artifact_kind": None})) == ""


# ---------------------------------------------------------------------------
# End to end through POST /tasks/{id}/result.
# ---------------------------------------------------------------------------

def _app(tmp_db):
    from agent_crew.server import create_app
    return TestClient(create_app(tmp_db, pane_map={"reviewer": "%crew-test-reviewer"},
                                 watchdog_disabled=True, worktree_map={}))


def _row(queue, task_id):
    return next(t for t in queue.list_all_with_status() if t["task_id"] == task_id)


def test_http_report_task_is_accepted_and_the_artifact_is_pinned(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("w2", "implement", "inventory", branch="main",
                              context={"worktree_base_sha": "a" * 40, "artifact_kind": "report",
                                       "report_format": "markdown"}))
    with _app(tmp_db) as client:
        response = client.post("/tasks/w2/result", json={
            "task_id": "w2", "status": "completed", "summary": "inventory done",
            "artifact": {"body": REPORT, "sha256": REPORT_SHA}})
    assert response.status_code == 200
    assert "held" not in response.json()
    row = _row(queue, "w2")
    assert row["status"] == "completed"
    context = queue.get_task_context("w2")
    assert context["result_artifact"]["kind"] == "report"
    assert REPORT_SHA in context["result_artifact"]["detail"]


def test_http_report_task_with_a_wrong_hash_is_held(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("w2b", "implement", "inventory", branch="main",
                              context={"artifact_kind": "report"}))
    with _app(tmp_db) as client:
        response = client.post("/tasks/w2b/result", json={
            "task_id": "w2b", "status": "completed", "summary": "x",
            "artifact": {"body": REPORT, "sha256": "0" * 64}})
    assert response.json()["held"] == "no_artifact"
    row = _row(queue, "w2b")
    assert row["status"] == "failed"
    assert row["error_info"] == {"reason": "no_artifact",
                                 "detail": "report sha256 does not match its content"}
