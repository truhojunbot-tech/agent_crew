import os
import socket
import subprocess
import time

from agent_crew import instructions, session

_AGENT_CMDS = {
    "claude": "claude --dangerously-skip-permissions --continue",
    "codex": "codex --resume",
    "gemini": "gemini --resume",
}
_DEFAULT_CMD = "claude --dangerously-skip-permissions --continue"


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
        subprocess.run(["git", "worktree", "add", "-B", f"agent/{agent}", wt_path], capture_output=True)
        worktrees[agent] = wt_path
    return worktrees


def write_instruction_files(worktrees: dict, project: str, port_file: str) -> None:
    for agent, wt_path in worktrees.items():
        instructions.write(agent, wt_path, project, port_file)


def write_sessions_json(path: str, agents: list[dict]) -> None:
    enriched = []
    for agent in agents:
        cmd = _AGENT_CMDS.get(agent.get("name", ""), _DEFAULT_CMD)
        enriched.append({**agent, "cmd": cmd, "started_at": time.time(), "failures": 0})
    session.save_sessions(path, enriched)


def find_free_port(start: int = 8100) -> int:
    port = start
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("localhost", port)) != 0:
                return port
        port += 1


def write_port_file(path: str, port: int) -> None:
    with open(path, "w") as f:
        f.write(str(port))
