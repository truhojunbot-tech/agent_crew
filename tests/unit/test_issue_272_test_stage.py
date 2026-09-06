"""#272 — the test stage: run less, and never twice at once.

alpha_engine#5541 measured what the tester role was costing: load 30.29 on 16
cores, 1 GB free, a dev job queued 45+ minutes behind it, and two `make test`
runs starting 69 s apart in the same worktree. Two separate causes.

**Duplication.** The prompt said "run the full test suite" for every project,
unconditionally. CI already runs it on the PR, so this was a second copy of the
same work on a developer's host. The fix cannot be "skip alpha_engine" — scope
is a property of each project, so it is configuration, and the default has to
be safe for a project nobody configured.

**Concurrency.** The dispatcher's `active_worktrees` set already refuses two
concurrent tasks per worktree — but it is one process's memory. It does not
survive a restart while a `start_new_session=True` child keeps running, and it
cannot see a second dispatcher. Only a lock outside the process closes that,
which is why the cross-process test here matters more than the in-process one.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from agent_crew import testing_policy as tp

WT = "worktrees/demo/gemini"


@pytest.fixture
def worktree(tmp_path):
    d = tmp_path / WT
    d.mkdir(parents=True)
    return str(d)


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


# ── 1. scope resolution ───────────────────────────────────────────────


def test_the_default_is_targeted_because_ci_already_ran_the_suite(worktree):
    """★★The behaviour change. A project that configures nothing must stop
    duplicating CI — otherwise #272 fixes only the projects that opt in, which
    is the opposite of the ask."""
    scope = tp.load_scope(worktree, "demo", base="/nonexistent")
    assert scope["full_suite"] is False
    assert scope["source"] == "default"


def test_a_project_can_opt_back_into_the_full_suite(worktree):
    _write(os.path.join(worktree, tp.REPO_SCOPE_FILE),
           {"full_suite": True, "full": ["make test"]})
    scope = tp.load_scope(worktree, "demo", base="/nonexistent")
    assert scope["full_suite"] is True and scope["full"] == ["make test"]


def test_the_operator_file_outranks_the_repos_own(worktree, tmp_path):
    """⛔Deliberate precedence. Turning the scope down is an urgent response to
    a loaded host, and it must not require a commit to the repo whose tests are
    the problem."""
    _write(os.path.join(worktree, tp.REPO_SCOPE_FILE), {"full_suite": True})
    _write(str(tmp_path / "base" / "demo" / tp.STATE_SCOPE_FILE), {"full_suite": False})
    scope = tp.load_scope(worktree, "demo", base=str(tmp_path / "base"))
    assert scope["full_suite"] is False


def test_the_env_var_outranks_everything(worktree, tmp_path, monkeypatch):
    _write(str(tmp_path / "base" / "demo" / tp.STATE_SCOPE_FILE), {"full_suite": True})
    monkeypatch.setenv(tp.ENV_SCOPE, json.dumps({"guards": ["ruff check ."]}))
    scope = tp.load_scope(worktree, "demo", base=str(tmp_path / "base"))
    assert scope["guards"] == ["ruff check ."] and scope["full_suite"] is False


def test_the_env_var_may_also_be_a_path(worktree, tmp_path, monkeypatch):
    path = tmp_path / "scope.json"
    _write(str(path), {"targeted": ["pytest {paths}"]})
    monkeypatch.setenv(tp.ENV_SCOPE, str(path))
    assert tp.load_scope(worktree, "demo", base="/nonexistent")["targeted"] == ["pytest {paths}"]


@pytest.mark.parametrize("payload", ['{"targeted": "pytest {paths}"}', '{"targeted": null}'])
def test_a_hand_edited_string_where_a_list_belongs_is_tolerated(payload, monkeypatch, worktree):
    """A malformed scope must not break dispatch — a tester that cannot start
    is worse than a tester running slightly the wrong commands."""
    monkeypatch.setenv(tp.ENV_SCOPE, payload)
    scope = tp.load_scope(worktree, "demo", base="/nonexistent")
    assert isinstance(scope["targeted"], list)


def test_a_broken_scope_file_falls_back_without_exploding(worktree, monkeypatch, caplog):
    """⛔And it says so. A broken file silently reading as "no file" would leave
    the operator who wrote it believing it is in effect."""
    path = os.path.join(worktree, tp.REPO_SCOPE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("{not json")
    with caplog.at_level("WARNING"):
        assert tp.load_scope(worktree, "demo", base="/nonexistent")["source"] == "default"
    assert "unreadable" in caplog.text


# ── 2. what the tester is actually told ───────────────────────────────


def test_the_rendered_default_forbids_the_full_suite():
    text = tp.render_scope(tp.DEFAULT_SCOPE)
    assert "do NOT run the full suite" in text
    assert "git diff --name-only" in text


def test_the_rendered_scope_carries_the_configured_commands():
    text = tp.render_scope(tp._coerce(
        {"targeted": ["pytest {paths} -q"], "guards": ["ruff check ."]}, "t"))
    assert "pytest {paths} -q" in text and "ruff check ." in text


def test_an_opted_in_project_is_told_to_run_it_once():
    text = tp.render_scope(tp._coerce({"full_suite": True, "full": ["make test"]}, "t"))
    assert "make test" in text and "Run it once" in text
    assert "do NOT run the full suite" not in text


def test_the_tester_must_say_which_scope_it_used():
    """Without this the change is unobservable: a targeted pass and a full pass
    produce the same result shape, so nobody can tell whether #272 took."""
    assert "must name the scope" in tp.render_scope(tp.DEFAULT_SCOPE)


