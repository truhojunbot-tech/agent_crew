"""#358: worktree sync must leave a visible, deterministic base."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient


SHA_REQUESTED = "a" * 40
SHA_DEFAULT = "b" * 40


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
        tmp_db, state_path=str(state), pane_map={"implementer": "%1"},
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
        tmp_db, state_path=str(state), pane_map={"implementer": "%1"},
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
