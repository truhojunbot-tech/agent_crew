"""A survivable prep error must retain a verifiable dispatch base (#812)."""

import asyncio
import subprocess

from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path):
    remote = tmp_path / "remote.git"
    worker = tmp_path / "worker"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(worker)],
                   check=True, capture_output=True)
    _git(worker, "config", "user.name", "Test User")
    _git(worker, "config", "user.email", "test@example.invalid")
    (worker / "base.txt").write_text("base\n")
    _git(worker, "add", "base.txt")
    _git(worker, "commit", "-m", "base")
    base = _git(worker, "rev-parse", "HEAD")
    _git(worker, "remote", "add", "origin", str(remote))
    _git(worker, "push", "origin", "main")
    return worker, base


def _app(tmp_path, monkeypatch, worker, pushed, *, role="implementer"):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", "off")
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda *a, **kw: False)
    monkeypatch.setattr(sv, "_resolve_tmux_pane_target", lambda pane: pane)
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_process_kind", lambda pane: ("agent", "codex"))
    monkeypatch.setattr(sv, "_ensure_role_protocol", lambda *a, **kw: True)
    # These pin the needs_human end itself; #896's one transient retry is
    # covered in test_issue_896_prep_timeout_retry.py.
    monkeypatch.setattr(sv, "_PREP_TRANSIENT_RETRIES", 0)
    db = str(tmp_path / "tasks.db")
    app = sv.create_app(
        db, worktree_map={role: str(worker)},
        pane_map={role: "%91"},
        push_fn=lambda *args: pushed.append(args),
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
    )
    return app, TaskQueue(db)


def _timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(["git", "fetch", "origin"], 60)


def test_fetch_timeout_uses_head_and_descendant_completion_passes(
        tmp_path, monkeypatch):
    worker, base = _repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-812-head", "task_type": "implement", "description": "fix",
            "branch": "feature", "project": "demo", "context": {},
        })
        assert admitted.status_code == 201
        context = queue.get_task_context("impl-812-head")
        assert context["worktree_base_sha"] == base
        assert context["worktree_base_status"] == "head_after_prep_error"
        assert context["prep_error"] == "TimeoutExpired"
        assert pushed

        _git(worker, "checkout", "-b", "feature")
        (worker / "change.txt").write_text("change\n")
        _git(worker, "add", "change.txt")
        _git(worker, "commit", "-m", "change")
        commit = _git(worker, "rev-parse", "HEAD")
        _git(worker, "push", "origin", "feature")
        result = client.post("/tasks/impl-812-head/result", json={
            "task_id": "impl-812-head", "status": "completed", "summary": "pushed",
            "branch": "feature", "commit": commit,
        })

    assert result.status_code == 200, result.text
    assert result.json().get("held") != "no_artifact"
    assert queue.get_task_status("impl-812-head") == "completed"


def test_caller_base_precedes_head_after_prep_error(tmp_path, monkeypatch):
    worker, base = _repo(tmp_path)
    (worker / "later.txt").write_text("later\n")
    _git(worker, "add", "later.txt")
    _git(worker, "commit", "-m", "later")
    assert _git(worker, "rev-parse", "HEAD") != base
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-812-caller", "task_type": "implement", "description": "fix",
            "branch": "feature", "project": "demo",
            "context": {"worktree_base_sha": base},
        })

    assert admitted.status_code == 201
    context = queue.get_task_context("impl-812-caller")
    assert context["worktree_base_sha"] == base
    assert context["worktree_base_status"] == "caller_after_prep_error"
    assert context["prep_error"] == "TimeoutExpired"
    assert pushed


def test_unresolvable_caller_base_uses_head_after_timeout(tmp_path, monkeypatch):
    worker, head = _repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-812-unfetched", "task_type": "implement",
            "description": "fix", "branch": "feature", "project": "demo",
            "context": {"worktree_base_sha": "a" * 40},
        })

    assert admitted.status_code == 201
    context = queue.get_task_context("impl-812-unfetched")
    assert context["worktree_base_sha"] == head
    assert context["worktree_base_status"] == "head_after_prep_error"
    assert pushed


