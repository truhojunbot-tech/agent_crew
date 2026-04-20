import os
import socket
import subprocess
import time

from agent_crew import instructions, session

_AGENT_CMDS = {
    "claude": "claude --dangerously-skip-permissions --continue",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
    "gemini": "gemini --yolo",
}
_DEFAULT_CMD = "claude --dangerously-skip-permissions --continue"


def start_agents_in_panes(
    session_name: str,
    agents: list[str],
    port: int,
    pane_targets: list[str] | None = None,
) -> None:
    """Start agent CLIs in tmux panes.

    Push model: server delivers tasks to the pane via tmux send-keys when new
    tasks arrive. Agents do NOT poll — they wait for pane messages, parse the
    task, do the work, and POST results back. The kickoff prompt just tells
    the agent how to recognize the incoming AGENT_CREW TASK format.

    pane_targets (optional): explicit tmux targets per agent (e.g. pane_ids
    like ``%42``). When omitted, falls back to ``<session>:0.<i>`` which
    assumes a fresh session where pane 0 is the first agent.
    """
    def _send_literal_text(target: str, text: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-l", "-t", target, text],
            capture_output=True,
        )

    def _send_enter(target: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True)

    if pane_targets is None:
        pane_targets = [f"{session_name}:0.{i}" for i in range(len(agents))]
    if len(pane_targets) != len(agents):
        raise ValueError(
            f"pane_targets length {len(pane_targets)} != agents length {len(agents)}"
        )

    for agent, target in zip(agents, pane_targets):
        cmd = _AGENT_CMDS.get(agent, _DEFAULT_CMD)
        _send_literal_text(target, cmd)
        _send_enter(target)
    # Wait for agent CLIs to initialize
    time.sleep(3)
    for agent, target in zip(agents, pane_targets):
        role = _AGENT_TO_ROLE.get(agent, "implementer")
        kickoff_prompt = (
            f"You are agent '{agent}' (role: {role}). "
            f"The agent_crew server at http://127.0.0.1:{port} will push tasks to this pane. "
            f"When you see a block starting with '=== AGENT_CREW TASK ===', parse the fields "
            f"(task_id, task_type, branch, priority, context, description), do the work, "
            f"then POST result to /tasks/<task_id>/result. Do not poll — just wait for the next task. "
            f"Read your agent instruction file (CLAUDE.md / AGENTS.md / GEMINI.md) in this directory for full protocol."
        )
        _send_literal_text(target, kickoff_prompt)
        time.sleep(1)
        _send_enter(target)


def validate_git_repo(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-dir"],
        capture_output=True,
    )
    return result.returncode == 0


def create_worktrees(project: str, base: str, agents: list[str]) -> dict[str, str]:
    worktrees = {}
    for agent in agents:
        wt_path = os.path.join(base, project, agent)
        branch = f"agent/{project}/{agent}"
        result = subprocess.run(
            ["git", "worktree", "add", "-B", branch, wt_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and not os.path.isdir(wt_path):
            raise RuntimeError(
                f"Failed to create worktree for {agent}: {result.stderr.strip()}"
            )
        worktrees[agent] = wt_path
    return worktrees


_AGENT_TO_ROLE = {"claude": "implementer", "codex": "reviewer", "gemini": "tester"}


def write_instruction_files(worktrees: dict, project: str, port_file: str) -> None:
    for agent, wt_path in worktrees.items():
        role = _AGENT_TO_ROLE.get(agent, "implementer")
        instructions.write(role, wt_path, project, port_file)


def write_sessions_json(path: str, agents: list[dict]) -> None:
    enriched = []
    for agent in agents:
        cmd = _AGENT_CMDS.get(agent.get("name", ""), _DEFAULT_CMD)
        enriched.append({**agent, "cmd": cmd, "started_at": time.time(), "failures": 0})
    session.save_sessions(path, enriched)


def find_free_port(start: int = 8100) -> int:
    """Find a free port by actually binding to it (SO_REUSEADDR off) to avoid TOCTOU."""
    port = start
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1


def write_port_file(path: str, port: int) -> None:
    with open(path, "w") as f:
        f.write(str(port))
