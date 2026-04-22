import fcntl
import json
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
    pane_targets: list[str] | None = None,
    worktrees: dict[str, str] | None = None,
) -> None:
    """Start agent CLIs in tmux panes.

    Push model: server delivers tasks to the pane via tmux send-keys when new
    tasks arrive. Agents do NOT poll — they wait for pane messages, parse the
    task, do the work, and POST results back.

    No kickoff prompt is sent. Each worktree already contains the role-specific
    instruction file (CLAUDE.md / AGENTS.md / GEMINI.md) which the agent CLI
    auto-loads, and that file explicitly documents the ``=== AGENT_CREW TASK ===``
    protocol. Sending an extra kickoff prompt wastes per-agent API quota (and
    for rate-limited backends like codex, it cascades: the kickoff gets throttled,
    leaving the subsequent task push in a dead composer state).

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
            # codex prints a trust prompt after launch; "1" accepts the current
            # directory as trusted so the CLI reaches the ready state.
            time.sleep(1)
            _send_literal_text(target, "1")
            _send_enter(target)
    # Give agent CLIs time to finish their own boot banners before the first
    # task push might arrive.
    time.sleep(3)


def pretrust_claude_worktree(worktrees: dict[str, str]) -> None:
    """Pre-accept Claude's workspace-trust dialog for the claude worktree.

    `--dangerously-skip-permissions` bypasses tool permissions but not the
    workspace-trust dialog, so setup would otherwise stall on a "Trust this
    folder?" prompt. Claude stores trust per-project in ~/.claude.json under
    projects[<abs_path>].hasTrustDialogAccepted — we pre-seed it here so the
    dialog is skipped on first launch. Other Claude sessions may write this
    file concurrently, so we hold an exclusive flock across read-modify-write.
    """
    wt_path = worktrees.get("claude")
    if not wt_path:
        return
    config = os.path.expanduser("~/.claude.json")
    if not os.path.exists(config):
        return
    wt_abs = os.path.abspath(wt_path)
    with open(config, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = json.load(f)
            proj = data.setdefault("projects", {}).setdefault(wt_abs, {})
            if proj.get("hasTrustDialogAccepted") is True:
                return
            proj["hasTrustDialogAccepted"] = True
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


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
