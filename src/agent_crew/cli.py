import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import click

from agent_crew import setup as setup_module

_DEFAULT_BASE = os.path.expanduser("~/.agent_crew")
_DEFAULT_AGENTS = "claude,codex,gemini"


def _proj_dir(base: str, project: str) -> str:
    return os.path.join(base, project)


def _crew_log(proj_dir: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(os.path.join(proj_dir, "crew.log"), "a") as f:
            f.write(line)
    except OSError:
        pass


def _tmux_snapshot(session: str) -> str:
    """Return a one-line summary of all panes in the session."""
    r = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", session,
         "-F", "#{pane_id}:#{pane_current_path}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip().replace("\n", " | ") if r.returncode == 0 else "(session gone)"


def _state_path(base: str, project: str) -> str:
    return os.path.join(base, project, "state.json")


def _read_state(base: str, project: str) -> dict | None:
    path = _state_path(base, project)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _write_state(base: str, project: str, state: dict) -> None:
    os.makedirs(_proj_dir(base, project), exist_ok=True)
    with open(_state_path(base, project), "w") as f:
        json.dump(state, f, indent=2)


def _port_listening(port: int, timeout: float = 5.0) -> bool:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


_PANE_IDLE_PATTERNS = [
    "$",            # shell prompt (agent CLI exited)
    "❯",            # zsh prompt
    ">>>",          # python repl
    "Completed",    # common completion signal
]


def _capture_pane(target: str) -> str | None:
    """Capture last 5 lines of a tmux pane. target is a pane_id (e.g. '%42')
    or a session:window.pane spec. Returns None if tmux fails."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", "-5"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _pane_looks_idle(pane_output: str) -> bool:
    """Heuristic: does the pane output suggest the agent is idle/done?"""
    last_line = pane_output.rstrip().rsplit("\n", 1)[-1] if pane_output.strip() else ""
    return any(pat in last_line for pat in _PANE_IDLE_PATTERNS)


def _pane_alive(pane_id: str) -> bool:
    """Return True if the tmux pane still exists."""
    r = subprocess.run(
        ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_id}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == pane_id


def _verify_delivery(port: int, task_id: str, timeout: float = 15.0) -> bool:
    """Poll task status until it transitions out of 'pending' (i.e. pane received it).
    Returns True if delivered, False if still pending after timeout."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/tasks/{task_id}", timeout=2
            ) as resp:
                task = json.loads(resp.read())
            if task.get("status") != "pending":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


_STATUS_ALIASES = (
    ("queued", "pending"),
    ("running", "in_progress"),
    ("done", "completed"),
)

def _fetch_tasks_by_status(port: int, status: str) -> list[dict]:
    import urllib.request

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/tasks?status={status}", timeout=2
    ) as resp:
        return json.loads(resp.read())


@click.group()
def crew():
    """agent_crew — multi-agent development crew CLI."""


