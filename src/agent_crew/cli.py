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


def _auto_detect_project(base: str) -> str | None:
    """Try to auto-detect active project from crew state directory.

    Returns project name if found, else None.
    Strategy: check ~/.agent_crew for state.json files and return most recently
    modified project's name.
    """
    try:
        proj_dir = os.path.expanduser(base)
        if not os.path.isdir(proj_dir):
            return None

        # Find all projects with state.json
        projects_with_state = []
        for entry in os.listdir(proj_dir):
            state_path = os.path.join(proj_dir, entry, "state.json")
            if os.path.isfile(state_path):
                mtime = os.path.getmtime(state_path)
                projects_with_state.append((entry, mtime))

        if not projects_with_state:
            return None

        # Return the most recently modified project
        projects_with_state.sort(key=lambda x: x[1], reverse=True)
        return projects_with_state[0][0]
    except Exception:
        return None


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


def _pane_cwd(pane_id: str) -> str | None:
    """Get the current working directory of a tmux pane."""
    r = subprocess.run(
        ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return r.stdout.strip()
    return None


def _tmux_target_valid(target: str) -> bool:
    """Validate that a tmux target (session:window.pane) actually exists.

    Args:
        target: tmux target in format 'session:window.pane' or 'session:window'

    Returns:
        True if the target exists and is accessible, False otherwise
    """
    r = subprocess.run(
        ["tmux", "display-message", "-t", target, "-p", "#{session_name}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _validate_pane_map(session: str, pane_ids: list[str], worktrees: dict[str, str], agent_list: list[str]) -> dict[str, str]:
    """Validate that pane_ids exist and match expected worktrees.

    Returns a dict of validation results with keys:
    - 'valid': bool - all panes exist and match
    - 'mismatches': list[tuple(agent, pane_id, issue)] - panes with issues
    - 'suggestions': list[str] - remediation suggestions
    """
    mismatches = []

    for agent, pane_id, expected_wt in zip(agent_list, pane_ids, [worktrees.get(a, "") for a in agent_list]):
        if not _pane_alive(pane_id):
            mismatches.append((agent, pane_id, "pane does not exist"))
            continue

        # Check working directory matches expected worktree
        actual_cwd = _pane_cwd(pane_id)
        if actual_cwd and expected_wt:
            # Normalize paths for comparison
            actual_normalized = os.path.normpath(actual_cwd)
            expected_normalized = os.path.normpath(expected_wt)
            if not actual_normalized.startswith(expected_normalized):
                mismatches.append((agent, pane_id, f"cwd mismatch: {actual_cwd} != {expected_wt}"))

    suggestions = []
    if mismatches:
        suggestions.append("Option 1: Run `crew recover` to recreate dead panes")
        suggestions.append("Option 2: Manually fix state.json pane_ids and restart server")
        suggestions.append("Option 3: Run `crew teardown` and `crew setup` to reinitialize")

    return {
        "valid": len(mismatches) == 0,
        "mismatches": mismatches,
        "suggestions": suggestions,
    }


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

        # Kill old panes to prevent duplicates when creating new ones
        for pane_id in existing_pane_ids:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True, text=True,
            )

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

    # Check window width — main-vertical layout needs minimum width for readable panes
    # (e.g., 80-wide window + 3 agents can create 1-char wide panes on the right)
    window_width_result = subprocess.run(
        ["tmux", "display-message", "-t", window_target, "-p", "#{window_width}"],
        capture_output=True, text=True,
    )
    if window_width_result.returncode == 0:
        try:
            window_width = int(window_width_result.stdout.strip())
            min_width_needed = (len(agent_list) + 1) * 60  # Coordinator (60) + agents (60 each min)
            if window_width < min_width_needed:
                click.echo(
                    f"Warning: window width {window_width} may be too narrow for {len(agent_list)} agents.\n"
                    f"         Minimum recommended: {min_width_needed} chars ({len(agent_list)+1} panes × 60)\n"
                    f"         Agent panes may be unreadable (< 20 chars wide).",
                    err=True,
                )
        except (ValueError, AttributeError):
            pass  # Can't parse width, continue anyway

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

    # Verify pane widths are acceptable (avoid silent failures with 1-char wide panes)
    if pane_ids:
        pane_widths = []
        for pane_id in pane_ids:
            width_result = subprocess.run(
                ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_width}"],
                capture_output=True, text=True,
            )
            if width_result.returncode == 0:
                try:
                    width = int(width_result.stdout.strip())
                    pane_widths.append((pane_id, width))
                    if width < 20:
                        click.echo(
                            f"Warning: pane {pane_id} width is {width} chars (too narrow for CLI).\n"
                            f"         Expand your tmux window or use fewer agents.",
                            err=True,
                        )
                except (ValueError, AttributeError):
                    pass

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

    task_groups = None
    try:
        task_groups = {
            display_status: _fetch_tasks_by_status(port, api_status)
            for display_status, api_status in _STATUS_ALIASES
        }
    except Exception:
        click.echo("\nTASKS: (server unreachable)")

    if task_groups:
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

    click.echo("\nAgents:")
    pane_targets = state.get("pane_ids") or [
        f"{session_name}:0.{i}" for i in range(len(agent_list))
    ]

    # Validate pane_ids match actual tmux state
    worktrees = state.get("worktrees", {})
    validation = _validate_pane_map(session_name, pane_targets, worktrees, agent_list)
    if not validation["valid"]:
        click.echo("\n⚠️  PANE VALIDATION ERROR:")
        for agent, pane_id, issue in validation["mismatches"]:
            click.echo(f"  {agent} ({pane_id}): {issue}")
        click.echo("\nSuggestions:")
        for suggestion in validation["suggestions"]:
            click.echo(f"  {suggestion}")

    agent_status = {}
    for agent, target in zip(agent_list, pane_targets):
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            capture_output=True,
            text=True,
        )
        alive = result.returncode == 0
        status_str = "alive" if alive else "dead"

        # Extract last line of pane output for current activity
        current_task = ""
        if alive and result.stdout:
            lines = result.stdout.strip().split("\n")
            # Find a line that looks like it shows task/activity
            for line in lines[-5:]:
                if "task" in line.lower() or "processing" in line.lower() or "review" in line.lower():
                    current_task = f" — {line.strip()[:60]}"
                    break

        click.echo(f"  {agent} ({target}): {status_str}{current_task}")
        agent_status[agent] = alive

    # Performance metrics
    if _queue_for_preview is not None and task_groups:
        click.echo("\nMetrics:")
        all_done = task_groups.get("done", [])
        all_running = task_groups.get("running", [])

        # Count tasks by type in done list
        type_counts = {}
        for t in all_done:
            ttype = t.get("task_type") if isinstance(t, dict) else getattr(t, "task_type", "?")
            type_counts[ttype] = type_counts.get(ttype, 0) + 1

        for ttype, count in sorted(type_counts.items()):
            click.echo(f"  {ttype}: {count} done")

        # Running by role
        if all_running:
            role_running = {}
            for t in all_running:
                ttype = t.get("task_type") if isinstance(t, dict) else getattr(t, "task_type", "?")
                role_running[ttype] = role_running.get(ttype, 0) + 1
            click.echo(f"  In progress: {role_running}")


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
    port_file = os.path.join(proj_dir, "port")
    recovered = []

    # Validate pane_ids match actual tmux state
    agent_list = state.get("agents", [])
    pane_ids = state.get("pane_ids", [])
    if agent_list and pane_ids:
        validation = _validate_pane_map(session_name, pane_ids, worktrees, agent_list)
        if not validation["valid"]:
            click.echo("⚠️  State validation: pane_ids don't match actual tmux state")
            click.echo("     This may happen if the tmux session was recreated or panes were moved")
            click.echo("     Recovery will recreate dead panes and update state.json")

    # Ensure instruction files exist in all worktrees before agents start
    setup_module.write_instruction_files(worktrees, project, port_file)

    # Restart server if not listening
    if not _port_listening(port, timeout=1.0):
        pythonpath = os.pathsep.join(p for p in sys.path if p)
        pane_map_file = os.path.join(proj_dir, "pane_map.json")
        server_env = {
            **os.environ,
            "AGENT_CREW_DB": db_file,
            "AGENT_CREW_PANE_MAP": pane_map_file,
            "PYTHONPATH": pythonpath,
        }
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
        # Session exists — validate window before touching tmux to avoid killing wrong panes
        window = state.get("window", "0")
        window_target = f"{session_name}:{window}"

        # Validate the window exists before running any split-window commands
        if not _tmux_target_valid(window_target):
            raise click.ClickException(
                f"Window {window_target} not found. state.json may be stale.\n"
                f"Options:\n"
                f"  1. Run 'crew teardown' and 'crew setup' to rebuild\n"
                f"  2. Manually fix state.json window field\n"
                f"  3. Run 'tmux list-windows -t {session_name}' to see valid windows"
            )

        # Check each agent pane individually and recreate only the dead ones so alive
        # panes keep their running agent CLIs. Also handle missing panes.
        agent_list = state.get("agents", [])
        existing_pane_ids = state.get("pane_ids", [])
        if agent_list:
            new_pane_ids: list[str] = []
            dead_agents: list[str] = []
            dead_targets: list[str] = []

            # Match existing panes with agents; recreate dead ones or those without worktree
            for i, agent in enumerate(agent_list):
                pane_id = existing_pane_ids[i] if i < len(existing_pane_ids) else None
                wt_path = worktrees.get(agent, "")

                # Check if pane exists and is alive with valid worktree
                if pane_id and _pane_alive(pane_id) and wt_path and os.path.isdir(wt_path):
                    # Keep alive pane that has valid worktree context
                    new_pane_ids.append(pane_id)
                    continue

                # Pane is dead, missing worktree, or invalid — recreate it
                # Safety check: ensure window_target is still valid before split-window
                if not _tmux_target_valid(window_target):
                    raise click.ClickException(
                        f"Window {window_target} became invalid during recovery. "
                        f"This may indicate the session was modified. Aborting."
                    )

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
                    # Fallback to existing if split failed
                    click.echo(f"Warning: failed to create pane for {agent}", err=True)
                    new_pane_ids.append(pane_id or "unknown")

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
@click.option("--create-issue", is_flag=True, help="Create GitHub issue for task")
@click.option("--create-pr", is_flag=True, help="Create GitHub PR after implementation")
@click.option("--repo", default="", help="GitHub repo (owner/repo format)")
@click.option("--implementer", default="", help="Agent for implementation (claude/codex/gemini)")
@click.option("--reviewer", default="", help="Agent for review (claude/codex/gemini)")
def run_cmd(task: str, db: str, project: str, base: str,
            max_iter: int, no_tester: bool, branch: str, timeout: int,
            create_issue: bool, create_pr: bool, repo: str,
            implementer: str, reviewer: str):
    """Run TASK through the code-review loop."""
    if not task.strip():
        raise click.UsageError("task must not be empty")

    if not db:
        if not project:
            # Try to auto-detect project
            detected = _auto_detect_project(base)
            if not detected:
                raise click.ClickException(
                    "Error: --db or --project is required.\n"
                    f"Usage: crew run \"task\" --project <name>\n"
                    f"Or:    crew run \"task\" --db <path>/tasks.db\n"
                    f"List projects: ls {os.path.expanduser(base)}/"
                )
            project = detected
        state = _read_state(base, project)
        if state is None:
            raise click.ClickException(f"project {project!r} not found in {os.path.expanduser(base)}/")
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
    from agent_crew import github
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
        start_time = time.time()
        deadline = start_time + wait_timeout
        fallback_start = start_time + 30  # grace period before pane checks
        pane_idle_count = 0
        last_progress_print = start_time
        while time.time() < deadline:
            result = queue.get_result(task_id)
            if result is not None:
                elapsed = int(time.time() - start_time)
                return result
            # Print progress every 10 seconds during the wait
            now = time.time()
            if now - last_progress_print >= 10:
                elapsed = int(now - start_time)
                click.echo(f"  Waiting... ({elapsed}s elapsed)")
                last_progress_print = now
            # Pane capture fallback: after 30s, check every 10s
            if first_pane_target and now > fallback_start:
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

    def _create_and_report_pr(branch_name: str, repo_url: str, task_desc: str) -> None:
        """Create a GitHub PR and report the result."""
        if not github.check_gh_installed():
            click.echo("Warning: gh CLI not installed, skipping PR creation")
            return
        resolved_repo = repo_url or github.get_repo()
        if not resolved_repo:
            click.echo("Warning: Could not determine repo, skipping PR creation")
            return
        pr_number = github.create_pr(
            title=task_desc.split('\n')[0][:72],
            body=task_desc,
            branch=branch_name,
            base="main",
            repo=resolved_repo
        )
        if pr_number:
            pr_url = github.get_pr_url(resolved_repo, pr_number)
            click.echo(f"Created PR #{pr_number}: {pr_url}")
        else:
            click.echo("Warning: Failed to create GitHub PR")

    # Determine port for gate resolution (available when --project is set)
    _run_port = 0
    if project:
        _pstate = _read_state(base, project)
        if _pstate:
            _run_port = _pstate.get("port", 0)

    # Create GitHub issue if requested
    issue_number = None
    if create_issue:
        if not github.check_gh_installed():
            raise click.ClickException("gh CLI is not installed. Install it to use --create-issue.")
        repo_url = repo or github.get_repo()
        if not repo_url:
            raise click.ClickException("Could not determine repo. Use --repo to specify owner/repo.")
        issue_number = github.create_issue(
            title=task.split('\n')[0][:72],  # First line, max 72 chars
            body=task,
            repo=repo_url
        )
        if issue_number:
            click.echo(f"Created GitHub issue #{issue_number}")
        else:
            raise click.ClickException("Failed to create GitHub issue")

    impl_context = {}
    if implementer:
        impl_context["agent_override"] = implementer

    impl_id = enqueue_implement(queue, task, branch, context=impl_context, port=_run_port)
    click.echo(f"[1/{max_iter}] Implementing... ({impl_id})")
    if _run_port and not _verify_delivery(_run_port, impl_id, timeout=15.0):
        click.echo(f"Warning: task {impl_id!r} still pending after 15s — agent pane may not have received it.")

    for iteration in range(1, max_iter + 1):
        impl_start = time.time()
        _wait(impl_id)
        impl_elapsed = int(time.time() - impl_start)
        click.echo(f"[{iteration}/{max_iter}] ✅ Implementation done ({impl_elapsed}s)")

        review_context = {}
        if reviewer:
            review_context["agent_override"] = reviewer
        review_id = enqueue_review(queue, task, branch, prev_task_id=impl_id, context=review_context, port=_run_port)
        click.echo(f"[{iteration}/{max_iter}] Reviewing... ({review_id})")
        review_start = time.time()
        review_result = _wait(review_id)
        review_elapsed = int(time.time() - review_start)

        # pass no_tester=True here — test enqueue is handled manually below
        outcome = handle_review_result(
            review_result,
            iteration=iteration,
            max_iter=max_iter,
            no_tester=True,
            queue=queue,
        )

        if outcome == "escalate":
            click.echo(f"[{iteration}/{max_iter}] ❌ Escalated after {max_iter} iterations.")
            return

        if outcome == "approved":
            click.echo(f"[{iteration}/{max_iter}] ✅ Review approved ({review_elapsed}s)")
            # Auto-resolve any pending gates before proceeding
            if _run_port:
                _drain_resolvable_gates(_run_port)
            if not no_tester:
                test_id = enqueue_test(queue, task, branch, port=_run_port)
                click.echo(f"[{iteration}/{max_iter}] Testing... ({test_id})")
                test_start = time.time()
                test_result = _wait(test_id)
                test_elapsed = int(time.time() - test_start)
                test_outcome = handle_test_result(test_result)
                if test_outcome == "passed":
                    if _run_port:
                        _drain_resolvable_gates(_run_port)
                    click.echo(f"[{iteration}/{max_iter}] ✅ Tests passed ({test_elapsed}s). Loop complete.")
                    # Create PR if requested
                    if create_pr:
                        _create_and_report_pr(branch, repo, task)
                    return
                else:
                    click.echo(f"[{iteration}/{max_iter}] ❌ Tests {test_outcome} ({test_elapsed}s). Re-implementing.")
                    impl_id = enqueue_implement(queue, task, branch,
                                               context={"retry": True}, port=_run_port)
                    continue
            else:
                if _run_port:
                    _drain_resolvable_gates(_run_port)
                click.echo(f"[{iteration}/{max_iter}] ✅ Loop complete ({review_elapsed}s, no tester).")
                # Create PR if requested
                if create_pr:
                    _create_and_report_pr(branch, repo, task)
            return

        # request_changes: re-implement with feedback
        click.echo(f"[{iteration}/{max_iter}] 🔄 Changes requested ({review_elapsed}s). Re-implementing.")
        feedback = build_feedback(review_result)
        retry_context = {"feedback": feedback}
        if implementer:
            retry_context["agent_override"] = implementer
        impl_id = enqueue_implement(queue, task, branch, context=retry_context, port=_run_port)

    click.echo(f"❌ Max iterations ({max_iter}) reached without approval.")


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
            # Try to auto-detect project
            detected = _auto_detect_project(base)
            if not detected:
                raise click.ClickException(
                    "Error: --db or --project is required.\n"
                    f"Usage: crew discuss \"topic\" --project <name>\n"
                    f"Or:    crew discuss \"topic\" --db <path>/tasks.db\n"
                    f"List projects: ls {os.path.expanduser(base)}/"
                )
            project = detected
        project_state = _read_state(base, project)
        if project_state is None:
            raise click.ClickException(f"project {project!r} not found in {os.path.expanduser(base)}/")
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
    _pane_map = {}
    _session_name = ""
    if project:
        _pstate = _read_state(base, project)
        if _pstate:
            _run_port = _pstate.get("port", 0)
            _pane_map = _pstate.get("pane_map", {})
            _session_name = _pstate.get("session", "")

    wait_timeout = float(timeout)

    def _wait_all(task_ids: list[str], timeout: float = 0.0) -> tuple[dict, list[str], dict]:
        """Poll for all task results. On timeout, return (partial_done, missing_ids, idle_status)
        where idle_status maps agent names to idle/active state.
        The caller decides whether partial results are actionable or if idle agents
        need escalation."""
        effective_timeout = timeout if timeout > 0 else wait_timeout
        deadline = time.time() + effective_timeout
        done: dict = {}
        idle_status: dict[str, bool] = {}  # agent -> is_idle

        while time.time() < deadline:
            for tid in task_ids:
                if tid not in done:
                    r = queue.get_result(tid)
                    if r is not None:
                        done[tid] = r
            if len(done) == len(task_ids):
                return done, [], idle_status
            time.sleep(0.1)

        missing = [tid for tid in task_ids if tid not in done]

        # Check pane state for missing tasks to distinguish idle vs. still working
        if missing and _pane_map and _session_name:
            for agent in agent_list:
                pane_id = _pane_map.get(agent)
                if pane_id:
                    pane_out = _capture_pane(pane_id)
                    idle_status[agent] = bool(pane_out and _pane_looks_idle(pane_out))

        return done, missing, idle_status

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
        results_map, missing, idle_status = _wait_all(task_ids)

        if missing:
            partial_hit = True
            missing_by_round[round_num] = missing
            # Report idle agents for debugging
            idle_agents = [a for a in agent_list if idle_status.get(a, False)]
            active_agents = [a for a in agent_list if not idle_status.get(a, False) and a in missing]
            if idle_agents:
                click.echo(f"  Idle agents (no pane activity): {', '.join(idle_agents)}", err=True)
            if active_agents:
                click.echo(f"  Still working: {', '.join(active_agents)}", err=True)

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
