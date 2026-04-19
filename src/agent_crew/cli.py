import json
import os
import shutil
import signal
import socket
import subprocess
import sys

import click

from agent_crew import setup as setup_module

_DEFAULT_BASE = os.path.expanduser("~/.agent_crew")
_DEFAULT_AGENTS = "claude,codex,gemini"


def _proj_dir(base: str, project: str) -> str:
    return os.path.join(base, project)


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

    if _read_state(base, project) is not None:
        raise click.ClickException(f"project {project!r} is already set up. Run teardown first.")

    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    proj_dir = _proj_dir(base, project)
    os.makedirs(proj_dir, exist_ok=True)

    # Worktrees
    worktrees = setup_module.create_worktrees(project, base, agent_list)

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

    # Start server
    db_file = os.path.join(proj_dir, "tasks.db")
    server_env = {**os.environ, "AGENT_CREW_DB": db_file}
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_crew.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # tmux session + panes
    session_name = f"crew_{project}"
    first_wt = next(iter(worktrees.values()))
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", first_wt],
                   capture_output=True)
    for i, (agent, wt_path) in enumerate(worktrees.items()):
        if i > 0:
            subprocess.run(["tmux", "new-window", "-t", session_name, "-c", wt_path],
                           capture_output=True)

    _write_state(base, project, {
        "project": project,
        "port": port,
        "port_file": port_file,
        "session": session_name,
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
def status(project: str, base: str):
    """Show status of PROJECT."""
    state = _read_state(base, project)
    if state is None:
        raise click.ClickException(f"project {project!r} not found. Run setup first.")

    session_name = state["session"]
    port = state["port"]
    agent_list = state["agents"]

    click.echo(f"Project: {project}")
    click.echo(f"Port: {port}")

    task_count = 0
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/tasks", timeout=2) as resp:
            task_count = len(json.loads(resp.read()))
    except Exception:
        pass
    click.echo(f"Tasks: {task_count}")

    for i, agent in enumerate(agent_list):
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{session_name}:{i}", "-p"],
            capture_output=True,
        )
        alive = result.returncode == 0
        click.echo(f"  {agent}: {'alive' if alive else 'dead'}")


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

    # Kill tmux session
    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)

    # Kill server
    if server_pid:
        try:
            os.kill(server_pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    # Remove worktrees
    for agent in agent_list:
        wt_path = worktrees.get(agent, "")
        if wt_path:
            subprocess.run(["git", "worktree", "remove", "--force", wt_path],
                           capture_output=True)

    # Remove state dir
    shutil.rmtree(proj_dir, ignore_errors=True)

    click.echo(f"Teardown complete: {project}")


@crew.command("run")
@click.argument("task")
def run_cmd(task: str):
    """Run TASK. TASK must not be empty."""
    if not task.strip():
        raise click.UsageError("task must not be empty")
    click.echo(f"Running task: {task}")


@crew.command()
@click.argument("topic")
def discuss(topic: str):
    """Start a panel discussion on TOPIC. TOPIC must not be empty."""
    if not topic.strip():
        raise click.UsageError("topic must not be empty")
    click.echo(f"Starting discussion on: {topic}")