@crew.command()
@click.argument("project")
@click.option("--agents", default=_DEFAULT_AGENTS, help="Comma-separated agent names")
@click.option("--base", default=_DEFAULT_BASE, show_default=True, help="Base directory for state/worktrees")
def setup(project: str, agents: str, base: str):
    """Configure environment for PROJECT."""
    cwd = os.getcwd()
    if not setup_module.validate_git_repo(cwd):
        raise click.ClickException("not a git repository")

    existing_state = _read_state(base, project)
    if existing_state is not None:
        existing_pane_ids = existing_state.get("pane_ids", [])
        alive_panes = [p for p in existing_pane_ids if _pane_alive(p)]
        server_alive = _port_listening(existing_state.get("port", 0), timeout=1.0)
        if alive_panes and server_alive:
            click.echo(
                f"Project {project!r} is already set up with {len(alive_panes)} live pane(s) "
                f"and server on port {existing_state['port']}. Reusing."
            )
            return
        click.echo(f"Project {project!r} has stale state (panes dead or server down). Re-initializing...")

    # Warn if other project panes exist in this window
    tmux_pane_env_pre = os.environ.get("TMUX_PANE", "")
    if tmux_pane_env_pre:
        other_states = []
        for entry in os.listdir(base) if os.path.isdir(base) else []:
            if entry == project:
                continue
            st = _read_state(base, entry)
            if st and any(_pane_alive(p) for p in st.get("pane_ids", [])):
                other_states.append(entry)
        if other_states:
            click.echo(
                f"Warning: live panes from other project(s) detected: {other_states}. "
                "These will share the same tmux window."
            )
            if not click.confirm("Continue?", default=False):
                raise click.ClickException("Aborted.")

    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    proj_dir = _proj_dir(base, project)
    os.makedirs(proj_dir, exist_ok=True)
    _crew_log(proj_dir, f"setup START agents={agent_list}")

    # Worktrees
    worktrees = setup_module.create_worktrees(project, base, agent_list)
    _crew_log(proj_dir, f"worktrees created: {list(worktrees.values())}")

    # Port + port file
    port = setup_module.find_free_port()
    port_file = os.path.join(proj_dir, "port")
    setup_module.write_port_file(port_file, port)

    # Instruction files (into each worktree)
    setup_module.write_instruction_files(worktrees, project, port_file)

    # sessions.json
    sessions_file = os.path.join(proj_dir, "sessions.json")
    agent_dicts = [{"name": a, "pane": i} for i, a in enumerate(agent_list)]
    setup_module.write_sessions_json(sessions_file, agent_dicts)

    # Agent panes live in the caller's own tmux window. Coordinator stays on
    # the left (full height); agents stack vertically on the right via
    # main-vertical layout. No width preflight — narrow windows get narrower
    # agent panes rather than an error.
    tmux_pane_env = os.environ.get("TMUX_PANE", "")
    if not tmux_pane_env:
        raise click.ClickException(
            "crew setup must run inside a tmux session. "
            "Start tmux first, then re-run."
        )
    current = subprocess.run(
        ["tmux", "display-message", "-p", "-t", tmux_pane_env, "#S:#I"],
        capture_output=True, text=True,
    )
    if current.returncode != 0 or not current.stdout.strip():
        raise click.ClickException(
            f"failed to read caller tmux session: {current.stderr.strip() or current.stdout.strip()}"
        )
    session_name, _, window_index = current.stdout.strip().partition(":")
    window_index = window_index or "0"
    window_target = f"{session_name}:{window_index}"
    _crew_log(proj_dir, f"tmux using caller session={session_name} window={window_index}: {_tmux_snapshot(session_name)}")

    pane_ids: list[str] = []
    for agent, wt_path in worktrees.items():
        result = subprocess.run(
            ["tmux", "split-window", "-h", "-c", wt_path, "-t", window_target,
             "-P", "-F", "#{pane_id}"],
            capture_output=True, text=True,
        )
        pane_id = result.stdout.strip()
        rc = result.returncode
        _crew_log(proj_dir, f"split-window agent={agent} rc={rc} pane_id={pane_id!r} stderr={result.stderr.strip()!r}")
        if pane_id:
            pane_ids.append(pane_id)

    # Coordinator fills the left column; agent panes stack top-to-bottom on
    # the right. main-vertical reorganizes panes regardless of split order.
    layout_result = subprocess.run(
        ["tmux", "select-layout", "-t", window_target, "main-vertical"],
        capture_output=True, text=True,
    )
    _crew_log(proj_dir, f"select-layout rc={layout_result.returncode} after: {_tmux_snapshot(session_name)}")

    # Write pane_map.json — server reads this at startup for push routing.
    # Two key flavors share one dict:
    #   role key (implementer/reviewer/tester) — routes implement/review/test tasks
    #   agent-name key (claude/codex/gemini)    — routes discuss tasks (panelists
    #                                              fan out per agent)
    pane_map: dict[str, str] = {}
    for a, pid in zip(agent_list, pane_ids):
        role = setup_module._AGENT_TO_ROLE.get(a, "implementer")
        pane_map[role] = pid
        pane_map[a] = pid
    pane_map_file = os.path.join(proj_dir, "pane_map.json")
    with open(pane_map_file, "w") as f:
        json.dump(pane_map, f)
    _crew_log(proj_dir, f"pane_map written: {pane_map}")

    # Start server — include sys.path in PYTHONPATH so subprocess can import agent_crew.
    # AGENT_CREW_PANE_MAP tells the server where to push tasks; AGENT_CREW_PORT is
    # embedded in push messages so agents know where to POST results.
    db_file = os.path.join(proj_dir, "tasks.db")
    pythonpath = os.pathsep.join(p for p in sys.path if p)
    server_env = {
        **os.environ,
        "AGENT_CREW_DB": db_file,
        "AGENT_CREW_PANE_MAP": pane_map_file,
        "AGENT_CREW_PORT": str(port),
        "PYTHONPATH": pythonpath,
    }
    log_file = open(os.path.join(proj_dir, "server.log"), "w")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_crew.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        env=server_env,
        stdout=log_file,
        stderr=log_file,
    )
    _crew_log(proj_dir, f"server started pid={server_proc.pid} port={port}")

    # Wait until server is ready (up to 15 s) before returning
    if not _port_listening(port, timeout=15.0):
        server_proc.terminate()
        log_file.close()
        _crew_log(proj_dir, f"server failed to start on port {port}")
        raise click.ClickException(
            f"server failed to start on port {port}. "
            f"Check {os.path.join(proj_dir, 'server.log')}"
        )

    # Pre-accept Claude's workspace-trust dialog for the claude worktree path
    # so --dangerously-skip-permissions doesn't get blocked on an interactive
    # "Trust this folder?" prompt at first launch.
    setup_module.pretrust_claude_worktree(worktrees)

    # Start agent CLIs. Agents wait for pane pushes; no kickoff prompt, no
    # polling loop. Instructions live in each worktree's CLAUDE.md/AGENTS.md/GEMINI.md
    # which the CLIs auto-load.
    setup_module.start_agents_in_panes(
        session_name, agent_list, pane_targets=pane_ids or None, worktrees=worktrees
    )
    _crew_log(proj_dir, f"agents started pane_ids={pane_ids}")

    _write_state(base, project, {
        "project": project,
        "port": port,
        "port_file": port_file,
        "session": session_name,
        "window": window_index,
        "pane_ids": pane_ids,
        "pane_map": pane_map,
        "agents": agent_list,
        "worktrees": worktrees,
        "db": db_file,
        "server_pid": server_proc.pid,
        "sessions_file": sessions_file,
    })

    click.echo(f"Setup complete: {project} on port {port}")


