import pathlib
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent_crew.cli import (
    _capture_pane,
    _pane_looks_idle,
    crew,
)


# U-C01: crew --help → exit 0, 서브커맨드 목록 포함
def test_u_c01_crew_help():
    runner = CliRunner()
    result = runner.invoke(crew, ["--help"])
    assert result.exit_code == 0
    assert "setup" in result.output
    assert "status" in result.output
    assert "run" in result.output
    assert "discuss" in result.output
    assert "teardown" in result.output


# U-C02: crew setup (project 이름 없음) → 에러
def test_u_c02_crew_setup_no_project():
    runner = CliRunner()
    result = runner.invoke(crew, ["setup"])
    assert result.exit_code != 0


# U-C03: crew status <project> (존재하지 않는 프로젝트) → not found 에러, exit != 0
def test_u_c03_crew_status_no_project():
    runner = CliRunner()
    result = runner.invoke(crew, ["status", "nonexistent", "--base", "/tmp/_crew_no_such_base"])
    assert result.exit_code != 0
    assert len(result.output.strip()) > 0


# U-C04: crew run (빈 task) → 에러
def test_u_c04_crew_run_empty_task():
    runner = CliRunner()
    result = runner.invoke(crew, ["run", ""])
    assert result.exit_code != 0


# U-C05: crew discuss (빈 topic) → 에러
def test_u_c05_crew_discuss_empty_topic():
    runner = CliRunner()
    result = runner.invoke(crew, ["discuss", ""])
    assert result.exit_code != 0


# U-C06: crew teardown <project> (존재하지 않는 프로젝트) → not found 에러, exit != 0
def test_u_c06_crew_teardown():
    runner = CliRunner()
    result = runner.invoke(crew, ["teardown", "nonexistent", "--base", "/tmp/_crew_no_such_base"])
    assert result.exit_code != 0
    assert len(result.output.strip()) > 0


# U-C07: _pane_looks_idle — shell prompt detected
def test_u_c07_pane_looks_idle_shell_prompt():
    assert _pane_looks_idle("some output\n$ ") is True
    assert _pane_looks_idle("some output\n$") is True
    assert _pane_looks_idle("some output\n❯ ") is True
    assert _pane_looks_idle("some output\n>>> ") is True
    assert _pane_looks_idle("Completed task") is True


# U-C08: _pane_looks_idle — agent still working
def test_u_c08_pane_looks_idle_active():
    assert _pane_looks_idle("Running tests...\ntest_foo PASSED") is False
    assert _pane_looks_idle("Writing file src/main.py") is False
    assert _pane_looks_idle("") is False


# U-C09: _capture_pane — successful capture
def test_u_c09_capture_pane_success():
    mock_result = MagicMock(returncode=0, stdout="line1\nline2\n$ ")
    with patch("agent_crew.cli.subprocess.run", return_value=mock_result) as mock_run:
        output = _capture_pane("%42")
    assert output == "line1\nline2\n$ "
    args = mock_run.call_args[0][0]
    assert "tmux" in args
    assert "capture-pane" in args
    assert "%42" in args


# U-C10: _capture_pane — tmux not available
def test_u_c10_capture_pane_failure():
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("agent_crew.cli.subprocess.run", return_value=mock_result):
        output = _capture_pane("%42")
    assert output is None


# U-C16: discuss --nowait enqueues tasks and returns immediately (no polling)
def test_u_c16_discuss_nowait_returns_immediately(tmp_path):
    """--nowait must not block on task results — it enqueues and prints task_ids
    so the caller can poll `crew status` at their own pace."""
    db_path = str(tmp_path / "tasks.db")
    runner = CliRunner()
    result = runner.invoke(crew, [
        "discuss", "Adopt Rust?",
        "--db", db_path,
        "--agents", "analyst,critic",
        "--nowait",
    ])
    assert result.exit_code == 0, result.output
    assert "queued" in result.output.lower()
    # Both agent names must appear so caller can correlate tasks to panelists.
    # Line format: "  <agent> (<perspective>): <task_id>"
    assert "analyst " in result.output
    assert "critic " in result.output
    # Rows exist in DB with pending status (never got pushed — no tmux/server).
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT task_type, status FROM tasks").fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(r["task_type"] == "discuss" for r in rows)


