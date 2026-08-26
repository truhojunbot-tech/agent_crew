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


@pytest.fixture
def e2e_project(monkeypatch, isolated_tmux_session):
    """Isolates `crew setup`/`recover` into `isolated_tmux_session` and
    cleans up every artifact a test may have created.

    Usage: call the fixture with each (base_dir, project) pair the test
    set up, right after the `crew setup`/`recover` invocation that created
    it, so teardown knows what to clean up:

        def test_something(e2e_project, base_dir):
            runner.invoke(crew, ["setup", "myproj", "--base", base_dir])
            e2e_project(base_dir, "myproj")
            ...

    Teardown order: kill the crew server process (by pid, from state.json)
    first — while it's still able to see its own tmux panes for any
    graceful-shutdown bookkeeping — then let `isolated_tmux_session`'s own
    finalizer kill the tmux session. Each step is independently
    best-effort so one failure doesn't skip the rest.
    """
    monkeypatch.setenv("AGENT_CREW_TMUX_SESSION", isolated_tmux_session)
    registered: list[tuple[str, str]] = []

    def register(base_dir: str, project: str) -> None:
        registered.append((base_dir, project))

    try:
        yield register
    finally:
        for base_dir, project in registered:
            state_path = os.path.join(base_dir, project, "state.json")
            try:
                state = json.loads(open(state_path).read())
            except (OSError, ValueError):
                continue

            pid = state.get("server_pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

            # Belt-and-suspenders: some shells survive the tmux pane's HUP
            # long enough to relaunch crew-log-viewer once more before
            # exiting. Kill any watch loop for this project's logs by the
            # log path baked into its command line.
            db_file = state.get("db", "")
            proj_dir = os.path.dirname(db_file) if db_file else ""
            if proj_dir:
                for role in ("implementer", "reviewer", "tester"):
                    log_path = os.path.join(proj_dir, f"dispatch_{role}.log")
                    subprocess.run(
                        ["pkill", "-f", f"crew-log-viewer {log_path}"],
                        capture_output=True,
                    )