@crew.command()
@click.argument("project")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
@click.option("--preview", default=200, type=int, show_default=True,
              help="Chars of result summary to preview for completed tasks (0 to disable)")
def status(project: str, base: str, preview: int):
    """Show status of PROJECT."""
    state = _read_state(base, project)
    if state is None:
        raise click.ClickException(f"project {project!r} not found. Run setup first.")

    session_name = state["session"]
    port = state["port"]
    agent_list = state["agents"]
    db_file = state.get("db", "")
    click.echo(f"Project: {project}")
    click.echo(f"Port: {port}")

    # Result cache for completed-task preview. Loaded from DB directly — the
    # list_tasks API only returns TaskRequest shape (no summary/verdict).
    result_cache: dict = {}
    _queue_for_preview = None
    if preview > 0 and db_file and os.path.exists(db_file):
        try:
            from agent_crew.queue import TaskQueue
            _queue_for_preview = TaskQueue(db_file)
        except Exception:
            _queue_for_preview = None

    def _load_result(task_id: str):
        if _queue_for_preview is None:
            return None
        if task_id in result_cache:
            return result_cache[task_id]
        try:
            r = _queue_for_preview.get_result(task_id)
        except Exception:
            r = None
        result_cache[task_id] = r
        return r

    try:
        task_groups = {
            display_status: _fetch_tasks_by_status(port, api_status)
            for display_status, api_status in _STATUS_ALIASES
        }
    except Exception:
        click.echo("\nTASKS: (server unreachable)")
    else:
        total = sum(len(tasks) for tasks in task_groups.values())
        click.echo(f"\nTasks: {total}")
        for display_status, _ in _STATUS_ALIASES:
            tasks = task_groups[display_status]
            click.echo(f"\n{display_status.upper()} ({len(tasks)}):")
            for t in tasks:
                def _get(key, default=None):
                    if isinstance(t, dict):
                        return t.get(key, default)
                    return getattr(t, key, default)
                tid = _get("task_id") or "?"
                ttype = _get("task_type") or "?"
                prio = _get("priority")
                desc = str(_get("description") or "")[:50]
                ctx = _get("context") or {}
                agent = ""
                if isinstance(ctx, dict) and ctx.get("agent"):
                    agent = f" @{ctx['agent']}"
                click.echo(f"  [{tid}] p{prio} {ttype}{agent} — {desc}")
                # Preview result summary/verdict for completed tasks so you can
                # peek at discuss outputs without re-running the whole panel.
                if display_status == "done" and preview > 0 and tid != "?":
                    r = _load_result(tid)
                    if r is not None:
                        verdict = getattr(r, "verdict", None)
                        summary = getattr(r, "summary", "") or ""
                        if verdict:
                            click.echo(f"      verdict: {verdict}")
                        if summary:
                            snippet = summary.replace("\n", " ").strip()
                            if len(snippet) > preview:
                                snippet = snippet[:preview] + "…"
                            click.echo(f"      summary: {snippet}")

    click.echo("\nPanes:")
    pane_targets = state.get("pane_ids") or [
        f"{session_name}:0.{i}" for i in range(len(agent_list))
    ]
    for agent, target in zip(agent_list, pane_targets):
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            capture_output=True,
        )
        alive = result.returncode == 0
        click.echo(f"  {agent} ({target}): {'alive' if alive else 'dead'}")