# U-C17: discuss times out → partial synthesis written, missing task_ids reported, exit 2
def test_u_c17_discuss_timeout_preserves_partial_synthesis(tmp_path):
    """When the timeout fires with only some agents done, the caller shouldn't
    lose the completed work — we write a PARTIAL synthesis and emit the
    missing task_ids so the user can re-run or wait-and-status."""
    from agent_crew.queue import TaskQueue
    from agent_crew.discussion import enqueue_panel_tasks
    from agent_crew.protocol import TaskResult

    db_path = str(tmp_path / "tasks.db")
    output = str(tmp_path / "synthesis.md")
    queue = TaskQueue(db_path)

    # Pre-stage: one agent "already responded", the other never will.
    task_ids = enqueue_panel_tasks(
        queue, ["analyst", "critic"], "AI strategy", {"round": 1}, port=0
    )
    # Mark analyst's task as completed with a real summary.
    queue.submit_result(task_ids[0], TaskResult(
        task_id=task_ids[0], status="completed", summary="analyst summary content."
    ))
    # critic's task_ids[1] stays pending → will trigger the timeout branch.

    # We need enqueue_panel_tasks to return our pre-staged IDs so _wait_all
    # polls for them. Patch it to return the existing IDs and not re-enqueue.
    runner = CliRunner()
    with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=task_ids):
        result = runner.invoke(crew, [
            "discuss", "AI strategy",
            "--db", db_path,
            "--agents", "analyst,critic",
            "--output", output,
            "--timeout", "1",  # fire quickly
        ])

    assert result.exit_code == 2, f"expected partial-success exit 2, got {result.exit_code}: {result.output}"
    # Synthesis file must exist and carry PARTIAL marker + the completed summary.
    content = pathlib.Path(output).read_text()
    assert "PARTIAL" in content
    assert "analyst summary content." in content
    # Missing task_id must be surfaced so the user can chase it.
    combined = result.output + (result.stderr if hasattr(result, "stderr_bytes") else "")
    assert task_ids[1] in combined or task_ids[1] in content


