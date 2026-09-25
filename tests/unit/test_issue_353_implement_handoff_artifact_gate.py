"""#353 — an implementation summary is not a reviewable artifact."""
from __future__ import annotations

from unittest.mock import MagicMock
import asyncio
import subprocess

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


#: A pane id tmux can never hand out — real ids are "%" + digits. Hardcoding
#: a plausible one ("%91") meant the dispatcher pushed this fixture into a
#: developer's live pane; see conftest's tmux injection guard.
UNREACHABLE_PANE = "%crew-test-reviewer"

SHA_BASE = "a" * 40
SHA_NEW = "b" * 40
SHA_TARGET = "c" * 40


def _app(tmp_db, monkeypatch, artifact_ok):
    from agent_crew.server import create_app

    monkeypatch.setattr("agent_crew.server.verify_implement_artifact",
                        lambda *_args, **_kwargs: (artifact_ok, "test evidence"))
    return TestClient(create_app(tmp_db, pane_map={"reviewer": UNREACHABLE_PANE},
                                 watchdog_disabled=True, worktree_map={}))


def _implement(queue, task_id="impl-353"):
    queue.enqueue(TaskRequest(task_id, "implement", "change", branch="feat/work",
                              context={"worktree_base_sha": SHA_BASE}))


def test_completed_implement_without_commit_becomes_no_artifact_and_never_dispatches_review(
        tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    _implement(queue)
    with _app(tmp_db, monkeypatch, artifact_ok=False) as client:
        response = client.post("/tasks/impl-353/result", json={
            "task_id": "impl-353", "status": "completed", "summary": "tests green",
        })

    assert response.status_code == 200
    assert response.json()["held"] == "no_artifact"
    row = next(task for task in queue.list_all_with_status() if task["task_id"] == "impl-353")
    assert row["status"] == "failed"
    assert row["error_info"]["reason"] == "no_artifact"
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]


def test_commit_not_present_on_origin_is_no_artifact(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    _implement(queue, "impl-unpushed")
    with _app(tmp_db, monkeypatch, artifact_ok=False) as client:
        response = client.post("/tasks/impl-unpushed/result", json={
            "task_id": "impl-unpushed", "status": "completed", "summary": "done",
            "branch": "feat/work", "commit": SHA_NEW,
        })

    assert response.json()["held"] == "no_artifact"
    row = next(task for task in queue.list_all_with_status() if task["task_id"] == "impl-unpushed")
    assert row["error_info"]["reason"] == "no_artifact"
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]