@crew.command()
@click.argument("project")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
def recover(project: str, base: str):
    """Recover a crashed PROJECT: restart server and recreate tmux panes."""
    state = _read_state(base, project)
    if state is None:
        raise click.ClickException(f"project {project!r} not found. Run setup first.")

    session_name = state["session"]
    port = state["port"]
    worktrees = state.get("worktrees", {})
    db_file = state["db"]
    proj_dir = _proj_dir(base, project)
    recovered = []

    # Restart server if not listening
    if not _port_listening(port, timeout=1.0):
        pythonpath = os.pathsep.join(p for p in sys.path if p)
        server_env = {**os.environ, "AGENT_CREW_DB": db_file, "PYTHONPATH": pythonpath}
        log_path = os.path.join(proj_dir, "server.log")
        log_file = open(log_path, "a")
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent_crew.server:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
            env=server_env,
            stdout=log_file,
            stderr=log_file,
        )
        if _port_listening(port, timeout=15.0):
            state["server_pid"] = server_proc.pid
            _write_state(base, project, state)
            recovered.append("server")
        else:
            server_proc.terminate()
            log_file.close()
            raise click.ClickException(
                f"Failed to restart server on port {port}. Check {log_path}"
            )

    # Recreate tmux session if gone
    has_session = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    ).returncode == 0

    if not has_session:
        # Original session is gone — fall back to current tmux session/window.
        tmux_pane_env = os.environ.get("TMUX_PANE", "")
        dm_target = ["-t", tmux_pane_env] if tmux_pane_env else []
        current = subprocess.run(
            ["tmux", "display-message", "-p"] + dm_target + ["#S:#I"],
            capture_output=True, text=True,
        )
        if current.returncode != 0 or not current.stdout.strip():
            raise click.ClickException(
                f"tmux session {session_name!r} missing and not running inside tmux. "
                f"Start a tmux session and re-run recover."
            )
        cur_session, _, cur_window = current.stdout.strip().partition(":")
        window_target = f"{cur_session}:{cur_window or '0'}"
        pane_ids: list[str] = []
        for _, wt_path in worktrees.items():
            result = subprocess.run(
                ["tmux", "split-window", "-h", "-c", wt_path, "-t", window_target,
                 "-P", "-F", "#{pane_id}"],
                capture_output=True, text=True,
            )
            pane_id = result.stdout.strip()
            if pane_id:
                pane_ids.append(pane_id)
        subprocess.run(
            ["tmux", "select-layout", "-t", window_target, "main-vertical"],
            capture_output=True,
        )
        if pane_ids:
            setup_module.start_agents_in_panes(
                cur_session,
                state.get("agents", []),
                pane_targets=pane_ids,
                worktrees=worktrees,
            )
            state["session"] = cur_session
            state["window"] = cur_window or "0"
            state["pane_ids"] = pane_ids
            _write_state(base, project, state)
        recovered.append("tmux")
    else:
        # Session exists — check each agent pane individually and recreate
        # only the dead ones so alive panes keep their running agent CLIs.
        agent_list = state.get("agents", [])
        existing_pane_ids = state.get("pane_ids", [])
        if agent_list and len(existing_pane_ids) == len(agent_list):
            window_target = f"{session_name}:{state.get('window', '0')}"
            new_pane_ids: list[str] = []
            dead_agents: list[str] = []
            dead_targets: list[str] = []
            for agent, pane_id in zip(agent_list, existing_pane_ids):
                if _pane_alive(pane_id):
                    new_pane_ids.append(pane_id)
                    continue
                wt_path = worktrees.get(agent, "")
                split_cmd = ["tmux", "split-window", "-h"]
                if wt_path:
                    split_cmd += ["-c", wt_path]
                split_cmd += ["-t", window_target, "-P", "-F", "#{pane_id}"]
                r = subprocess.run(split_cmd, capture_output=True, text=True)
                new_id = r.stdout.strip()
                if new_id:
                    new_pane_ids.append(new_id)
                    dead_agents.append(agent)
                    dead_targets.append(new_id)
                else:
                    new_pane_ids.append(pane_id)

            if dead_agents:
                subprocess.run(
                    ["tmux", "select-layout", "-t", window_target, "main-vertical"],
                    capture_output=True,
                )
                setup_module.start_agents_in_panes(
                    session_name, dead_agents,
                    pane_targets=dead_targets, worktrees=worktrees,
                )
                pane_map = state.get("pane_map", {})
                for a, pid in zip(agent_list, new_pane_ids):
                    role = setup_module._AGENT_TO_ROLE.get(a, "implementer")
                    pane_map[role] = pid
                    pane_map[a] = pid
                state["pane_ids"] = new_pane_ids
                state["pane_map"] = pane_map
                _write_state(base, project, state)
                pane_map_file = os.path.join(proj_dir, "pane_map.json")
                try:
                    with open(pane_map_file, "w") as f:
                        json.dump(pane_map, f)
                except OSError:
                    pass
                recovered.append(f"panes({len(dead_agents)})")

    if recovered:
        click.echo(f"Recovered: {', '.join(recovered)}")
    else:
        click.echo("Nothing to recover: server and tmux already running.")


@crew.command()
@click.argument("project")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
def teardown(project: str, base: str):
    """Tear down PROJECT."""
    state = _read_state(base, project)
    if state is None:
        raise click.ClickException(f"project {project!r} not found. Run setup first.")

    session_name = state["session"]
    agent_list = state["agents"]
    worktrees = state.get("worktrees", {})
    server_pid = state.get("server_pid")
    proj_dir = _proj_dir(base, project)

    _crew_log(proj_dir, f"teardown START session={session_name} agents={agent_list} server_pid={server_pid}")
    _crew_log(proj_dir, f"tmux before teardown: {_tmux_snapshot(session_name)}")

    # Kill only the saved agent pane_ids — never kill the session.
    # Using saved IDs is reliable even if agents cd'd away from their worktree paths.
    pane_ids = state.get("pane_ids", [])
    for pane_id in pane_ids:
        r = subprocess.run(
            ["tmux", "kill-pane", "-t", pane_id],
            capture_output=True, text=True,
        )
        _crew_log(proj_dir, f"kill-pane {pane_id} rc={r.returncode} {r.stderr.strip()}")

    _crew_log(proj_dir, f"tmux after kill-panes: {_tmux_snapshot(session_name)}")

    # Kill server
    if server_pid:
        try:
            os.kill(server_pid, signal.SIGTERM)
            _crew_log(proj_dir, f"server SIGTERM pid={server_pid}")
        except (ProcessLookupError, OSError) as e:
            _crew_log(proj_dir, f"server kill skipped pid={server_pid}: {e}")

    # Remove worktrees
    for agent in agent_list:
        wt_path = worktrees.get(agent, "")
        if wt_path:
            r = subprocess.run(["git", "worktree", "remove", "--force", wt_path],
                               capture_output=True, text=True)
            _crew_log(proj_dir, f"worktree remove {agent} rc={r.returncode}")
    subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True)

    _crew_log(proj_dir, f"teardown DONE — removing state dir {proj_dir}")
    # Remove state dir
    shutil.rmtree(proj_dir, ignore_errors=True)

    click.echo(f"Teardown complete: {project}")