def test_review_prep_error_never_pins_unrelated_worktree_head(tmp_path, monkeypatch):
    worker, unrelated = _repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed, role="reviewer")

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "review-812-unrelated", "task_type": "review",
            "description": "review PR", "branch": "main", "project": "demo",
            "context": {"pr_number": 999},
        })

    assert admitted.status_code == 201
    context = queue.get_task_context("review-812-unrelated")
    assert context.get("reviewed_sha") is None
    assert context.get("reviewed_sha") != unrelated
    assert context["worktree_base_status"] == "unknown"
    assert queue.get_task_status("review-812-unrelated") == "needs_human"
    assert queue.get_result("review-812-unrelated").error_info["reason"] == "prepared_base_not_recorded"
    assert pushed == []


def test_unknown_prepared_sha_never_clears_a_valid_caller_base(tmp_path, monkeypatch):
    worker, base = _repo(tmp_path)
    app, queue = _app(tmp_path, monkeypatch, worker, [])
    queue.enqueue(TaskRequest(
        "impl-812-unknown", "implement", "fix", branch="feature",
        project="demo", context={"worktree_base_sha": base},
    ))
    task = queue.get_task("impl-812-unknown")

    with TestClient(app):
        assert app.state.record_prepared_base(task, "implementer", "", "test")
    context = queue.get_task_context(task.task_id)
    assert context["worktree_base_sha"] == base
    assert context["worktree_base_status"] == "caller_when_prep_unknown"


def test_unreadable_head_after_prep_error_stops_before_provider(tmp_path, monkeypatch):
    worker = tmp_path / "unreadable"
    worker.mkdir()
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-812-unreadable", "task_type": "implement",
            "description": "fix", "branch": "feature", "project": "demo", "context": {},
        })

    assert admitted.status_code == 201
    assert pushed == []
    assert queue.get_task_status("impl-812-unreadable") == "needs_human"
    context = queue.get_task_context("impl-812-unreadable")
    assert context["worktree_base_sha"] is None
    assert context["prep_error"] == "TimeoutExpired"
    assert queue.get_result("impl-812-unreadable").status == "needs_human"
    assert queue.get_result("impl-812-unreadable").error_info["reason"] == "prepared_base_not_recorded"


def test_unverified_caller_is_not_recorded_when_prep_and_head_fail(tmp_path, monkeypatch):
    worker = tmp_path / "unreadable"
    worker.mkdir()
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-812-unverified", "task_type": "implement",
            "description": "fix", "branch": "feature", "project": "demo",
            "context": {"worktree_base_sha": "a" * 40},
        })

    assert admitted.status_code == 201
    assert queue.get_task_status("impl-812-unverified") == "needs_human"
    context = queue.get_task_context("impl-812-unverified")
    assert context.get("worktree_base_sha") is None
    assert context["worktree_base_status"] == "unknown"
    assert queue.get_result("impl-812-unverified").error_info["reason"] == "prepared_base_not_recorded"
    assert pushed == []


def test_successful_second_prep_clears_stale_error_label(tmp_path, monkeypatch):
    worker, base = _repo(tmp_path)
    app, queue = _app(tmp_path, monkeypatch, worker, [])
    queue.enqueue(TaskRequest("impl-812-reprep", "implement", "fix", branch="feature",
                              project="demo", context={}))
    task = queue.get_task("impl-812-reprep")
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)

    with TestClient(app):
        first = sv._prepare_worktree_for_task(str(worker), task.task_id, "feature",
                                               "implementer", task_context=task.context)
        assert app.state.record_prepared_base(task, "implementer", first, "test")
        monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", lambda *a, **kw: base)
        second = sv._prepare_worktree_for_task(str(worker), task.task_id, "feature",
                                                "implementer", task_context=task.context)
        assert app.state.record_prepared_base(task, "implementer", second, "test")

    context = queue.get_task_context(task.task_id)
    assert context["worktree_base_sha"] == base
    assert context["worktree_base_status"] == "known"
    assert not context.get("prep_error")
    assert not context.get("prep_error_base_source")