def test_artifact_verifier_requires_new_commit_and_origin_reachability(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-artifact", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-artifact", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)
    calls = []

    def git_ok(argv, **_kwargs):
        calls.append(argv)
        verb = argv[3]
        if verb == "rev-parse":
            return MagicMock(returncode=0, stdout=SHA_NEW + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", git_ok)
    ok, reason = verify_implement_artifact(task, result, repo_cwd="/repo")
    assert ok is True
    assert "origin" in reason
    assert any("fetch" in call for call in calls)
    assert any("merge-base" in call for call in calls)


def _rebase_git(argv, **_kwargs):
    """Model a pushed branch whose ancestry is independent of dispatch base."""
    if argv[3] == "rev-parse":
        target = argv[-1]
        if target == "origin/main^{commit}":
            return MagicMock(returncode=0, stdout=SHA_TARGET + "\n", stderr="")
        return MagicMock(returncode=0, stdout=SHA_NEW + "\n", stderr="")
    if argv[3] == "merge-base":
        ancestor, descendant = argv[-2:]
        return MagicMock(returncode=0 if (ancestor, descendant) in {
            (SHA_TARGET, SHA_NEW), (SHA_NEW, "origin/feat/work")
        } else 1, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


def test_rebase_artifact_descending_from_declared_origin_target_passes(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-rebase", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE, "rebase_onto": "main"})
    result = TaskResult("impl-rebase", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return _rebase_git(argv, **kwargs)

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", run)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is True, detail
    assert any(call[3] == "fetch" and any(
        "refs/heads/main:refs/remotes/origin/main" in arg for arg in call
    ) for call in calls)
    assert any(call[-1] == "origin/main^{commit}" for call in calls)
    assert any(call[-2:] == [SHA_TARGET, SHA_NEW] for call in calls)


def test_rebase_artifact_descending_from_neither_anchor_is_rejected(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-unrelated", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE, "rebase_onto": "main"})
    result = TaskResult("impl-unrelated", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)

    def unrelated(argv, **kwargs):
        if argv[3] == "merge-base" and argv[-2:] == [SHA_TARGET, SHA_NEW]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return _rebase_git(argv, **kwargs)

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", unrelated)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "rebase" in detail


def test_unresolvable_rebase_target_fails_closed(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-bad-target", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE, "rebase_onto": "missing"})
    result = TaskResult("impl-bad-target", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)

    def missing_target(argv, **kwargs):
        if argv[3] == "fetch" and any("missing" in arg for arg in argv):
            return MagicMock(returncode=1, stdout="", stderr="unknown ref")
        return _rebase_git(argv, **kwargs)

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", missing_target)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "rebase_onto" in detail


def test_artifact_without_rebase_target_still_requires_dispatch_base(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-no-rebase", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-no-rebase", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return _rebase_git(argv, **kwargs)

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", run)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "dispatch base" in detail
    assert not any(call[3] == "fetch" and "main" in call for call in calls)


def test_rebase_target_without_dispatch_base_still_runs_http_artifact_gate(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-rebase-only", "implement", "change",
                              branch="feat/work", context={"rebase_onto": "main"}))

    with _app(tmp_db, monkeypatch, artifact_ok=False) as client:
        response = client.post("/tasks/impl-rebase-only/result", json={
            "task_id": "impl-rebase-only", "status": "completed", "summary": "done",
            "branch": "feat/work", "commit": SHA_NEW,
        })

    assert response.status_code == 200
    assert response.json()["held"] == "no_artifact"
    row = next(task for task in queue.list_all_with_status()
               if task["task_id"] == "impl-rebase-only")
    assert row["error_info"]["reason"] == "no_artifact"


def test_rebase_target_itself_is_not_a_new_artifact(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-target-only", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE, "rebase_onto": "main"})
    result = TaskResult("impl-target-only", "completed", "done",
                        branch="feat/work", commit=SHA_TARGET)
    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", _rebase_git)

    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "no new artifact" in detail


def test_rebase_artifact_still_requires_pushed_commit(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-unpushed-rebase", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE, "rebase_onto": "main"})
    result = TaskResult("impl-unpushed-rebase", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)

    def unpushed(argv, **kwargs):
        if argv[3] == "merge-base" and argv[-2:] == [SHA_NEW, "origin/feat/work"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return _rebase_git(argv, **kwargs)

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", unpushed)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "not reachable" in detail


def test_rebase_artifact_accepts_real_pushed_commit_on_declared_target(tmp_path):
    from agent_crew.pipeline import verify_implement_artifact

    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              text=True, capture_output=True).stdout.strip()

    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.email", "crew@example.test")
    git(clone, "config", "user.name", "Crew Test")
    (clone / "base.txt").write_text("base\n")
    git(clone, "add", "base.txt")
    git(clone, "commit", "-m", "base")
    git(clone, "branch", "-M", "main")
    git(clone, "push", "-u", "origin", "main")

    git(clone, "switch", "-c", "dispatch-base")
    (clone / "dispatch.txt").write_text("old task base\n")
    git(clone, "add", "dispatch.txt")
    git(clone, "commit", "-m", "dispatch base")
    dispatch_base = git(clone, "rev-parse", "HEAD")

    git(clone, "switch", "main")
    git(clone, "switch", "-c", "feat/work")
    (clone / "feature.txt").write_text("rebased artifact\n")
    git(clone, "add", "feature.txt")
    git(clone, "commit", "-m", "feature")
    commit = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "-u", "origin", "feat/work")

    task = TaskRequest("impl-real-rebase", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": dispatch_base, "rebase_onto": "main"})
    result = TaskResult("impl-real-rebase", "completed", "done",
                        branch="feat/work", commit=commit)

    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", dispatch_base, commit],
        cwd=clone, capture_output=True,
    ).returncode == 1
    ok, detail = verify_implement_artifact(task, result, repo_cwd=str(clone))
    assert ok is True, detail


def test_artifact_verifier_rejects_commit_that_was_not_pushed(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-unpushed-proof", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-unpushed-proof", "completed", "done",
                        branch="feat/work", commit=SHA_NEW)

    def unpushed(argv, **_kwargs):
        if "fetch" in argv:
            return MagicMock(returncode=1, stdout="", stderr="remote rejected")
        return MagicMock(returncode=0, stdout=SHA_NEW + "\n", stderr="")

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", unpushed)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")
    assert ok is False
    assert "origin" in detail


def test_artifact_verifier_derives_and_records_origin_branch_head_when_worker_omits_commit(monkeypatch):
    """#353: a pushed artifact remains auditable when the POST omits commit."""
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-derived", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-derived", "completed", "done", branch="feat/work")
    calls = []

    def git_origin_head(argv, **_kwargs):
        calls.append(argv)
        if "fetch" in argv:
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[3] == "rev-parse":
            target = argv[-1]
            if target == "origin/feat/work^{commit}":
                return MagicMock(returncode=0, stdout=SHA_NEW + "\n", stderr="")
            if target == f"{SHA_NEW}^{{commit}}":
                return MagicMock(returncode=0, stdout=SHA_NEW + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", git_origin_head)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is True
    assert "derived" in detail
    assert result.commit == SHA_NEW
    assert any(call[-1] == "origin/feat/work^{commit}" for call in calls)


def test_http_result_persists_the_server_derived_commit_for_audit(tmp_db, monkeypatch):
    """The verifier mutation must survive the result write, not just memory."""
    from agent_crew.server import create_app

    queue = TaskQueue(tmp_db)
    _implement(queue, "impl-derived-http")

    def derives(_task, result, *, repo_cwd):
        result.commit = SHA_NEW
        return True, "origin branch contains derived commit"

    monkeypatch.setattr("agent_crew.server.verify_implement_artifact", derives)
    with TestClient(create_app(tmp_db, pane_map={"reviewer": UNREACHABLE_PANE},
                               watchdog_disabled=True, worktree_map={})) as client:
        response = client.post("/tasks/impl-derived-http/result", json={
            "task_id": "impl-derived-http", "status": "completed", "summary": "done",
            "branch": "feat/work",
        })

    assert response.status_code == 200
    stored = next(task for task in queue.list_tasks() if task.task_id == "impl-derived-http")
    assert stored.context["result_commit"] == SHA_NEW


def test_artifact_verifier_rejects_missing_commit_when_origin_head_is_dispatch_base(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-no-new", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-no-new", "completed", "done", branch="feat/work")

    def head_is_base(argv, **_kwargs):
        if "fetch" in argv:
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[3] == "rev-parse":
            return MagicMock(returncode=0, stdout=SHA_BASE + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", head_is_base)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "dispatch base" in detail
    assert result.commit == SHA_BASE


def test_artifact_verifier_rejects_missing_commit_when_origin_branch_is_absent(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-no-branch", "implement", "x", branch="feat/missing",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-no-branch", "completed", "done", branch="feat/missing")

    def no_origin_branch(argv, **_kwargs):
        if "fetch" in argv:
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[3] == "rev-parse":
            return MagicMock(returncode=1, stdout="", stderr="unknown revision")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.pipeline.subprocess.run", no_origin_branch)
    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "origin branch head" in detail
    assert result.commit == ""


def test_artifact_verifier_rejects_missing_commit_when_origin_is_unavailable(monkeypatch):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-no-origin", "implement", "x", branch="feat/work",
                       context={"worktree_base_sha": SHA_BASE})
    result = TaskResult("impl-no-origin", "completed", "done", branch="feat/work")
    monkeypatch.setattr(
        "agent_crew.pipeline.subprocess.run",
        lambda argv, **_kwargs: MagicMock(returncode=1, stdout="", stderr="offline")
        if "fetch" in argv else MagicMock(returncode=0, stdout="", stderr=""),
    )

    ok, detail = verify_implement_artifact(task, result, repo_cwd="/repo")

    assert ok is False
    assert "origin" in detail
    assert result.commit == ""


def test_generated_implementer_protocol_requires_structured_branch_and_commit():
    from agent_crew import instructions

    for role in ("implementer", "reviewer", "tester"):
        for delivery in ("both", "mcp", "dispatcher"):
            protocol = instructions.generate(
                role, "demo", 8105, agent="codex", delivery=delivery,
            )
            assert "<branch-name>" in protocol
            assert "<full-commit-sha>" in protocol


def test_sync_preserves_protocol_files_and_uses_project_base(monkeypatch, tmp_path):
    from agent_crew.cli import _sync_worktrees_to_main

    wt = tmp_path / "codex"
    wt.mkdir()
    calls = []
    monkeypatch.setattr("agent_crew.cli.subprocess.run",
                        lambda argv, **_kwargs: calls.append(argv) or MagicMock(returncode=0))

    _sync_worktrees_to_main({"implementer": str(wt)}, base_branch="feat/exec-engine")

    stash = next(call for call in calls if "stash" in call)
    checkout = next(call for call in calls if "checkout" in call)
    assert any(item.endswith(".claude/CLAUDE.md") for item in stash)
    assert any(item.endswith("AGENTS.md") for item in stash)
    assert any(item.endswith("GEMINI.md") for item in stash)
    assert any(item.endswith(".gemini/settings.json") for item in stash)
    assert checkout[-1] == "origin/feat/exec-engine"


def test_dispatch_regenerates_a_missing_role_protocol(tmp_path):
    from agent_crew.server import _ensure_role_protocol

    port_file = tmp_path / "port"
    port_file.write_text("8105\n")
    assert _ensure_role_protocol(
        "implementer", str(tmp_path), "demo", str(port_file), agent="claude",
    )
    assert (tmp_path / ".claude" / "CLAUDE.md").is_file()


def test_mcp_accepts_a_verified_artifact_from_its_worker_checkout(tmp_db, monkeypatch):
    """MCP runs in the worker worktree, not in the HTTP server process (#353)."""
    from agent_crew.mcp_server import build_mcp_server

    queue = TaskQueue(tmp_db)
    _implement(queue, "impl-mcp-artifact")
    assert queue.dequeue(role="implementer") is not None
    seen = []

    def verified(_task, _result, *, repo_cwd):
        seen.append(repo_cwd)
        return True, "origin branch contains reported commit"

    monkeypatch.setattr("agent_crew.mcp_server.verify_implement_artifact", verified)
    monkeypatch.setattr("agent_crew.mcp_server.os.getcwd", lambda: "/worker-checkout")
    tool = build_mcp_server(tmp_db)._tool_manager._tools["submit_result"].fn
    result = tool(task_id="impl-mcp-artifact", status="completed", summary="done",
                  branch="feat/work", commit=SHA_NEW)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)

    assert result["acknowledged"] is True
    assert seen == ["/worker-checkout"]
    assert any(t.task_type == "review" for t in queue.list_tasks())


def test_mcp_rebase_target_without_dispatch_base_runs_artifact_gate(tmp_db, monkeypatch):
    from agent_crew.mcp_server import build_mcp_server

    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-mcp-rebase-only", "implement", "change",
                              branch="feat/work", context={"rebase_onto": "main"}))
    assert queue.dequeue(role="implementer") is not None
    seen = []

    def rejected(task, _result, *, repo_cwd):
        seen.append((task.context["rebase_onto"], repo_cwd))
        return False, "rebase_onto target unavailable"

    monkeypatch.setattr("agent_crew.mcp_server.verify_implement_artifact", rejected)
    monkeypatch.setattr("agent_crew.mcp_server.os.getcwd", lambda: "/worker-checkout")
    tool = build_mcp_server(tmp_db)._tool_manager._tools["submit_result"].fn
    result = tool(task_id="impl-mcp-rebase-only", status="completed", summary="done",
                  branch="feat/work", commit=SHA_NEW)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)

    assert result["held"] == "no_artifact"
    assert seen == [("main", "/worker-checkout")]