def test_no_project_name_is_hardcoded_into_the_prompt():
    """⛔The issue asked for this explicitly. alpha_engine may be cited as the
    measurement, but it must not be the condition."""
    text = tp.render_scope(tp.DEFAULT_SCOPE)
    assert "alpha_engine#5541" in text, "the measurement should stay cited"
    assert "if alpha_engine" not in text.lower()
    for other in ("quota-core", "quota-ops", "halla"):
        assert other not in text


def test_the_tester_instruction_file_embeds_the_scope(worktree):
    from agent_crew.instructions import generate

    _write(os.path.join(worktree, tp.REPO_SCOPE_FILE), {"guards": ["make lint"]})
    text = generate("tester", "demo", 8105, delivery="dispatcher", worktree_path=worktree)
    assert "make lint" in text
    assert "<test_scope>" not in text, "the placeholder leaked into the prompt"


def test_the_other_roles_are_untouched():
    """⛔A scope block in the implementer or reviewer prompt would be a silent
    scope creep into roles this issue says nothing about."""
    from agent_crew.instructions import generate

    for role in ("implementer", "reviewer"):
        text = generate(role, "demo", 8105, delivery="dispatcher")
        assert "Test scope" not in text and "<test_scope>" not in text


# ── 3. the lock ───────────────────────────────────────────────────────


def test_the_lock_file_lives_outside_the_worktree(worktree, tmp_path):
    """⛔Out of the repo on purpose: no lock file to gitignore, and one path per
    worktree that every process on the host agrees on regardless of which
    project's state directory it was started from."""
    path = tp.lock_path(worktree, base=str(tmp_path / "base"))
    assert not path.startswith(os.path.realpath(worktree))
    assert path.startswith(str(tmp_path / "base"))


def test_two_paths_to_one_worktree_take_the_same_lock(worktree, tmp_path):
    indirect = os.path.join(worktree, os.pardir, os.path.basename(worktree))
    assert tp.lock_path(worktree, base="/b") == tp.lock_path(indirect, base="/b")


def test_a_second_holder_is_refused_while_the_first_holds(worktree, tmp_path):
    base = str(tmp_path / "base")
    with tp.test_stage_lock(worktree, base=base) as first:
        assert first is True
        with tp.test_stage_lock(worktree, base=base) as second:
            assert second is False, "two test stages entered the same worktree"


def test_the_lock_is_released_when_the_stage_ends(worktree, tmp_path):
    base = str(tmp_path / "base")
    with tp.test_stage_lock(worktree, base=base):
        pass
    with tp.test_stage_lock(worktree, base=base) as again:
        assert again is True


def test_the_lock_is_released_even_when_the_stage_raises(worktree, tmp_path):
    base = str(tmp_path / "base")
    with pytest.raises(RuntimeError):
        with tp.test_stage_lock(worktree, base=base):
            raise RuntimeError("test stage blew up")
    with tp.test_stage_lock(worktree, base=base) as again:
        assert again is True, "a crashed test stage wedged the worktree"


