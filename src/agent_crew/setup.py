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


def _get_agent_cmd(agent: str, worktree_path: str | None = None) -> str:
    cmd = _AGENT_CMDS.get(agent, _DEFAULT_CMD)
    if "--continue" in cmd:
        has_history = worktree_path is not None and os.path.isdir(
            os.path.join(worktree_path, ".claude", "projects")
        )
        if not has_history:
            cmd = cmd.replace(" --continue", "")
    return cmd


def start_agents_in_panes(
    session_name: str,
    agents: list[str],
    port: int,
    pane_targets: list[str] | None = None,
    worktrees: dict[str, str] | None = None,
) -> None:
    """Start agent CLIs in tmux panes and send initial polling prompt.

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
        wt_path = worktrees.get(agent) if worktrees else None
        cmd = _get_agent_cmd(agent, wt_path)
        _send_literal_text(target, cmd)
        _send_enter(target)
        if agent == "codex":
            time.sleep(1)
            _send_literal_text(target, "1")
            _send_enter(target)
    # Wait for agent CLIs to initialize
    time.sleep(3)
    for agent, target in zip(agents, pane_targets):
        role = _AGENT_TO_ROLE.get(agent, "implementer")
        polling_prompt = (
            f"You are agent '{agent}' (role: {role}). "
            f"Run this background polling loop using bash tool: "
            f"while true; do RESP=$(curl -sf 'http://127.0.0.1:{port}/tasks/next?role={role}'); "
            f"if [ -n \"$RESP\" ]; then echo \"NEW_TASK: $RESP\"; fi; sleep 30; done & "
            f"When you see NEW_TASK output, process it and POST the result to "
            f"http://127.0.0.1:{port}/tasks/{{id}}/result . Start the loop now."
        )
        _send_literal_text(target, polling_prompt)
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
