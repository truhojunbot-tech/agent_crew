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


# U-C22: _auto_detect_project — returns None when base dir doesn't exist
def test_u_c22_auto_detect_nonexistent_base():
    from agent_crew.cli import _auto_detect_project
    result = _auto_detect_project("/nonexistent/path/that/does/not/exist")
    assert result is None


# U-C23: _auto_detect_project — returns None when no projects exist
def test_u_c23_auto_detect_no_projects(tmp_path):
    from agent_crew.cli import _auto_detect_project
    base = str(tmp_path)
    result = _auto_detect_project(base)
    assert result is None


# U-C24: _auto_detect_project — returns most recently modified project
def test_u_c24_auto_detect_most_recent_project(tmp_path):
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


# U-C25: _auto_detect_project — ignores dirs without state.json
def test_u_c25_auto_detect_ignores_dirs_without_state(tmp_path):
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


# U-C26: _auto_detect_project — works with tilde expansion
def test_u_c26_auto_detect_tilde_expansion(tmp_path, monkeypatch):
    from agent_crew.cli import _auto_detect_project
    import json
    import os

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


# U-C27: _auto_detect_project — handles exception gracefully
def test_u_c27_auto_detect_handles_exception(tmp_path):
    from agent_crew.cli import _auto_detect_project
    from unittest.mock import patch

    base = str(tmp_path)

    # Mock os.listdir to raise an exception
    with patch("os.listdir", side_effect=PermissionError("Access denied")):
        result = _auto_detect_project(base)

    assert result is None


# U-C28: crew run auto-detects project when --db and --project not provided
def test_u_c28_run_auto_detect_project(tmp_path):
    from agent_crew.cli import _auto_detect_project
    import json

    runner = CliRunner()

    # Setup base directory with a project
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

    # Try to run without --db or --project; it should auto-detect
    result = runner.invoke(crew, [
        "run", "test task",
        "--base", base,
    ])

    # We expect it to fail because we don't have a real server, but the
    # error should NOT be about missing --db/--project
    assert "--db" not in result.output or "--project" not in result.output or \
           "auto_test" in result.output or result.exit_code == 0


# U-C29: crew discuss auto-detects project when --db and --project not provided
def test_u_c29_discuss_auto_detect_project(tmp_path):
    import json

    runner = CliRunner()

    # Setup base directory with a project
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

    # Try to run without --db or --project; it should auto-detect
    result = runner.invoke(crew, [
        "discuss", "test topic",
        "--base", base,
    ])

    # We expect it to fail because we don't have a real server, but the
    # error should NOT be about missing --db/--project
    assert "--db" not in result.output or "--project" not in result.output or \
           "discuss_auto_test" in result.output or result.exit_code == 0


# U-C30: _auto_detect_project — returns None when base is not a directory
def test_u_c30_auto_detect_base_is_file(tmp_path):
    from agent_crew.cli import _auto_detect_project

    # Create a file instead of a directory
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("I am a file")

    result = _auto_detect_project(str(file_path))
    assert result is None