@crew.command("run")
@click.argument("task")
@click.option("--db", default="", help="SQLite DB path (standalone)")
@click.option("--project", default="", help="Project name (reads DB from state)")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
@click.option("--max-iter", default=0, type=int, help="Max review iterations (0 = default)")
@click.option("--no-tester", is_flag=True, help="Skip test phase after approval")
@click.option("--branch", default="main", show_default=True)
@click.option("--timeout", default=600, type=int, show_default=True, help="Task wait timeout in seconds")
def run_cmd(task: str, db: str, project: str, base: str,
            max_iter: int, no_tester: bool, branch: str, timeout: int):
    """Run TASK through the code-review loop."""
    if not task.strip():
        raise click.UsageError("task must not be empty")

    if not db:
        if not project:
            raise click.ClickException("--db or --project is required")
        state = _read_state(base, project)
        if state is None:
            raise click.ClickException(f"project {project!r} not found")
        db = state["db"]

    from agent_crew.loop import (
        DEFAULT_MAX_ITER,
        build_feedback,
        enqueue_implement,
        enqueue_review,
        enqueue_test,
        handle_review_result,
        handle_test_result,
    )
    from agent_crew.queue import TaskQueue
    import time

    if max_iter <= 0:
        max_iter = DEFAULT_MAX_ITER

    queue = TaskQueue(db)

    wait_timeout = float(timeout)

    # Resolve pane info for pane capture fallback
    first_pane_target = ""
    if project:
        st = _read_state(base, project)
        if st:
            pane_ids = st.get("pane_ids") or []
            session_name_st = st.get("session", "")
            if pane_ids:
                first_pane_target = pane_ids[0]
            elif session_name_st:
                first_pane_target = f"{session_name_st}:0.0"

    import urllib.request as _urllib_req

    def _auto_submit_failed(task_id: str, reason: str) -> None:
        """POST a failed result so the queue unblocks when the agent misses it."""
        if not _run_port:
            return
        payload = json.dumps({
            "task_id": task_id,
            "status": "failed",
            "summary": reason,
            "findings": [],
            "verdict": None,
            "pr_number": None,
        }).encode()
        req = _urllib_req.Request(
            f"http://127.0.0.1:{_run_port}/tasks/{task_id}/result",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _urllib_req.urlopen(req, timeout=5):
                pass
            click.echo(f"  Auto-submitted failed result for {task_id!r} to unblock queue.")
        except Exception as e:
            click.echo(f"  Auto-submit failed: {e}")

    def _wait(task_id: str):
        deadline = time.time() + wait_timeout
        fallback_start = time.time() + 30  # grace period before pane checks
        pane_idle_count = 0
        while time.time() < deadline:
            result = queue.get_result(task_id)
            if result is not None:
                return result
            # Pane capture fallback: after 30s, check every 10s
            if first_pane_target and time.time() > fallback_start:
                pane_out = _capture_pane(first_pane_target)
                if pane_out and _pane_looks_idle(pane_out):
                    pane_idle_count += 1
                    if pane_idle_count >= 3:
                        reason = (
                            f"Auto-submitted: agent pane idle for 30s without POSTing result. "
                            f"Task {task_id!r} marked failed so queue can proceed."
                        )
                        click.echo(f"Warning: {reason}")
                        _auto_submit_failed(task_id, reason)
                        raise click.ClickException(
                            f"task {task_id!r}: agent idle — result auto-submitted as failed. "
                            f"Check pane manually or re-run."
                        )
                else:
                    pane_idle_count = 0
                time.sleep(10)
            else:
                time.sleep(0.5)
        raise click.ClickException(f"task {task_id!r} timed out after {wait_timeout}s")

    def _auto_resolve_gates(port: int) -> int:
        """Resolve any pending gates via HTTP. Returns count of resolved gates."""
        resolved = 0
        try:
            with _urllib_req.urlopen(
                f"http://127.0.0.1:{port}/gates/pending", timeout=2
            ) as resp:
                gates = json.loads(resp.read())
            for gate in gates:
                gate_id = gate.get("id") or gate.get("gate_id")
                if not gate_id:
                    continue
                payload = json.dumps({"status": "approved"}).encode()
                req = _urllib_req.Request(
                    f"http://127.0.0.1:{port}/gates/{gate_id}/resolve",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with _urllib_req.urlopen(req, timeout=2):
                        pass
                    click.echo(f"  Gate {gate_id} auto-approved.")
                    resolved += 1
                except Exception:
                    pass
        except Exception:
            pass
        return resolved

    def _drain_resolvable_gates(port: int) -> int:
        """Resolve gates until the pending set is empty."""
        total = 0
        while True:
            resolved = _auto_resolve_gates(port)
            if resolved == 0:
                return total
            total += resolved

    # Determine port for gate resolution (available when --project is set)
    _run_port = 0
    if project:
        _pstate = _read_state(base, project)
        if _pstate:
            _run_port = _pstate.get("port", 0)

    impl_id = enqueue_implement(queue, task, branch, port=_run_port)
    click.echo(f"[1/{max_iter}] Implementing... ({impl_id})")
    if _run_port and not _verify_delivery(_run_port, impl_id, timeout=15.0):
        click.echo(f"Warning: task {impl_id!r} still pending after 15s — agent pane may not have received it.")

    for iteration in range(1, max_iter + 1):
        _wait(impl_id)
        click.echo(f"[{iteration}/{max_iter}] Implementation done.")

        review_id = enqueue_review(queue, task, branch, prev_task_id=impl_id, port=_run_port)
        click.echo(f"[{iteration}/{max_iter}] Reviewing... ({review_id})")
        review_result = _wait(review_id)

        # pass no_tester=True here — test enqueue is handled manually below
        outcome = handle_review_result(
            review_result,
            iteration=iteration,
            max_iter=max_iter,
            no_tester=True,
            queue=queue,
        )

        if outcome == "escalate":
            click.echo(f"[{iteration}/{max_iter}] Escalated after {max_iter} iterations.")
            return

        if outcome == "approved":
            click.echo(f"[{iteration}/{max_iter}] Review approved.")
            # Auto-resolve any pending gates before proceeding
            if _run_port:
                _drain_resolvable_gates(_run_port)
            if not no_tester:
                test_id = enqueue_test(queue, task, branch, port=_run_port)
                click.echo(f"[{iteration}/{max_iter}] Testing... ({test_id})")
                test_result = _wait(test_id)
                test_outcome = handle_test_result(test_result)
                if test_outcome == "passed":
                    if _run_port:
                        _drain_resolvable_gates(_run_port)
                    click.echo(f"[{iteration}/{max_iter}] Tests passed. Loop complete.")
                    return
                else:
                    click.echo(f"[{iteration}/{max_iter}] Tests {test_outcome}. Re-implementing.")
                    impl_id = enqueue_implement(queue, task, branch,
                                               context={"retry": True}, port=_run_port)
                    continue
            else:
                if _run_port:
                    _drain_resolvable_gates(_run_port)
                click.echo(f"[{iteration}/{max_iter}] Loop complete (no tester).")
            return

        # request_changes: re-implement with feedback
        click.echo(f"[{iteration}/{max_iter}] Changes requested. Re-implementing.")
        feedback = build_feedback(review_result)
        impl_id = enqueue_implement(queue, task, branch, context={"feedback": feedback}, port=_run_port)

    click.echo(f"Max iterations ({max_iter}) reached without approval.")


@crew.command()
@click.argument("topic")
@click.option("--agents", default="",
              help="Comma-separated agent names (claude/codex/gemini). "
                   "Defaults to the project's installed agents, or "
                   f"{_DEFAULT_AGENTS!r} if --db is used standalone.")
@click.option("--perspectives", default="",
              help="Comma-separated panel perspectives (analyst/critic/advocate/risk). "
                   "Assigned to agents by position; cycles if fewer than agents. "
                   "Defaults to analyst,critic,advocate,risk.")
@click.option("--rounds", default=1, type=int, show_default=True, help="Number of discussion rounds")
@click.option("--then-run", is_flag=True, help="Trigger code-review loop after synthesis")
@click.option("--db", default="", help="SQLite DB path (standalone)")
@click.option("--project", default="", help="Project name (reads DB from state)")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
@click.option("--output", default="synthesis.md", show_default=True, help="Path to write synthesis")
@click.option("--branch", default="main", show_default=True)
@click.option("--timeout", default=300, type=int, show_default=True, help="Task wait timeout in seconds")
@click.option("--nowait", is_flag=True,
              help="Enqueue panel tasks and return immediately. Track progress via `crew status`.")
def discuss(topic: str, agents: str, perspectives: str, rounds: int, then_run: bool,
            db: str, project: str, base: str, output: str, branch: str,
            timeout: int, nowait: bool):
    """Start a panel discussion on TOPIC. TOPIC must not be empty."""
    if not topic.strip():
        raise click.UsageError("topic must not be empty")

    project_state = None
    if not db:
        if not project:
            raise click.ClickException("--db or --project is required")
        project_state = _read_state(base, project)
        if project_state is None:
            raise click.ClickException(f"project {project!r} not found")
        db = project_state["db"]

    from agent_crew.discussion import (
        DEFAULT_PERSPECTIVES, assign_perspectives, build_synthesis, enqueue_panel_tasks
    )
    from agent_crew.loop import enqueue_implement
    from agent_crew.queue import TaskQueue
    import time

    # Agents default: project's installed agents (from state) in project mode,
    # or the global default (claude,codex,gemini) in standalone mode.
    if agents.strip():
        agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    elif project_state and project_state.get("agents"):
        agent_list = list(project_state["agents"])
    else:
        agent_list = [a.strip() for a in _DEFAULT_AGENTS.split(",") if a.strip()]

    # In project mode, refuse agent names that the server can't push to —
    # pane_map is the source of truth. Silent skip here previously left
    # tasks queued forever (the analyst/critic/advocate misfire).
    if project_state:
        pane_map = project_state.get("pane_map") or {}
        unknown = [a for a in agent_list if a not in pane_map]
        if unknown:
            known = sorted(
                k for k in pane_map.keys()
                if k not in ("implementer", "reviewer", "tester")
            )
            raise click.ClickException(
                f"agent(s) {unknown} not found in project pane_map. "
                f"Known agents: {known}. "
                f"Note: analyst/critic/advocate are *perspectives* — pass them via "
                f"--perspectives, not --agents."
            )

    if perspectives.strip():
        perspective_pool = [p.strip() for p in perspectives.split(",") if p.strip()]
    else:
        perspective_pool = DEFAULT_PERSPECTIVES

    queue = TaskQueue(db)
    perspectives_map = assign_perspectives(agent_list, perspectives=perspective_pool)

    _run_port = 0
    if project:
        _pstate = _read_state(base, project)
        if _pstate:
            _run_port = _pstate.get("port", 0)

    wait_timeout = float(timeout)

    def _wait_all(task_ids: list[str], timeout: float = 0.0) -> tuple[dict, list[str]]:
        """Poll for all task results. On timeout, return (partial_done, missing_ids)
        instead of raising — the caller decides whether partial results are
        actionable (e.g. synthesize what we have) or fatal."""
        effective_timeout = timeout if timeout > 0 else wait_timeout
        deadline = time.time() + effective_timeout
        done: dict = {}
        while time.time() < deadline:
            for tid in task_ids:
                if tid not in done:
                    r = queue.get_result(tid)
                    if r is not None:
                        done[tid] = r
            if len(done) == len(task_ids):
                return done, []
            time.sleep(0.1)
        missing = [tid for tid in task_ids if tid not in done]
        return done, missing

    if nowait:
        # Fire-and-forget: enqueue round 1 only, emit task_ids, exit.
        context: dict = {"round": 1}
        task_ids = enqueue_panel_tasks(
            queue, agent_list, topic, context,
            port=_run_port, perspectives=perspectives_map,
        )
        click.echo(f"Discussion queued ({len(task_ids)} tasks). Track via `crew status`:")
        for agent, tid in zip(agent_list, task_ids):
            click.echo(f"  {agent} ({perspectives_map[agent]}): {tid}")
        return

    prior_synthesis = ""
    final_synthesis = ""
    partial_hit = False
    missing_by_round: dict = {}

    for round_num in range(1, rounds + 1):
        context = {"round": round_num}
        if round_num > 1 and prior_synthesis:
            context["prior_synthesis"] = prior_synthesis

        task_ids = enqueue_panel_tasks(
            queue, agent_list, topic, context,
            port=_run_port, perspectives=perspectives_map,
        )
        results_map, missing = _wait_all(task_ids)

        if missing:
            partial_hit = True
            missing_by_round[round_num] = missing

        # Build synthesis from whoever completed. If nobody completed, skip
        # this round's synthesis entirely rather than writing an empty file.
        panel_results = []
        for agent, tid in zip(agent_list, task_ids):
            r = results_map.get(tid)
            if r is None:
                continue
            panel_results.append({
                "agent": agent,
                "perspective": perspectives_map[agent],
                "summary": r.summary,
            })

        if not panel_results:
            break

        synthesis_label = (
            f"Round {round_num} synthesis."
            if not missing
            else f"Round {round_num} PARTIAL synthesis ({len(panel_results)}/{len(task_ids)} agents; "
                 f"missing task_ids: {', '.join(missing)})."
        )
        final_synthesis = build_synthesis(
            panel_results,
            topic=topic,
            synthesis=synthesis_label,
            decision="Proceed as discussed." if not missing else "Review partial before deciding.",
        )
        prior_synthesis = final_synthesis

        # If round N timed out, don't enqueue round N+1 — the next round should
        # have the full prior synthesis as context, not a half-baked one.
        if missing:
            break

    if final_synthesis:
        with open(output, "w") as f:
            f.write(final_synthesis)

    if partial_hit:
        click.echo(
            f"Discussion TIMED OUT after {wait_timeout:.0f}s — partial synthesis written to {output}.",
            err=True,
        )
        for round_num, missing in missing_by_round.items():
            click.echo(f"  round {round_num} missing: {', '.join(missing)}", err=True)
        click.echo(
            "Agents may still be working. Check `crew status` and re-run discuss once they finish "
            "(or raise --timeout).",
            err=True,
        )
        # Non-zero exit signals partial success to scripts; synthesis file still usable.
        raise click.exceptions.Exit(2)

    click.echo(f"Discussion complete. Synthesis written to {output}")

    if then_run:
        from agent_crew.loop import (
            DEFAULT_MAX_ITER,
            build_feedback,
            enqueue_review,
            handle_review_result,
        )

        max_iter = DEFAULT_MAX_ITER
        impl_id = enqueue_implement(queue, topic, branch, port=_run_port)
        click.echo(f"Code-review loop started: {impl_id}")

        for iteration in range(1, max_iter + 1):
            _wait_result = None
            deadline = time.time() + wait_timeout
            while time.time() < deadline:
                _wait_result = queue.get_result(impl_id)
                if _wait_result is not None:
                    break
                time.sleep(0.1)
            if _wait_result is None:
                raise click.ClickException(f"task {impl_id!r} timed out after {wait_timeout}s")

            review_id = enqueue_review(queue, topic, branch, prev_task_id=impl_id, port=_run_port)
            review_result = None
            deadline = time.time() + wait_timeout
            while time.time() < deadline:
                review_result = queue.get_result(review_id)
                if review_result is not None:
                    break
                time.sleep(0.1)
            if review_result is None:
                raise click.ClickException(f"task {review_id!r} timed out after {wait_timeout}s")

            outcome = handle_review_result(
                review_result, iteration=iteration, max_iter=max_iter,
                no_tester=True, queue=queue,
            )
            if outcome == "escalate":
                click.echo(f"Escalated after {max_iter} iterations.")
                return
            if outcome == "approved":
                click.echo("Loop complete: approved.")
                return

            feedback = build_feedback(review_result)
            impl_id = enqueue_implement(queue, topic, branch, context={"feedback": feedback}, port=_run_port)

        click.echo(f"Max iterations ({max_iter}) reached without approval.")


@crew.command()
@click.option("--repo", required=True, help="GitHub repo (owner/name)")
@click.option("--db", default="", help="SQLite DB path (standalone)")
@click.option("--project", default="", help="Project name (reads DB from state)")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
@click.option("--branch", default="main", show_default=True)
@click.option("--no-confirm", is_flag=True, help="Skip approval gate, enqueue task immediately")
@click.option("--merge-history", default="none", show_default=True, help="Recent merge history text")
def triage(repo: str, db: str, project: str, base: str, branch: str,
           no_confirm: bool, merge_history: str):
    """Triage GitHub issues and select the next task."""
    if not db:
        if not project:
            raise click.ClickException("--db or --project is required")
        state = _read_state(base, project)
        if state is None:
            raise click.ClickException(f"project {project!r} not found")
        db = state["db"]

    from agent_crew import triage as triage_module
    from agent_crew.queue import TaskQueue

    queue = TaskQueue(db)

    def _agent_fn(prompt: str) -> str:
        import re
        # Pick the first issue listed in the prompt without re-fetching from GitHub.
        # build_prompt produces lines like: "- #42: Add OAuth login (labels: enhancement)"
        m = re.search(r"- #(\d+): (.+?) \(labels:", prompt)
        if not m:
            return ""
        return f"ISSUE: {m.group(1)}\nDESCRIPTION: {m.group(2).strip()}"

    if no_confirm:
        # Skip gate: fetch → filter → pick → enqueue directly
        raw = triage_module.fetch_issues_from_gh(repo)
        issues = triage_module.parse_issues(raw)
        filtered = triage_module.filter_processed(issues)
        prompt = triage_module.build_prompt(filtered, merge_history)
        if prompt is None:
            click.echo("No issues to triage.")
            return
        response_text = _agent_fn(prompt)
        parsed = triage_module.parse_response(response_text)
        if parsed is None:
            click.echo("No issues to triage.")
            return
        pseudo_result = {"parsed": parsed, "branch": branch}
        task_id = triage_module.enqueue_task(queue, pseudo_result)
        click.echo(f"Task enqueued: {task_id}")
    else:
        result = triage_module.run(queue, repo, _agent_fn, branch=branch, merge_history=merge_history)
        if result is None:
            click.echo("No issues to triage.")
            return
        click.echo(f"Gate created: {result['gate_id']}")
        click.echo(f"Issue #{result['parsed']['issue']}: {result['parsed']['description']}")


@crew.command()
@click.option("--repo", required=True, help="GitHub repo (owner/name)")
@click.option("--db", default="", help="SQLite DB path (standalone)")
@click.option("--project", default="", help="Project name (reads DB from state)")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
@click.option("--branch", default="main", show_default=True)
@click.option("--interval", default="60s", show_default=True, help="Poll interval (e.g. 10s, 1m)")
@click.option("--cycles", default=0, type=int, help="Max cycles (0 = run forever)")
@click.option("--merge-history", default="none", show_default=True)
def poll(repo: str, db: str, project: str, base: str, branch: str,
         interval: str, cycles: int, merge_history: str):
    """Continuously triage issues on a schedule."""
    if not db:
        if not project:
            raise click.ClickException("--db or --project is required")
        state = _read_state(base, project)
        if state is None:
            raise click.ClickException(f"project {project!r} not found")
        db = state["db"]

    import re
    import time as _time

    m = re.fullmatch(r"(\d+)(s|m|h)?", interval.strip())
    if not m:
        raise click.ClickException(f"Invalid interval: {interval!r}. Use e.g. '30s', '2m'.")
    value = int(m.group(1))
    unit = m.group(2) or "s"
    seconds = value * {"s": 1, "m": 60, "h": 3600}[unit]

    from agent_crew import triage as triage_module
    from agent_crew.queue import TaskQueue

    queue = TaskQueue(db)

    def _agent_fn(prompt: str) -> str:
        import re
        m = re.search(r"- #(\d+): (.+?) \(labels:", prompt)
        if not m:
            return ""
        return f"ISSUE: {m.group(1)}\nDESCRIPTION: {m.group(2).strip()}"

    cycle = 0
    while True:
        cycle += 1
        result = triage_module.run(queue, repo, _agent_fn, branch=branch, merge_history=merge_history)
        if result is None:
            click.echo(f"[cycle {cycle}] No issues to triage.")
        else:
            click.echo(f"[cycle {cycle}] Gate created: {result['gate_id']}")

        if cycles and cycle >= cycles:
            break
        _time.sleep(seconds)