def test_different_worktrees_do_not_block_each_other(tmp_path):
    base = str(tmp_path / "base")
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    os.makedirs(a); os.makedirs(b)
    with tp.test_stage_lock(a, base=base) as first, tp.test_stage_lock(b, base=base) as second:
        assert first and second


def test_an_unwritable_lock_directory_does_not_stop_testing(worktree, monkeypatch, caplog):
    """⛔A hygiene mechanism must not become an outage. If the lock cannot be
    taken at all, run — unlocked and loudly — rather than refuse to test."""
    monkeypatch.setattr(tp.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    with caplog.at_level("WARNING"):
        with tp.test_stage_lock(worktree, base="/nope") as ok:
            assert ok is True
    assert "proceeding unlocked" in caplog.text


def test_a_second_PROCESS_is_refused(worktree, tmp_path):
    """★★The case the in-memory `active_worktrees` set cannot cover, and the
    one the incident actually was: the guard has to survive a dispatcher
    restart while a detached child keeps running."""
    base = str(tmp_path / "base")
    probe = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")!r})
        from agent_crew.testing_policy import test_stage_lock
        with test_stage_lock({worktree!r}, base={base!r}) as ok:
            print("ACQUIRED" if ok else "REFUSED")
    """)
    with tp.test_stage_lock(worktree, base=base) as held:
        assert held is True
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                             text=True, timeout=60)
    assert out.stdout.strip() == "REFUSED", (out.stdout, out.stderr[-400:])

    # ⛔Control: once we let go, another process gets it. Without this the
    #   assertion above would also pass if the probe were simply broken.
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "ACQUIRED", (out.stdout, out.stderr[-400:])


# ── 4. the dispatcher honours it ──────────────────────────────────────


def _dispatch(tmp_path, monkeypatch, task_type, *, lock_base, role="tester"):
    """Run one dispatch through the real handler; return whether it spawned."""
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    spawned = []

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0

        return _P()

    wt = tmp_path / "worktrees" / "demo" / "gemini"
    wt.mkdir(parents=True, exist_ok=True)   # the caller may have made it to lock it
    state = tmp_path / "state.json"
    # Both agents point at the SAME directory on purpose: the non-test control
    # then holds the lock for exactly the path the implementer would use, so it
    # proves the gate is scoped to the task type and not to the worktree.
    state.write_text(json.dumps(
        {"port": 0, "worktrees": {"gemini": str(wt), "claude": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_BASE", lock_base)
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="t-1", task_type=task_type, description="go",
                              branch="main", context={}))
        task = q.dequeue(role=role)
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, role))
        status = {t.task_id: t.status for t in q.list_tasks()}["t-1"]
    return bool(spawned), status, str(wt)


def test_the_dispatcher_defers_a_test_task_while_the_lock_is_held(tmp_path, monkeypatch):
    """★★End to end: a held lock must stop the second `make test` from ever
    starting, and must put the task back rather than burn it."""
    base = str(tmp_path / "lockbase")
    wt = tmp_path / "worktrees" / "demo" / "gemini"
    wt.mkdir(parents=True)
    with tp.test_stage_lock(str(wt), base=base):
        spawned, status, _ = _dispatch(tmp_path, monkeypatch, "test", lock_base=base)
    assert spawned is False, "a second test stage was launched against a locked worktree"
    assert status == "pending", f"the deferred task was not requeued (status={status})"


def test_an_unlocked_worktree_still_dispatches_its_test(tmp_path, monkeypatch):
    """⛔The control. A lock that also blocks the uncontended case is a bug."""
    spawned, _, _ = _dispatch(tmp_path, monkeypatch, "test",
                              lock_base=str(tmp_path / "lockbase"))
    assert spawned is True


def test_a_non_test_task_is_not_gated_by_the_test_lock(tmp_path, monkeypatch):
    """The lock is scoped to the test stage. Gating implement/review on it
    would serialise the whole crew behind a slow test run."""
    base = str(tmp_path / "lockbase")
    wt = tmp_path / "worktrees" / "demo" / "gemini"
    wt.mkdir(parents=True)
    with tp.test_stage_lock(str(wt), base=base):
        spawned, _, _ = _dispatch(tmp_path, monkeypatch, "implement",
                                  lock_base=base, role="implementer")
    assert spawned is True