def test_dispatcher_unreadable_head_never_spawns_provider(tmp_path, monkeypatch):
    worker = tmp_path / "unreadable"
    worker.mkdir()
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("provider must not launch without a base")

    monkeypatch.setattr(sv.asyncio, "create_subprocess_exec", fake_exec)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app):
        queue.enqueue(TaskRequest(
            "impl-812-dispatch", "implement", "fix", branch="feature",
            project="demo", context={},
        ))
        task = queue.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert spawned == []
    assert queue.get_task_status("impl-812-dispatch") == "needs_human"
    assert queue.get_task_context("impl-812-dispatch")["prep_error"] == "TimeoutExpired"


def test_empty_second_prep_keeps_error_and_never_readopts_stale_head(
        tmp_path, monkeypatch):
    """review-98317bcb: an empty re-preparation must not launder the prior
    HEAD fallback into a "caller" base once the worktree has moved on."""
    worker, head_a = _repo(tmp_path)
    app, queue = _app(tmp_path, monkeypatch, worker, [])
    queue.enqueue(TaskRequest("impl-812-stale", "implement", "fix", branch="feature",
                              project="demo", context={}))
    task = queue.get_task("impl-812-stale")
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)

    with TestClient(app):
        first = sv._prepare_worktree_for_task(str(worker), task.task_id, "feature",
                                               "implementer", task_context=task.context)
        assert first == head_a
        assert app.state.record_prepared_base(task, "implementer", first, "test")
        assert queue.get_task_context(task.task_id)["worktree_base_sha"] == head_a

        (worker / "moved.txt").write_text("moved\n")
        _git(worker, "add", "moved.txt")
        _git(worker, "commit", "-m", "moved")
        head_b = _git(worker, "rev-parse", "HEAD")
        assert head_b != head_a

        monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", lambda *a, **kw: "")
        second = sv._prepare_worktree_for_task(str(worker), task.task_id, "feature",
                                                "implementer", task_context=task.context)
        assert second == ""
        assert task.context["prep_error"] == "TimeoutExpired"
        assert app.state.record_prepared_base(task, "implementer", second, "test")

    context = queue.get_task_context(task.task_id)
    assert context["worktree_base_sha"] is None
    assert context["worktree_base_status"] == "unknown"
    assert context["prep_error"] == "TimeoutExpired"
    assert context.get("prep_error_base_source") is None


def test_dispatcher_stops_when_empty_reprep_follows_prep_error(tmp_path, monkeypatch):
    worker, head_a = _repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-812-reprep-stop", "task_type": "implement",
            "description": "fix", "branch": "feature", "project": "demo", "context": {},
        })
        assert admitted.status_code == 201
        assert queue.get_task_context("impl-812-reprep-stop")["worktree_base_sha"] == head_a
        assert len(pushed) == 1

        (worker / "moved.txt").write_text("moved\n")
        _git(worker, "add", "moved.txt")
        _git(worker, "commit", "-m", "moved")

        monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", lambda *a, **kw: "")
        task = queue.get_task("impl-812-reprep-stop")
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert len(pushed) == 1
    context = queue.get_task_context("impl-812-reprep-stop")
    assert context["worktree_base_sha"] is None
    assert context["worktree_base_status"] == "unknown"
    assert context["prep_error"] == "TimeoutExpired"
    assert queue.get_task_status("impl-812-reprep-stop") == "needs_human"
    assert queue.get_result("impl-812-reprep-stop").error_info["reason"] == "prepared_base_not_recorded"


def test_unknown_prep_rejects_caller_base_that_is_not_current_head(tmp_path, monkeypatch):
    worker, base = _repo(tmp_path)
    (worker / "later.txt").write_text("later\n")
    _git(worker, "add", "later.txt")
    _git(worker, "commit", "-m", "later")
    assert _git(worker, "rev-parse", "HEAD") != base
    app, queue = _app(tmp_path, monkeypatch, worker, [])
    queue.enqueue(TaskRequest(
        "impl-812-caller-moved", "implement", "fix", branch="feature",
        project="demo", context={"worktree_base_sha": base},
    ))
    task = queue.get_task("impl-812-caller-moved")

    with TestClient(app):
        assert app.state.record_prepared_base(task, "implementer", "", "test")
    context = queue.get_task_context(task.task_id)
    assert context["worktree_base_sha"] is None
    assert context["worktree_base_status"] == "unknown"
