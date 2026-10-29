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
import socket
import subprocess
import time
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


def _discover_project_dirs(base_dir: str, registered: list) -> set:
    """Every directory under `base_dir` containing a state.json, plus
    whatever was explicitly registered. Split out from `e2e_project` so a
    test can call it directly and prove discovery finds an unregistered
    `crew setup` — register() calls are documentation of intent, not a
    prerequisite for cleanup (#210 review)."""
    discovered: set = set()
    for root, _dirs, files in os.walk(base_dir):
        if "state.json" in files:
            discovered.add(root)
    for base_dir_arg, project in registered:
        discovered.add(os.path.join(base_dir_arg, project))
    return discovered


def _cleanup_project_dir(proj_dir: str) -> None:
    state_path = os.path.join(proj_dir, "state.json")
    try:
        state = json.loads(open(state_path).read())
    except (OSError, ValueError):
        return

    terminate_pids([state.get("server_pid")])

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
        for proj_dir in _discover_project_dirs(base_dir, registered):
            _cleanup_project_dir(proj_dir)


# ── #879: crew server reaper + port isolation ────────────────────────────────

CREW_PORT_RANGE = range(8100, 8200)


def _os_ephemeral_port() -> int:
    """A port the kernel considers free, taken from its ephemeral range —
    never the 8100-8199 crew range a live dispatcher would use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pid_alive(pid: int) -> bool:
    """True if `pid` is running. A zombie counts as dead; it is not reaped
    here, so the owning Popen still sees its real exit status."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def terminate_pids(pids, grace: float = 3.0) -> None:
    """SIGTERM every pid, then SIGKILL whatever is still alive after `grace`."""
    pids = {p for p in pids if isinstance(p, int) and p > 1 and p != os.getpid()}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.05)
    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _state_server_pids(root: str) -> set:
    pids: set = set()
    for dirpath, _dirs, files in os.walk(root):
        if "state.json" not in files:
            continue
        try:
            pid = json.loads(open(os.path.join(dirpath, "state.json")).read()).get("server_pid")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(pid, int):
            pids.add(pid)
    return pids


class CrewServerReaper:
    """Records every crew server a test starts and kills them all on exit.

    `crew setup` and `crew recover` each spawn a uvicorn and overwrite
    state.json's `server_pid`, so reading state.json once at the end misses
    the setup-time server whenever recover started a second one (#879).
    The reaper therefore records pids at spawn time (Popen of the crew
    server command, plus the listener pid `_server_pid` resolves behind an
    `sg` wrapper) and *also* sweeps every state.json under `root`.

    It also points the allocator's scan at a kernel-chosen ephemeral port,
    so a test server can never occupy — or look like — a live crew in
    8100-8199. Tests only; product defaults are untouched.
    """

    def __init__(self, monkeypatch, root: str):
        self._mp = monkeypatch
        self.root = str(root)
        self.pids: set = set()
        self.ports: set = set()
        self.thread_servers: list = []

    def __enter__(self):
        from agent_crew import cli as cli_module
        from agent_crew import setup as setup_module

        reaper = self
        real_find_free_port = setup_module.find_free_port
        real_server_pid = cli_module._server_pid
        real_popen = subprocess.Popen

        def find_free_port(start=8100, *, base=None, project="", limit=65535):
            if start in CREW_PORT_RANGE:
                start = _os_ephemeral_port()
                while start in CREW_PORT_RANGE:  # paranoia: tiny ip_local_port_range
                    start = _os_ephemeral_port()
            port = real_find_free_port(start, base=base, project=project, limit=limit)
            reaper.ports.add(port)
            return port

        def server_pid(proc, command, port):
            pid = real_server_pid(proc, command, port)
            reaper.pids.add(pid)
            return pid

        class RecordingPopen(real_popen):
            def __init__(self, args, *a, **kw):
                super().__init__(args, *a, **kw)
                if isinstance(args, (list, tuple)) and "agent_crew.server:app" in args:
                    reaper.pids.add(self.pid)

        self._mp.setattr(setup_module, "find_free_port", find_free_port)
        self._mp.setattr(cli_module, "_server_pid", server_pid)
        self._mp.setattr(cli_module.subprocess, "Popen", RecordingPopen)
        return self

    def all_pids(self) -> set:
        return self.pids | _state_server_pids(self.root)

    def serve_in_thread(self, app, port: int):
        """Run `app` with uvicorn in a daemon thread of this process.

        Such a server has no pid of its own, so pid reaping cannot see it;
        reap() stops it instead, whether or not the test got that far.
        """
        import threading

        import uvicorn

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        self.thread_servers.append((server, thread))
        self.ports.add(port)
        thread.start()
        return server, thread

    def reap(self) -> None:
        for server, _ in self.thread_servers:
            server.should_exit = True
        for _, thread in self.thread_servers:
            thread.join(timeout=10.0)
        terminate_pids(self.all_pids())

    def __exit__(self, *exc):
        self.reap()
        return False


@pytest.fixture(autouse=True)
def crew_server_reaper(monkeypatch, tmp_path):
    """Autouse for every E2E test: no crew server outlives its test, on
    success or failure, and none is ever started in the crew port range."""
    with CrewServerReaper(monkeypatch, tmp_path) as reaper:
        yield reaper
