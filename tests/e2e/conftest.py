"""E2E test fixtures — shared across all e2e tests.

Issue #207: tests that exercise `crew setup` (and `crew recover`'s
session-gone fallback) create real tmux panes, a real crew dispatcher
server process, and — in dispatcher mode — a `crew-log-viewer` watch loop
(``while true; do crew-log-viewer <log>; sleep 1; done``) in its own pane.

``_resolve_tmux_window`` (cli.py) resolves the target tmux session by
walking up from the test process's own PID to whatever tmux session
launched it. When these tests are run interactively from inside a real,
operational tmux session (e.g. a bot's own coordinator pane), that IS the
target — every pane, the log-viewer loop, and (if a health check reads the
now-multi-pane session's "active pane" and finds a leftover test shell) a
misdirected restart command can all land in production.

The fixtures below force every such test into a disposable tmux session
via the ``AGENT_CREW_TMUX_SESSION`` override, and guarantee that session
and everything it spawned — panes, the log-viewer loop, and the crew
server process (which is a detached background process, not itself living
inside a pane) — are gone afterward, even if the test fails.
"""
import json
import os
import signal
import subprocess
import uuid

import pytest


@pytest.fixture
def base_dir(tmp_path):
    d = tmp_path / "base"
    d.mkdir()
    return str(d)


@pytest.fixture
def isolated_tmux_session():
    """A dedicated, disposable tmux session for one test.

    Created before the test runs (so a window/pane already exists for
    `crew setup` to split from) and killed unconditionally afterward —
    `tmux kill-session` tears down every pane, and the process group HUP
    that follows takes any `crew-log-viewer` watch loop with it.
    """
    name = f"crew-test-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-x", "220", "-y", "50"],
        check=True,
    )
    try:
        yield name
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def _cleanup_project_dir(proj_dir: str) -> None:
    state_path = os.path.join(proj_dir, "state.json")
    try:
        state = json.loads(open(state_path).read())
    except (OSError, ValueError):
        return

    pid = state.get("server_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    # Belt-and-suspenders: some shells survive the tmux pane's HUP long
    # enough to relaunch crew-log-viewer once more before exiting. Kill
    # any watch loop for this project's logs by the log path baked into
    # its command line.
    db_file = state.get("db", "")
    log_dir = os.path.dirname(db_file) if db_file else proj_dir
    for role in ("implementer", "reviewer", "tester"):
        log_path = os.path.join(log_dir, f"dispatch_{role}.log")
        subprocess.run(["pkill", "-f", f"crew-log-viewer {log_path}"], capture_output=True)


@pytest.fixture
def e2e_project(monkeypatch, isolated_tmux_session, base_dir):
    """Isolates `crew setup`/`recover` into `isolated_tmux_session` and
    cleans up every artifact a test may have created — the crew server
    process (by pid, from state.json) and any surviving crew-log-viewer
    watch loop. (`isolated_tmux_session`'s own finalizer separately kills
    the tmux session and everything running inside it.)

    Calling this with each (base_dir, project) pair a test sets up is
    still useful for clarity/documentation, but is NOT what makes cleanup
    reliable: `runner.invoke(crew, ["setup", ...])` never raises — Click's
    CliRunner captures any exception into `result.exception` — so a setup
    that spawns its server and writes state.json before failing later
    still returns normally, and the explicit register() call after it
    still runs. The real gap that mattered (#210 review) is a test whose
    own assertions raise *before* it gets to call register() at all, or a
    future test that simply forgets to. Teardown closes that gap by
    additionally walking `base_dir` for every `state.json` any `crew
    setup` under it wrote, register() calls or not.
    """
    monkeypatch.setenv("AGENT_CREW_TMUX_SESSION", isolated_tmux_session)
    registered: list[tuple[str, str]] = []

    def register(base_dir_arg: str, project: str) -> None:
        registered.append((base_dir_arg, project))

    try:
        yield register
    finally:
        discovered: set[str] = set()
        for root, _dirs, files in os.walk(base_dir):
            if "state.json" in files:
                discovered.add(root)
        for base_dir_arg, project in registered:
            discovered.add(os.path.join(base_dir_arg, project))

        for proj_dir in discovered:
            _cleanup_project_dir(proj_dir)
