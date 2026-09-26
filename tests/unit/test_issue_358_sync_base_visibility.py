"""#358: worktree sync must leave a visible, deterministic base."""
from unittest.mock import MagicMock
import subprocess
from pathlib import Path
import pytest

from fastapi.testclient import TestClient
from agent_crew.pipeline import verify_implement_artifact
from agent_crew.protocol import TaskRequest, TaskResult


SHA_REQUESTED = "a" * 40
SHA_DEFAULT = "b" * 40


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True)


def test_sync_uses_real_origin_for_known_and_fallback_bases(tmp_path):
    """Real refs, not mocked subprocess results, prove #358's checkout path."""
    from agent_crew.cli import _sync_worktrees_to_main

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True,
                   capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "test")
    (clone / "base.txt").write_text("base\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", "-b", "feat/live")
    (clone / "live.txt").write_text("live\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "live")
    _git(clone, "push", "origin", "feat/live")
    live_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
    _git(clone, "checkout", "main")
    worker = tmp_path / "worker"
    _git(clone, "worktree", "add", "--detach", str(worker), "HEAD")

    known = _sync_worktrees_to_main({"implementer": str(worker)}, base_branch="feat/live")
    assert known["implementer"] == {"requested_ref": "origin/feat/live", "actual_ref": "origin/feat/live", "sha": live_sha, "status": "known"}
    fallback = _sync_worktrees_to_main({"implementer": str(worker)}, base_branch="not-pushed")
    assert fallback["implementer"]["requested_ref"] == "origin/not-pushed"
    assert fallback["implementer"]["actual_ref"] == "origin/main"
    assert fallback["implementer"]["status"] == "fallback"


def test_all_sync_call_sites_consume_the_returned_provenance():
    """Deleting any assignment at the four call sites makes this source contract fail."""
    source = Path("src/agent_crew/cli.py").read_text()
    assert source.count("= _sync_worktrees_to_main(") >= 4
    assert '"sync_landed_bases": retry_bases' in source
    assert '"sync_landed_bases": _sync_landed_bases' in source
    assert '"sync_landed_bases": _discuss_sync_bases' in source


def test_sync_records_requested_origin_base_when_checkout_succeeds(monkeypatch, tmp_path):
    from agent_crew.cli import _sync_worktrees_to_main

    worktree = tmp_path / "worker"
    worktree.mkdir()
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "rev-parse" in argv:
            return MagicMock(returncode=0, stdout=SHA_REQUESTED + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.cli.subprocess.run", run)
    bases = _sync_worktrees_to_main({"implementer": str(worktree)}, base_branch="feature/live")

    assert bases == {"implementer": {"requested_ref": "origin/feature/live",
                                      "actual_ref": "origin/feature/live",
                                      "sha": SHA_REQUESTED, "status": "known"}}
    assert any(call[-1] == "origin/feature/live" for call in calls if "checkout" in call)


def test_sync_falls_back_to_origin_default_and_records_actual_base(monkeypatch, tmp_path):
    from agent_crew.cli import _sync_worktrees_to_main

    worktree = tmp_path / "worker"
    worktree.mkdir()
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "checkout" in argv and argv[-1] == "origin/unpushed-feature":
            return MagicMock(returncode=1, stdout="", stderr="unknown revision")
        if "symbolic-ref" in argv:
            return MagicMock(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-parse" in argv:
            return MagicMock(returncode=0, stdout=SHA_DEFAULT + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.cli.subprocess.run", run)
    bases = _sync_worktrees_to_main(
        {"implementer": str(worktree)}, base_branch="unpushed-feature")

    assert bases == {"implementer": {"requested_ref": "origin/unpushed-feature",
                                      "actual_ref": "origin/main",
                                      "sha": SHA_DEFAULT, "status": "fallback"}}
    assert any(call[-1] == "origin/main" for call in calls if "checkout" in call)


def test_sync_git_failure_returns_unknown_without_raising(monkeypatch, tmp_path):
    from agent_crew.cli import _sync_worktrees_to_main

    worktree = tmp_path / "worker"
    worktree.mkdir()
    monkeypatch.setattr(
        "agent_crew.cli.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )

    assert _sync_worktrees_to_main({"implementer": str(worktree)}) == {
        "implementer": {"requested_ref": "origin/main", "actual_ref": None,
                          "sha": None, "status": "unknown"},
    }


def test_dispatch_records_actual_or_unknown_worktree_base_without_blocking(
        monkeypatch, tmp_db, tmp_path):
    """A prep failure remains deliverable but leaves visible unknown identity."""
    from agent_crew.server import create_app

    worktree = tmp_path / "worker"
    worktree.mkdir()
    state = tmp_path / "state.json"
    state.write_text(
        '{"roles": [{"role": "implementer", "agent": "codex", "worktree": '
        + repr(str(worktree)).replace("'", '"') + "}]}"
    )
    pushed = []
    monkeypatch.setattr("agent_crew.server._prepare_worktree_for_task", lambda *_a, **_k: "")
    monkeypatch.setattr("agent_crew.server._pane_has_usage_limit", lambda *_a, **_k: False)
    app = create_app(
        tmp_db, state_path=str(state), pane_map={"implementer": "%91"},
        push_fn=lambda *args: pushed.append(args), watchdog_disabled=True,
    )

    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "sync-unknown", "task_type": "implement", "description": "x",
            "branch": "unpushed", "priority": 3, "context": {}, "project": "demo",
        })
        stored = client.get("/tasks/sync-unknown").json()

    assert response.status_code == 201
    assert pushed, "sync uncertainty must not block dispatch"
    assert stored["context"]["worktree_base_sha"] is None
    assert stored["context"]["worktree_base_status"] == "unknown"


def test_dispatch_records_the_exact_prepared_worktree_base(monkeypatch, tmp_db, tmp_path):
    from agent_crew.server import create_app

    worktree = tmp_path / "worker"
    worktree.mkdir()
    state = tmp_path / "state.json"
    state.write_text(
        '{"roles": [{"role": "implementer", "agent": "codex", "worktree": '
        + repr(str(worktree)).replace("'", '"') + "}]}"
    )
    monkeypatch.setattr(
        "agent_crew.server._prepare_worktree_for_task", lambda *_a, **_k: SHA_DEFAULT,
    )
    monkeypatch.setattr("agent_crew.server._pane_has_usage_limit", lambda *_a, **_k: False)
    app = create_app(
        tmp_db, state_path=str(state), pane_map={"implementer": "%91"},
        push_fn=lambda *_args: None, watchdog_disabled=True,
    )

    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "sync-known", "task_type": "implement", "description": "x",
            "branch": "main", "priority": 3, "context": {}, "project": "demo",
        })
        stored = client.get("/tasks/sync-known").json()

    assert response.status_code == 201
    assert stored["context"]["worktree_base_sha"] == SHA_DEFAULT
    assert stored["context"]["worktree_base_status"] == "known"


@pytest.mark.parametrize("explicit_base", [None, "feat/integration"])
def test_implement_dispatch_ignores_prior_pr_branch_head(
        tmp_path, tmp_db, monkeypatch, explicit_base):
    """#397: a previous PR branch cannot become the next task's base."""
    from agent_crew.server import create_app
    import json

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "test")
    (clone / "base.txt").write_text("base\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", "-b", "fix/prior")
    (clone / "prior.txt").write_text("prior PR\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "prior PR")
    _git(clone, "push", "origin", "fix/prior")
    prior_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
    worker_clone = tmp_path / "worker-clone"
    _git(tmp_path, "clone", str(origin), str(worker_clone))
    _git(worker_clone, "config", "user.email", "worker@example.com")
    _git(worker_clone, "config", "user.name", "worker")
    worker = tmp_path / "worker"
    _git(worker_clone, "worktree", "add", "--detach", str(worker),
         "origin/fix/prior")
    assert _git(worker, "rev-parse", "origin/main").stdout.strip() != prior_sha
    _git(clone, "checkout", "main")
    (clone / "main.txt").write_text("new main\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "new main")
    _git(clone, "push", "origin", "main")
    main_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
    expected_sha = main_sha
    if explicit_base:
        _git(clone, "checkout", "-b", explicit_base)
        (clone / "integration.txt").write_text("explicit base\n")
        _git(clone, "add", ".")
        _git(clone, "commit", "-m", "integration base")
        _git(clone, "push", "origin", explicit_base)
        expected_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
    assert _git(worker, "rev-parse", "HEAD").stdout.strip() == prior_sha
    assert _git(worker, "rev-parse", "origin/main").stdout.strip() != main_sha

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"roles": [{"role": "implementer", "agent": "codex",
                                          "worktree": str(worker)}]}))
    monkeypatch.setattr("agent_crew.server._pane_has_usage_limit", lambda *_a, **_k: False)
    app = create_app(tmp_db, state_path=str(state), pane_map={"implementer": "%91"},
                     push_fn=lambda *_args: None, watchdog_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "impl-397", "task_type": "implement", "description": "next task",
            "branch": "fix/prior", "priority": 3,
            "context": {"base_branch": explicit_base} if explicit_base else {},
            "project": "demo",
        })
        stored = client.get("/tasks/impl-397").json()
    assert response.status_code == 201
    assert stored["context"]["worktree_base_sha"] == expected_sha
    assert _git(worker, "rev-parse", "HEAD").stdout.strip() == expected_sha

    # Dispatch can prepare twice; the recorded SHA must keep the second pass
    # on the same base even if the remote base branch advances in between.
    _git(clone, "checkout", explicit_base or "main")
    (clone / "advanced.txt").write_text("base advanced\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "advance base")
    _git(clone, "push", "origin", explicit_base or "main")
    from agent_crew.server import _prepare_worktree_for_task
    assert _prepare_worktree_for_task(str(worker), "impl-397", "fix/prior",
                                      "implementer", stored["context"]) == expected_sha

    (worker / "next.txt").write_text("valid task B commit\n")
    _git(worker, "add", ".")
    _git(worker, "commit", "-m", "next task")
    result_sha = _git(worker, "rev-parse", "HEAD").stdout.strip()
    _git(worker, "push", "origin", "HEAD:refs/heads/fix/next")
    task = TaskRequest("impl-397", "implement", "next task", branch="fix/next",
                       context=stored["context"])
    result = TaskResult("impl-397", "completed", "done", branch="fix/next",
                        commit=result_sha)
    assert verify_implement_artifact(task, result, repo_cwd=str(worker))[0]