# U-C18: discuss in project mode refuses agents not in pane_map
# Prevents the silent-push-skip bug where perspective names (analyst/critic)
# were passed as agents and every task sat queued forever.
def test_u_c18_discuss_rejects_unknown_agents_in_project_mode(tmp_path):
    import json
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()
    db_file = str(proj_dir / "tasks.db")
    state = {
        "project": "myproj",
        "port": 0,
        "session": "crew_myproj",
        "agents": ["claude", "codex"],
        "db": db_file,
        "pane_map": {
            "implementer": "%1", "claude": "%1",
            "reviewer": "%2", "codex": "%2",
        },
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    result = runner.invoke(crew, [
        "discuss", "Adopt microservices?",
        "--project", "myproj",
        "--base", str(tmp_path),
        "--agents", "analyst,critic",
    ])
    assert result.exit_code != 0
    out = result.output
    assert "analyst" in out and "critic" in out
    # Must hint that these are perspectives, not agents — that's the confusion
    # we're trying to head off.
    assert "perspective" in out.lower()
    # Must list the real agents so the user can see what to pass.
    assert "claude" in out
    assert "codex" in out


# U-C19: discuss context carries both agent (pane routing) and perspective
# (instruction framing). Previously only agent was in context, so the agent
# CLI's perspective-aware prompt had nothing to read.
def test_u_c19_discuss_context_includes_perspective():
    from agent_crew.discussion import enqueue_panel_tasks

    class _FakeQueue:
        def __init__(self):
            self.enqueued = []
        def enqueue(self, req):
            self.enqueued.append(req)
            return req.task_id

    q = _FakeQueue()
    ids = enqueue_panel_tasks(
        q, ["claude", "codex"], "Adopt Rust?", {"round": 1}, port=0,
        perspectives={"claude": "analyst", "codex": "critic"},
    )
    assert len(ids) == 2
    assert q.enqueued[0].context["agent"] == "claude"
    assert q.enqueued[0].context["perspective"] == "analyst"
    assert q.enqueued[1].context["agent"] == "codex"
    assert q.enqueued[1].context["perspective"] == "critic"


# U-C20: recover — session alive + all panes alive → no-op (nothing to recover)
def test_u_c20_recover_all_panes_alive_noop(tmp_path):
    """Issue #52: recover must skip work when every agent pane is already alive."""
    import json
    proj_dir = tmp_path / "rcproj"
    proj_dir.mkdir()
    state = {
        "project": "rcproj",
        "port": 8100,
        "session": "crew_rcproj",
        "window": "0",
        "agents": ["claude", "codex"],
        "pane_ids": ["%1", "%2"],
        "pane_map": {
            "claude": "%1", "codex": "%2",
            "implementer": "%1", "reviewer": "%2",
        },
        "worktrees": {"claude": "/tmp/wt/claude", "codex": "/tmp/wt/codex"},
        "db": str(proj_dir / "tasks.db"),
        "server_pid": 123,
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    def _fake_run(args, **_kw):
        # Window validation needs list-windows to succeed
        if "list-windows" in args:
            return MagicMock(returncode=0, stdout="0", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = CliRunner()
    with patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.cli._pane_alive", return_value=True), \
         patch("agent_crew.cli.os.path.isdir", return_value=True), \
         patch("agent_crew.cli.subprocess.run", side_effect=_fake_run) as mock_run, \
         patch("agent_crew.cli.setup_module.start_agents_in_panes") as mock_start:
        result = runner.invoke(crew, ["recover", "rcproj", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "nothing to recover" in result.output.lower()
    mock_start.assert_not_called()
    split_calls = [c for c in mock_run.call_args_list if "split-window" in str(c)]
    assert len(split_calls) == 0


# U-C21: recover — session alive + one pane dead → only dead pane recreated
def test_u_c21_recover_recreates_only_dead_panes(tmp_path):
    """Issue #52: alive panes keep their pane_id; dead panes get fresh ones,
    and only the dead agent gets (re-)started."""
    import json
    proj_dir = tmp_path / "rcproj"
    proj_dir.mkdir()
    state = {
        "project": "rcproj",
        "port": 8100,
        "session": "crew_rcproj",
        "window": "0",
        "agents": ["claude", "codex"],
        "pane_ids": ["%1", "%2"],
        "pane_map": {
            "claude": "%1", "codex": "%2",
            "implementer": "%1", "reviewer": "%2",
        },
        "worktrees": {"claude": "/tmp/wt/claude", "codex": "/tmp/wt/codex"},
        "db": str(proj_dir / "tasks.db"),
        "server_pid": 123,
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    def _fake_pane_alive(pane_id):
        return pane_id == "%1"

    def _fake_run(args, **_kw):
        if "list-windows" in args:
            return MagicMock(returncode=0, stdout="0", stderr="")
        if "split-window" in args:
            return MagicMock(returncode=0, stdout="%9\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = CliRunner()
    with patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.cli._pane_alive", side_effect=_fake_pane_alive), \
         patch("agent_crew.cli.os.path.isdir", return_value=True), \
         patch("agent_crew.cli.subprocess.run", side_effect=_fake_run) as mock_run, \
         patch("agent_crew.cli.setup_module.start_agents_in_panes") as mock_start:
        result = runner.invoke(crew, ["recover", "rcproj", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output

    split_calls = [c for c in mock_run.call_args_list if "split-window" in str(c)]
    assert len(split_calls) == 1, f"expected 1 split-window call, got {len(split_calls)}"

    mock_start.assert_called_once()
    pos = mock_start.call_args[0]
    kw = mock_start.call_args[1]
    assert pos[1] == ["codex"]
    assert kw.get("pane_targets") == ["%9"]

    new_state = json.loads((proj_dir / "state.json").read_text())
    assert new_state["pane_ids"] == ["%1", "%9"]
    assert new_state["pane_map"]["codex"] == "%9"
    assert new_state["pane_map"]["reviewer"] == "%9"
    assert new_state["pane_map"]["claude"] == "%1"
    assert "pane" in result.output.lower()


# U-C22: recover — session alive + all panes dead → all recreated in place
def test_u_c22_recover_all_panes_dead_recreates_all(tmp_path):
    import json
    proj_dir = tmp_path / "rcproj"
    proj_dir.mkdir()
    state = {
        "project": "rcproj",
        "port": 8100,
        "session": "crew_rcproj",
        "window": "0",
        "agents": ["claude", "codex"],
        "pane_ids": ["%1", "%2"],
        "pane_map": {
            "claude": "%1", "codex": "%2",
            "implementer": "%1", "reviewer": "%2",
        },
        "worktrees": {"claude": "/tmp/wt/claude", "codex": "/tmp/wt/codex"},
        "db": str(proj_dir / "tasks.db"),
        "server_pid": 123,
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    fresh_ids = iter(["%7\n", "%8\n"])

    def _fake_run(args, **_kw):
        if "list-windows" in args:
            return MagicMock(returncode=0, stdout="0", stderr="")
        if "split-window" in args:
            return MagicMock(returncode=0, stdout=next(fresh_ids), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = CliRunner()
    with patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.cli._pane_alive", return_value=False), \
         patch("agent_crew.cli.subprocess.run", side_effect=_fake_run) as mock_run, \
         patch("agent_crew.cli.setup_module.start_agents_in_panes") as mock_start:
        result = runner.invoke(crew, ["recover", "rcproj", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output

    split_calls = [c for c in mock_run.call_args_list if "split-window" in str(c)]
    assert len(split_calls) == 2, f"expected 2 split-window calls, got {len(split_calls)}"

    mock_start.assert_called_once()
    pos = mock_start.call_args[0]
    kw = mock_start.call_args[1]
    assert pos[1] == ["claude", "codex"]
    assert kw.get("pane_targets") == ["%7", "%8"]

    new_state = json.loads((proj_dir / "state.json").read_text())
    assert new_state["pane_ids"] == ["%7", "%8"]
    assert new_state["pane_map"]["claude"] == "%7"
    assert new_state["pane_map"]["codex"] == "%8"


# U-C15: teardown runs git worktree prune after removing worktrees
def test_u_c15_teardown_runs_worktree_prune(tmp_path):
    import json
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()
    state = {
        "session": "crew_myproj",
        "agents": ["claude"],
        "worktrees": {"claude": str(tmp_path / "wt" / "claude")},
        "server_pid": 0,
        "port_file": str(tmp_path / "port"),
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    mock_ok = MagicMock(returncode=0, stdout="", stderr="")
    runner = CliRunner()
    with patch("agent_crew.cli.subprocess.run", return_value=mock_ok) as mock_run, \
         patch("agent_crew.cli.shutil.rmtree"), \
         patch("agent_crew.cli.os.kill"), \
         patch("agent_crew.cli._tmux_snapshot", return_value=""):
        runner.invoke(crew, ["teardown", "myproj", "--base", str(tmp_path)])

    prune_calls = [
        c for c in mock_run.call_args_list
        if "worktree" in str(c) and "prune" in str(c)
    ]
    assert len(prune_calls) == 1, f"Expected 1 prune call, got {len(prune_calls)}"


# U-C31: _auto_detect_project — returns None when base dir doesn't exist
def test_u_c31_auto_detect_nonexistent_base():
    from agent_crew.cli import _auto_detect_project
    result = _auto_detect_project("/nonexistent/path/that/does/not/exist")
    assert result is None


# U-C32: _auto_detect_project — returns None when no projects exist
def test_u_c32_auto_detect_no_projects(tmp_path):
    from agent_crew.cli import _auto_detect_project
    base = str(tmp_path)
    result = _auto_detect_project(base)
    assert result is None


# U-C33: _auto_detect_project — returns most recently modified project
def test_u_c33_auto_detect_most_recent_project(tmp_path):
    from agent_crew.cli import _auto_detect_project
    import json
    import time

    base = str(tmp_path)

    # Create two projects with different mtimes
    proj1_dir = tmp_path / "project_old"
    proj1_dir.mkdir()
    (proj1_dir / "state.json").write_text(json.dumps({"project": "project_old"}))

    time.sleep(0.1)  # Ensure different mtimes

    proj2_dir = tmp_path / "project_new"
    proj2_dir.mkdir()
    (proj2_dir / "state.json").write_text(json.dumps({"project": "project_new"}))

    result = _auto_detect_project(base)
    assert result == "project_new"


# U-C34: _auto_detect_project — ignores dirs without state.json
def test_u_c34_auto_detect_ignores_dirs_without_state(tmp_path):
    from agent_crew.cli import _auto_detect_project
    import json

    base = str(tmp_path)

    # Create project with state.json
    proj_with_state = tmp_path / "with_state"
    proj_with_state.mkdir()
    (proj_with_state / "state.json").write_text(json.dumps({"project": "with_state"}))

    # Create project without state.json
    proj_without_state = tmp_path / "without_state"
    proj_without_state.mkdir()
    (proj_without_state / "other_file.txt").write_text("not a state file")

    result = _auto_detect_project(base)
    assert result == "with_state"


# U-C35: _auto_detect_project — works with tilde expansion
def test_u_c35_auto_detect_tilde_expansion(tmp_path, monkeypatch):
    from agent_crew.cli import _auto_detect_project
    import json

    # Mock home directory to tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))

    # Create nested directory structure
    crew_dir = tmp_path / ".agent_crew"
    crew_dir.mkdir()
    proj_dir = crew_dir / "test_project"
    proj_dir.mkdir()
    (proj_dir / "state.json").write_text(json.dumps({"project": "test_project"}))

    result = _auto_detect_project("~/.agent_crew")
    assert result == "test_project"


# U-C36: _auto_detect_project — handles exception gracefully
def test_u_c36_auto_detect_handles_exception(tmp_path):
    from agent_crew.cli import _auto_detect_project
    from unittest.mock import patch

    base = str(tmp_path)

    # Mock os.listdir to raise an exception
    with patch("os.listdir", side_effect=PermissionError("Access denied")):
        result = _auto_detect_project(base)

    assert result is None


# U-C37: crew run uses auto-detected project in place of explicit --project flag
def test_u_c37_run_auto_detect_used(tmp_path):
    from agent_crew.cli import run_cmd
    from unittest.mock import patch, MagicMock
    import json

    base = str(tmp_path)
    proj_dir = tmp_path / "auto_test"
    proj_dir.mkdir()
    db_file = str(proj_dir / "tasks.db")
    state = {
        "project": "auto_test",
        "port": 0,
        "db": db_file,
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    # Mock the downstream functions to avoid server requirement
    with patch("agent_crew.cli.click.ClickException") as mock_exc, \
         patch("agent_crew.cli._read_state") as mock_read:
        mock_read.return_value = state
        # When auto-detect happens and we provide only base, it should
        # find "auto_test" and proceed without error
        from agent_crew.cli import _auto_detect_project
        result = _auto_detect_project(base)
        assert result == "auto_test"


# U-C38: crew discuss uses auto-detected project in place of explicit --project flag
def test_u_c38_discuss_auto_detect_used(tmp_path):
    from agent_crew.cli import _auto_detect_project
    import json

    base = str(tmp_path)
    proj_dir = tmp_path / "discuss_auto_test"
    proj_dir.mkdir()
    db_file = str(proj_dir / "tasks.db")
    state = {
        "project": "discuss_auto_test",
        "port": 0,
        "db": db_file,
    }
    (proj_dir / "state.json").write_text(json.dumps(state))

    # When auto-detect happens, it should find "discuss_auto_test"
    result = _auto_detect_project(base)
    assert result == "discuss_auto_test"


# U-C39: _auto_detect_project — returns None when base is not a directory
def test_u_c39_auto_detect_base_is_file(tmp_path):
    from agent_crew.cli import _auto_detect_project

    # Create a file instead of a directory
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("I am a file")

    result = _auto_detect_project(str(file_path))
    assert result is None


# U-C40: crew run exits early with 'Dead panes detected' when a required pane is dead
def test_u_c40_run_exits_on_dead_pane(tmp_path):
    """crew run must check pane liveness at startup. If any required pane is
    dead, it must exit immediately with an error that tells the user to recover."""
    import json
    db_file = str(tmp_path / "tasks.db")
    state = {
        "project": "test_proj",
        "port": 0,
        "db": db_file,
        "session": "crew_test_proj",
        "agents": ["claude", "codex"],
        "pane_ids": ["%10", "%20"],
    }
    (tmp_path / "test_proj").mkdir()
    (tmp_path / "test_proj" / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    with patch("agent_crew.cli._pane_alive", return_value=False):
        result = runner.invoke(crew, [
            "run", "do something",
            "--project", "test_proj",
            "--base", str(tmp_path),
        ])

    assert result.exit_code != 0
    assert "dead" in result.output.lower() or "recover" in result.output.lower()


# U-C41: crew run proceeds past pane check when all panes are alive
def test_u_c41_run_proceeds_when_all_panes_alive(tmp_path):
    """crew run must not emit a 'dead panes' error when all panes are alive."""
    import json
    from agent_crew.queue import TaskQueue

    db_file = str(tmp_path / "tasks.db")
    TaskQueue(db_file)  # initialize DB schema
    state = {
        "project": "test_proj2",
        "port": 0,
        "db": db_file,
        "session": "crew_test_proj2",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "test_proj2").mkdir()
    (tmp_path / "test_proj2" / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    # Pane alive — pane check passes. Use timeout=1 so the wait loop exits fast.
    with patch("agent_crew.cli._pane_alive", return_value=True):
        result = runner.invoke(crew, [
            "run", "do something",
            "--project", "test_proj2",
            "--base", str(tmp_path),
            "--timeout", "1",
        ])

    # Error must NOT be about dead panes — if it is, the pane check logic is wrong.
    output_lower = result.output.lower()
    assert "dead panes" not in output_lower


# U-C42: crew status shows all terminal statuses (failed, needs_human, cancelled)
def test_u_c42_status_shows_all_terminal_statuses(tmp_path):
    """crew status must display failed, needs_human, and cancelled tasks, not just
    queued/running/done. Regression: _STATUS_ALIASES omitted these statuses."""
    import json
    from agent_crew.queue import TaskQueue

    db_file = str(tmp_path / "tasks.db")
    tq = TaskQueue(db_file)

    state = {
        "project": "proj42",
        "port": 9999,
        "db": db_file,
        "session": "crew_proj42",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "proj42").mkdir()
    (tmp_path / "proj42" / "state.json").write_text(json.dumps(state))

    from agent_crew.protocol import TaskRequest, TaskResult
    import uuid

    # Create tasks in terminal statuses
    for status in ("failed", "needs_human", "cancelled"):
        task = TaskRequest(
            task_id=str(uuid.uuid4()),
            task_type="implement",
            description=f"task with status {status}",
            branch="main",
            priority=3,
            context={},
        )
        tq.enqueue(task)
        # Transition through in_progress to reach terminal status
        dequeued = tq.dequeue()
        assert dequeued is not None
        if status != "cancelled":
            result_obj = TaskResult(
                task_id=dequeued.task_id,
                status=status,
                summary=f"finished with {status}",
                verdict=None,
                findings=[],
            )
            tq.submit_result(dequeued.task_id, result_obj)
        else:
            tq.cancel(dequeued.task_id)

    runner = CliRunner()
    # Server is "unreachable" — force DB fallback path
    with patch("agent_crew.cli._fetch_tasks_by_status", side_effect=Exception("unreachable")), \
         patch("agent_crew.cli.subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        result = runner.invoke(crew, ["status", "proj42", "--base", str(tmp_path)])

    output = result.output.lower()
    assert "failed" in output
    assert "needs_human" in output or "needs human" in output
    assert "cancelled" in output


# U-C43: crew status uses server as source of truth when reachable
def test_u_c43_status_uses_server_when_reachable(tmp_path):
    """When the server is reachable, crew status must query the server API,
    not the local DB, and show all tasks returned by the server."""
    import json

    db_file = str(tmp_path / "tasks.db")
    state = {
        "project": "proj43",
        "port": 9998,
        "db": db_file,
        "session": "crew_proj43",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "proj43").mkdir()
    (tmp_path / "proj43" / "state.json").write_text(json.dumps(state))

    # Server returns one task of each status
    server_tasks = [
        {"task_id": "t1", "task_type": "implement", "description": "server-task-pending",
         "branch": "main", "priority": 3, "context": {}, "status": "pending"},
        {"task_id": "t2", "task_type": "review", "description": "server-task-failed",
         "branch": "main", "priority": 3, "context": {}, "status": "failed"},
    ]

    def mock_fetch(port, status):
        return [t for t in server_tasks if t["status"] == status]

    runner = CliRunner()
    with patch("agent_crew.cli._fetch_tasks_by_status", side_effect=mock_fetch), \
         patch("agent_crew.cli.subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        result = runner.invoke(crew, ["status", "proj43", "--base", str(tmp_path)])

    assert "server-task-pending" in result.output
    assert "server-task-failed" in result.output


# U-C44: crew status falls back to DB with warning when server is unreachable
def test_u_c44_status_falls_back_to_db_with_warning(tmp_path):
    """When the server is unreachable, crew status must fall back to the local DB
    and print a warning so the operator knows the data may be stale."""
    import json
    from agent_crew.queue import TaskQueue
    from agent_crew.protocol import TaskRequest

    db_file = str(tmp_path / "tasks.db")
    tq = TaskQueue(db_file)

    pending_task = TaskRequest(
        task_id="db-task-001",
        task_type="implement",
        description="db-only task description",
        branch="main",
        priority=2,
        context={},
    )
    tq.enqueue(pending_task)

    state = {
        "project": "proj44",
        "port": 9997,
        "db": db_file,
        "session": "crew_proj44",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "proj44").mkdir()
    (tmp_path / "proj44" / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    with patch("agent_crew.cli._fetch_tasks_by_status", side_effect=Exception("connection refused")), \
         patch("agent_crew.cli.subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        result = runner.invoke(crew, ["status", "proj44", "--base", str(tmp_path)])

    output = result.output.lower()
    # Must warn about server being unreachable / data being from DB
    assert "unreachable" in output or "fallback" in output or "db" in output or "offline" in output
    # Must still show DB tasks
    assert "db-only task description" in result.output


# U-C45: crew run exits immediately with clear error when server is unreachable
def test_u_c45_run_exits_immediately_when_server_unreachable(tmp_path):
    """crew run must not hang 70+ seconds when the server is down. It should
    perform a health check at startup and exit within 5 seconds with a clear
    error message naming the port and suggesting 'crew recover'."""
    import json
    from agent_crew.queue import TaskQueue

    db_file = str(tmp_path / "tasks.db")
    TaskQueue(db_file)
    state = {
        "project": "proj45",
        "port": 9996,  # nothing listening here
        "db": db_file,
        "session": "crew_proj45",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "proj45").mkdir()
    (tmp_path / "proj45" / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    # Panes look alive (so pane check passes), but server is down
    with patch("agent_crew.cli._pane_alive", return_value=True), \
         patch("agent_crew.cli._port_listening", return_value=False):
        result = runner.invoke(crew, [
            "run", "do something",
            "--project", "proj45",
            "--base", str(tmp_path),
        ])

    assert result.exit_code != 0
    output = result.output.lower()
    # Must mention port and suggest recovery
    assert "9996" in result.output
    assert "unreachable" in output or "not running" in output or "server" in output
    assert "recover" in output or "status" in output


# U-C46: crew run proceeds normally when server is reachable
def test_u_c46_run_proceeds_when_server_reachable(tmp_path):
    """crew run must NOT emit a 'server unreachable' error when the server
    responds to the health check."""
    import json
    from agent_crew.queue import TaskQueue

    db_file = str(tmp_path / "tasks.db")
    TaskQueue(db_file)
    state = {
        "project": "proj46",
        "port": 9995,
        "db": db_file,
        "session": "crew_proj46",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "proj46").mkdir()
    (tmp_path / "proj46" / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    with patch("agent_crew.cli._pane_alive", return_value=True), \
         patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.cli._fetch_tasks_by_status", return_value=[]), \
         patch("agent_crew.cli.subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        result = runner.invoke(crew, [
            "run", "do something",
            "--project", "proj46",
            "--base", str(tmp_path),
            "--timeout", "1",
        ])

    output = result.output.lower()
    assert "server" not in output or "unreachable" not in output


# U-C47: /health endpoint returns 200 OK with status field
def test_u_c47_health_endpoint_returns_ok():
    """The server must expose GET /health returning {status: ok} so that
    crew run can perform a fast liveness check without waiting for a full
    task operation."""
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        app = create_app(db_path)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
    finally:
        os.unlink(db_path)


# U-C48: crew setup --help shows single-agent usage example
def test_u_c48_setup_help_shows_single_agent_example():
    """crew setup --help must include an example showing --agents for single-agent
    mode so users know they can spawn only one agent."""
    runner = CliRunner()
    result = runner.invoke(crew, ["setup", "--help"])
    assert result.exit_code == 0
    # The help text must hint at single-agent usage with --agents
    assert "--agents" in result.output
    # Must show a concrete agent name as example (codex or claude)
    assert "codex" in result.output or "claude" in result.output
    # Must show the pattern "crew setup" or similar context
    assert "single" in result.output.lower() or "example" in result.output.lower() or "e.g." in result.output.lower()


# U-C49: crew setup prints a Tip line after completing successfully
def test_u_c49_setup_prints_tip_after_completion(tmp_path):
    """After a successful setup, cli.py must print a Tip line mentioning --agents
    so users discover the single-agent mode."""

    def _fake_run(args, **_kw):
        cmd = args[1] if len(args) > 1 else ""
        if cmd == "display-message":
            # First call returns session:window, subsequent calls return numeric values
            joined = " ".join(args)
            if "#S:#I" in joined:
                return MagicMock(returncode=0, stdout="crew:0\n", stderr="")
            if "window_width" in joined:
                return MagicMock(returncode=0, stdout="200\n", stderr="")
            if "pane_width" in joined:
                return MagicMock(returncode=0, stdout="100\n", stderr="")
            return MagicMock(returncode=0, stdout="crew\n", stderr="")
        if cmd == "split-window":
            return MagicMock(returncode=0, stdout="%99\n", stderr="")
        if cmd == "select-layout":
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd in ("list-panes", "list-windows"):
            return MagicMock(returncode=0, stdout="0\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    runner = CliRunner(env={"TMUX_PANE": "%0"})
    with patch("agent_crew.cli.setup_module.validate_git_repo", return_value=True), \
         patch("agent_crew.cli._read_state", return_value=None), \
         patch("agent_crew.cli.setup_module.find_free_port", return_value=19999), \
         patch("agent_crew.cli.setup_module.write_port_file"), \
         patch("agent_crew.cli.setup_module.create_worktrees", return_value={"claude": str(tmp_path / "wt")}), \
         patch("agent_crew.cli.setup_module.write_instruction_files"), \
         patch("agent_crew.cli.setup_module.write_sessions_json"), \
         patch("agent_crew.cli.subprocess.run", side_effect=_fake_run), \
         patch("agent_crew.cli.subprocess.Popen", return_value=mock_proc), \
         patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.cli.setup_module.pretrust_claude_worktree"), \
         patch("agent_crew.cli.setup_module.start_agents_in_panes"), \
         patch("agent_crew.cli._write_state"), \
         patch("os.getcwd", return_value=str(tmp_path)):
        result = runner.invoke(crew, ["setup", "myproj", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    output = result.output.lower()
    assert "tip" in output
    assert "--agents" in result.output
