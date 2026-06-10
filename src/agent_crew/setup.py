import fcntl
import json
import logging
import os
import re
import socket
import subprocess
import time

_logger = logging.getLogger(__name__)

from agent_crew import instructions, session

_AGENT_CMDS = {
    "claude": "claude --dangerously-skip-permissions --continue --model claude-opus-4-7",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
    # ``--approval-mode yolo`` is the policy-engine flag that fully
    # bypasses every tool prompt (including shell/git which the legacy
    # ``--yolo`` short flag was observed to miss when an MCP server is
    # registered alongside built-in tools — Issue #110 phase 5b).
    # ``--model gemini-2.5-flash``: tester 단계에서 gemini-2.5-pro의 long-context
    # 응답이 streaming 없이 모델 처리에 머무는 구간이 길어 watchdog 300s
    # idle 오탐을 자주 일으켰다 (#115). flash는 응답이 가벼워 idle 빈도가
    # 줄고 테스트 실행/검토 작업 품질엔 영향이 작다.
    "gemini": "gemini --approval-mode yolo --model gemini-2.5-flash",
}
_DEFAULT_CMD = "claude --dangerously-skip-permissions --continue --model claude-opus-4-7"

# Substrings expected in pane output after a successful CLI boot.
# Used by start_agents_in_panes to warn when the CLI doesn't appear to have started.
_CLI_READY_MARKERS: dict[str, tuple[str, ...]] = {
    "claude": ("bypass permissions", "skip permissions"),
    "codex": ("gpt-", "codex>", "enter your task", "explain this codebase"),
    "gemini": ("gemini", "yolo"),
}


def _get_agent_cmd(agent: str, worktree_path: str | None = None) -> str:
    """Return the shell command that launches the agent CLI in its worktree.

    For codex, prefixes ``CODEX_HOME=<wt>/.codex_local`` so the agent reads
    a worktree-local config (Issue #110 phase 5 — per-agent MCP). codex
    has no project-local config-file discovery, so the env var is the
    only way to scope its MCP registration to a single project.
    """
    cmd = _AGENT_CMDS.get(agent, _DEFAULT_CMD)
    if "--continue" in cmd:
        has_history = worktree_path is not None and os.path.isdir(
            os.path.join(worktree_path, ".claude", "projects")
        )
        if not has_history:
            cmd = cmd.replace(" --continue", "")
    if agent == "codex" and worktree_path:
        codex_home = os.path.join(worktree_path, ".codex_local")
        cmd = f"CODEX_HOME={codex_home} {cmd}"
    if agent == "claude" and worktree_path:
        # Scope the Telegram plugin state dir to the worktree so the agent's
        # bun (if --channels loads it) never reads ~/.claude/channels/telegram/
        # and never steals the real trader bot token (#168).
        telegram_state = os.path.join(worktree_path, ".telegram")
        cmd = f"TELEGRAM_STATE_DIR={telegram_state} {cmd}"
    return cmd


def start_agents_in_panes(
    session_name: str,
    agents: list[str],
    pane_targets: list[str] | None = None,
    worktrees: dict[str, str] | None = None,
) -> None:
    """Start agent CLIs in tmux panes.

    Hybrid model: the server delivers tasks via tmux send-keys (push), and
    agents also poll ``get_next_task`` via MCP every 30 seconds as a fallback
    so no task is missed if a push is delayed or the pane was briefly busy.

    After CLI boot, a kickoff prompt is sent to each agent instructing them to
    start calling ``get_next_task(agent=...)`` every 30 seconds. This prompt
    starts the polling loop without waiting for a push to arrive.

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
        # #142: ensure the pane is at a shell prompt before launching.
        # If an old agent process (e.g. gemini without --approval-mode yolo) is
        # still running, send-keys -l feeds the cmd as chat input instead of
        # executing it in the shell. Ctrl+C interrupts any interactive process;
        # 'q' + Enter covers CLIs that catch Ctrl+C and present a quit prompt.
        # Safe on fresh panes: Ctrl+C on an idle shell is a no-op.
        subprocess.run(["tmux", "send-keys", "-t", target, "C-c"], capture_output=True)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", target, "q", "Enter"], capture_output=True)
        time.sleep(0.3)
        _send_literal_text(target, cmd)
        _send_enter(target)
        if agent == "codex":
            # codex shows a two-step trust dialog after launch:
            # 1) "1. Yes / 2. No" — cursor already on 1, Enter accepts
            # 2) "Press enter to continue" — needs a second Enter
            # The dialog takes ~3s to appear so we wait before responding.
            time.sleep(3)
            _send_enter(target)   # accept option 1 (already highlighted)
            time.sleep(0.5)
            _send_enter(target)   # dismiss "Press enter to continue"
    # Wait for each agent CLI to become ready, then send kickoff.
    # Per-agent polling replaces the hardcoded sleep(5): each agent gets up to
    # _KICKOFF_MAX_WAIT seconds to show a CLI-ready marker so the kickoff prompt
    # always lands at an active input cursor rather than mid-startup noise.
    _KICKOFF_POLL_INTERVAL = 2
    _KICKOFF_MAX_WAIT = 30

    for agent, target in zip(agents, pane_targets):
        markers = _CLI_READY_MARKERS.get(agent)
        if markers:
            deadline = time.time() + _KICKOFF_MAX_WAIT
            ready = False
            while time.time() < deadline:
                r = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", target],
                    capture_output=True, text=True,
                )
                if r.returncode == 0 and any(m.lower() in r.stdout.lower() for m in markers):
                    ready = True
                    break
                time.sleep(_KICKOFF_POLL_INTERVAL)
            if not ready:
                _logger.warning(
                    "start_agents_in_panes: pane %s (%s) not ready after %ss — "
                    "kickoff sent anyway but agent may not receive it correctly.",
                    target, agent, _KICKOFF_MAX_WAIT,
                )
        else:
            time.sleep(5)
        kickoff = (
            f"Start your task loop now: call get_next_task(agent=\"{agent}\") "
            f"via the agent_crew MCP server every 30 seconds. "
            f"If MCP is unavailable, poll GET /tasks/next every 30 seconds. "
            f"If either returns None, wait 30 seconds and try again. "
            f"When a task arrives, branch on task_type, do the work, "
            f"then call submit_result. Loop indefinitely."
        )
        # Use load-buffer + paste-buffer for reliable delivery of long text.
        # tmux send-keys -l can silently drop characters when the pane is
        # processing startup output at the same time.
        r_load = subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=kickoff,
            text=True,
            capture_output=True,
        )
        if r_load.returncode != 0:
            _logger.warning(
                "start_agents_in_panes: load-buffer failed for %s (%s): %s",
                target, agent, r_load.stderr,
            )
            _send_literal_text(target, kickoff)
        else:
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-d", "-t", target],
                capture_output=True,
            )
        time.sleep(0.5)
        _send_enter(target)
        time.sleep(0.5)


def start_log_viewers_in_panes(
    agents: list[str],
    pane_targets: list[str],
    log_dir: str,
    role_based: bool = False,
) -> None:
    """In dispatcher mode, start `tail -f dispatch_{role}.log` in each pane.

    The dispatcher writes agent output to per-role log files; panes tail those
    files so the user can monitor progress without an interactive CLI running.

    When ``role_based`` is True, positions in the ``agents`` list map directly
    to roles (implementer/reviewer/tester) regardless of duplicate names —
    pane 0 watches dispatch_implementer.log even if agent[0] == agent[1].
    """
    for i, (agent, target) in enumerate(zip(agents, pane_targets)):
        if role_based and i < len(ROLES):
            role = ROLES[i]
        else:
            role = _AGENT_TO_ROLE.get(agent, agent)
        log_path = os.path.join(log_dir, f"dispatch_{role}.log")
        # Ensure the file exists before tail starts.
        subprocess.run(["touch", log_path], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-c"], capture_output=True)
        time.sleep(0.2)
        cmd = f"crew-log-viewer {log_path}"
        subprocess.run(["tmux", "send-keys", "-l", "-t", target, cmd], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True)
        _logger.info("start_log_viewers_in_panes: %s watching %s", target, log_path)


def pretrust_claude_worktree(
    worktrees: dict[str, str],
    roles: list[dict] | None = None,
) -> None:
    """Pre-accept Claude's workspace-trust dialog for every claude worktree.

    `--dangerously-skip-permissions` bypasses tool permissions but not the
    workspace-trust dialog, so setup would otherwise stall on a "Trust this
    folder?" prompt. Claude stores trust per-project in ~/.claude.json under
    projects[<abs_path>].hasTrustDialogAccepted — we pre-seed it here so the
    dialog is skipped on first launch. Other Claude sessions may write this
    file concurrently, so we hold an exclusive flock across read-modify-write.

    Walks all (role, agent, worktree) entries so role_based mode with multiple
    claude worktrees (e.g. claude implementer + claude reviewer) is covered.
    """
    claude_wts: list[str] = []
    for role, agent, wt_path in _iter_role_entries(worktrees, roles):
        if agent == "claude" and wt_path:
            claude_wts.append(wt_path)
    if not claude_wts:
        return
    config = os.path.expanduser("~/.claude.json")
    if not os.path.exists(config):
        return
    with open(config, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = json.load(f)
            projects = data.setdefault("projects", {})
            changed = False
            for wt_path in claude_wts:
                wt_abs = os.path.abspath(wt_path)
                proj = projects.setdefault(wt_abs, {})
                if proj.get("hasTrustDialogAccepted") is not True:
                    proj["hasTrustDialogAccepted"] = True
                    changed = True
            if changed:
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


def resolve_project_path(project: str) -> str:
    """Auto-detect project path from project name.

    Searches common locations: ~/alfred/projects/<project>, ~/work/<project>, etc.
    Falls back to current directory if it's a git repo. Raises RuntimeError if not found.
    """
    # Priority order for project discovery
    candidates = [
        os.path.expanduser(f"~/alfred/projects/{project}"),
        os.path.expanduser(f"~/projects/{project}"),
        os.path.expanduser(f"~/work/{project}"),
        os.path.expanduser(f"~/{project}"),
    ]

    for path in candidates:
        if os.path.isdir(path) and validate_git_repo(path):
            return path

    # Fallback: check current directory
    cwd = os.getcwd()
    if validate_git_repo(cwd):
        return cwd

    raise RuntimeError(
        f"Could not find project '{project}' in any standard location. "
        f"Searched: {', '.join(candidates)}. "
        f"Use 'crew setup --project-path <path>' to specify explicitly."
    )


ROLES = ("implementer", "reviewer", "tester")


def create_worktrees(
    project: str,
    base: str,
    agents: list[str],
    project_path: str | None = None,
    *,
    role_based: bool = False,
) -> dict[str, str]:
    """Create git worktrees for agents.

    Args:
        project: project name (e.g., 'agent_crew')
        base: base directory for state (e.g., ~/.agent_crew)
        agents: list of agent names. Order maps to roles when role_based=True
            (position 0=implementer, 1=reviewer, 2=tester).
        project_path: explicit path to project git repo. If None, auto-detect.
        role_based: when True, the same agent name may appear multiple times
            (e.g. ``["claude","claude","gemini"]``) and worktrees are keyed by
            role (implementer/reviewer/tester) rather than agent name. Each
            role gets its own filesystem directory so duplicate-agent
            entries don't collide on shared git state.

    Worktrees are stored at:
      base/worktrees/<project>/<agent>/  (default)
      base/worktrees/<project>/<role>/   (role_based=True)
    For backward compatibility, existing worktrees at base/<project>/<agent>/
    are reused when role_based=False.
    State (state.json, tasks.db) at: base/<project>/
    """
    if project_path is None:
        project_path = resolve_project_path(project)

    if not validate_git_repo(project_path):
        raise RuntimeError(f"Project path {project_path!r} is not a git repository")

    worktrees: dict[str, str] = {}

    if role_based:
        # Map position → role; each role gets its own dir, branch.
        for i, agent in enumerate(agents):
            if i >= len(ROLES):
                _logger.warning(
                    "create_worktrees: role_based mode ignores agent[%d]=%r (max %d roles)",
                    i, agent, len(ROLES),
                )
                break
            role = ROLES[i]
            wt_path = os.path.join(base, "worktrees", project, role)
            branch = f"agent/{project}/{role}"
            result = subprocess.run(
                ["git", "-C", project_path, "worktree", "add", "-B", branch, wt_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0 and not os.path.isdir(wt_path):
                raise RuntimeError(
                    f"Failed to create worktree for role={role} agent={agent}: "
                    f"{result.stderr.strip()}"
                )
            worktrees[role] = wt_path
    else:
        for agent in agents:
            # Check for existing worktree at old location (backward compatibility)
            old_wt_path = os.path.join(base, project, agent)
            if os.path.isdir(old_wt_path):
                # Reuse existing worktree at old location
                worktrees[agent] = old_wt_path
                continue

            # Create new worktree at architecture-compliant location
            wt_path = os.path.join(base, "worktrees", project, agent)
            branch = f"agent/{project}/{agent}"
            # Run git worktree add FROM the project repo, not from cwd
            result = subprocess.run(
                ["git", "-C", project_path, "worktree", "add", "-B", branch, wt_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0 and not os.path.isdir(wt_path):
                raise RuntimeError(
                    f"Failed to create worktree for {agent}: {result.stderr.strip()}"
                )
            worktrees[agent] = wt_path

    # Create per-worktree .telegram/ state dirs for claude agents (#168).
    # Prevents the Telegram plugin bun (if loaded via --channels or session
    # history) from falling back to ~/.claude/channels/telegram/ and
    # accidentally reading the real trader bot token.
    # In role_based mode, identify claude worktrees by checking which positions
    # the agents list assigned claude to (positions map 1:1 to ROLES).
    claude_worktrees: list[str] = []
    if role_based:
        for i, agent in enumerate(agents[: len(ROLES)]):
            if agent == "claude":
                claude_worktrees.append(worktrees[ROLES[i]])
    else:
        if "claude" in worktrees:
            claude_worktrees.append(worktrees["claude"])

    for wt_path in claude_worktrees:
        try:
            telegram_dir = os.path.join(wt_path, ".telegram")
            os.makedirs(telegram_dir, exist_ok=True)
            env_path = os.path.join(telegram_dir, ".env")
            if not os.path.exists(env_path):
                with open(env_path, "w") as f:
                    f.write("TELEGRAM_BOT_TOKEN=DISABLED_AGENT_CREW_WORKER\n")
        except OSError:
            _logger.warning(
                "create_worktrees: could not create .telegram dir at %s",
                wt_path,
            )

    # Convert HTTPS GitHub origin → SSH so dispatched agents (gemini in
    # particular) don't hit "could not read Username" when they run git
    # operations in their non-interactive shell wrappers (#189). Only acts
    # when (a) origin is an HTTPS github.com URL, and (b) SSH auth to
    # github.com actually works on this host — otherwise it's a no-op so
    # users without SSH keys aren't broken. Honors AGENT_CREW_PREFER_HTTPS=1
    # as an opt-out.
    if os.environ.get("AGENT_CREW_PREFER_HTTPS", "").lower() not in ("1", "true", "yes"):
        _convert_origin_to_ssh_if_safe(project_path)

    return worktrees


def _convert_origin_to_ssh_if_safe(project_path: str) -> None:
    """Switch ``project_path``'s ``origin`` from HTTPS GitHub URL to SSH.

    Worktrees share the parent repo's remote config, so a single change to
    the project repo's origin propagates to every agent worktree. Safe to
    skip when origin isn't HTTPS github.com, or when SSH auth isn't
    available — in either case the original URL is preserved.
    """
    try:
        cur = subprocess.run(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if cur.returncode != 0:
            return
        url = cur.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return

    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return  # Not an HTTPS github URL — leave alone.
    owner, repo = m.group(1), m.group(2)
    ssh_url = f"git@github.com:{owner}/{repo}.git"

    # Probe SSH auth before switching — silently skip if SSH won't work.
    probe = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=accept-new", "git@github.com"],
        capture_output=True, text=True, timeout=15,
    )
    # `ssh -T git@github.com` exits 1 on success too — auth is judged by
    # the welcome line in stderr.
    if "successfully authenticated" not in (probe.stderr + probe.stdout).lower():
        _logger.info(
            "create_worktrees: SSH probe to github.com failed; "
            "leaving origin as HTTPS (%s)", url,
        )
        return

    set_result = subprocess.run(
        ["git", "-C", project_path, "remote", "set-url", "origin", ssh_url],
        capture_output=True, text=True, timeout=5,
    )
    if set_result.returncode == 0:
        _logger.info(
            "create_worktrees: switched origin %s → %s (#189)", url, ssh_url,
        )
    else:
        _logger.warning(
            "create_worktrees: failed to set ssh origin: %s",
            set_result.stderr.strip(),
        )


_AGENT_TO_ROLE = {"claude": "implementer", "codex": "reviewer", "gemini": "tester"}


def _iter_role_entries(
    worktrees: dict,
    roles: list[dict] | None = None,
) -> list[tuple[str, str, str]]:
    """Normalize (role, agent, worktree_path) entries.

    Prefers an explicit ``roles`` list when provided (new role_based schema).
    Falls back to deriving roles from worktrees dict keys when the keys look
    like agent names (legacy schema).
    """
    if roles:
        return [
            (r["role"], r["agent"], r["worktree"])
            for r in roles
            if r.get("role") and r.get("agent") and r.get("worktree")
        ]
    out: list[tuple[str, str, str]] = []
    for key, path in worktrees.items():
        # Legacy: key is agent name → look up role via default map.
        role = _AGENT_TO_ROLE.get(key, "implementer")
        out.append((role, key, path))
    return out


def write_instruction_files(
    worktrees: dict,
    project: str,
    port_file: str,
    roles: list[dict] | None = None,
) -> None:
    dispatcher_mode = os.getenv("AGENT_CREW_DISPATCHER", "1").lower() not in ("0", "false", "no")
    delivery = "dispatcher" if dispatcher_mode else None
    for role, agent, wt_path in _iter_role_entries(worktrees, roles):
        instructions.write(role, wt_path, project, port_file, agent=agent, delivery=delivery)


def _mcp_python_invocation() -> tuple[str, list[str], str]:
    """Resolve (interpreter, args, PYTHONPATH) for launching the agent_crew
    MCP server as a subprocess from any agent CLI."""
    import sys

    interpreter = sys.executable
    args = ["-m", "agent_crew.mcp_server"]
    pythonpath = os.pathsep.join(p for p in sys.path if p)
    return interpreter, args, pythonpath


def _write_postcompact_hook_claude(
    worktree_path: str,
    *,
    agent: str,
    role: str,
) -> str:
    """Drop a per-worktree PostCompact hook into ``.claude/settings.local.json``
    so Claude Code reinjects the compact task-loop prompt after ``/compact``.

    Without this, the long task-loop system prompt — which tells the agent
    to keep calling ``get_next_task`` — gets dropped at compaction and the
    pull loop stalls until the operator manually re-prompts. The hook
    runs ``build_task_loop_prompt_compact(agent, role)`` and emits the
    Claude Code ``additionalContext`` payload that re-establishes the loop
    in one turn (Issue #122).

    Returns the absolute path to the settings file. Idempotent — overwrites
    any prior version with fresh interpreter / PYTHONPATH values so the
    hook keeps working across env changes.
    """
    interpreter, _, pythonpath = _mcp_python_invocation()

    # Single-line python invocation; we go through `-c` so we don't have
    # to ship a separate hook script and risk it falling out of sync.
    # Prompt contains no shell-special chars but we still pipe via JSON to
    # be safe.
    snippet = (
        "import json; "
        "from agent_crew.prompts.task_loop import build_task_loop_prompt_compact; "
        "print(json.dumps({"
        "'hookSpecificOutput': {"
        "'hookEventName': 'PostCompact', "
        f"'additionalContext': build_task_loop_prompt_compact({agent!r}, {role!r})"
        "}}))"
    )
    # Prepend the env so the subprocess Claude Code spawns picks up our
    # PYTHONPATH (Claude Code does not inherit per-worktree mcp env into
    # hook commands).
    command = (
        f'PYTHONPATH="{pythonpath}" "{interpreter}" -c "{snippet}"'
    )

    settings = {
        # Disable Telegram/Discord plugins for worktree subagents: these headless
        # claude -p processes must not start bun servers that steal the coordinator
        # session's bot connection. The worktree .telegram dir has a disabled token
        # as a second layer, but disabling the plugin here is the primary guard.
        "enabledPlugins": {
            "telegram@claude-plugins-official": False,
            "discord@claude-plugins-official": False,
        },
        "hooks": {
            "PostCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    }
    settings_dir = os.path.join(worktree_path, ".claude")
    os.makedirs(settings_dir, exist_ok=True)
    path = os.path.join(settings_dir, "settings.local.json")
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    return os.path.abspath(path)


def _write_mcp_config_claude(worktree_path: str, db_path: str) -> str:
    """Claude Code reads ``.mcp.json`` at the worktree root.

    Also drops a PostCompact hook into ``.claude/settings.local.json`` so
    the task-loop prompt survives compaction (#122). The hook is keyed to
    claude+implementer because that is the canonical claude worktree
    role; if the operator routes a different task_type to this pane via
    `agent_override`, the precedence block in the loop prompt itself
    handles role disambiguation.
    """
    interpreter, args, pythonpath = _mcp_python_invocation()
    config = {
        "mcpServers": {
            "agent_crew": {
                "command": interpreter,
                "args": args,
                "env": {
                    "AGENT_CREW_DB": db_path,
                    "PYTHONPATH": pythonpath,
                },
            }
        }
    }
    path = os.path.join(worktree_path, ".mcp.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    _write_postcompact_hook_claude(
        worktree_path, agent="claude", role="implementer"
    )
    return os.path.abspath(path)


def _bootstrap_codex_auth(codex_home: str) -> None:
    """Copy ``~/.codex/auth.json`` into a worktree-local ``$CODEX_HOME``.

    With ``CODEX_HOME=<wt>/.codex_local`` codex no longer falls back to
    ``~/.codex`` for authentication — it would prompt for a fresh OAuth
    flow on every worktree. Reuse the operator's existing auth by
    copying the credentials file in once at setup. We do NOT symlink
    because codex rewrites this file on token refresh and the symlink
    could leak token expiry between unrelated sessions.

    Idempotent: skipped silently when the global file is missing or
    already mirrored. Permission mode 0600 is preserved.
    """
    src = os.path.expanduser("~/.codex/auth.json")
    if not os.path.isfile(src):
        return
    dst = os.path.join(codex_home, "auth.json")
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            fdst.write(fsrc.read())
        os.chmod(dst, 0o600)
    except OSError:
        # Don't crash setup over a missing auth file — codex will just
        # prompt the user to log in once when it next starts up.
        return


def _write_mcp_config_codex(worktree_path: str, db_path: str) -> str:
    """Codex looks at ``$CODEX_HOME/config.toml``. We point CODEX_HOME at
    ``<worktree>/.codex_local`` from the agent's launch command (see
    :func:`_get_agent_cmd`) and write the TOML here."""
    interpreter, args, pythonpath = _mcp_python_invocation()
    args_repr = "[" + ", ".join(f'"{a}"' for a in args) + "]"
    toml_text = (
        '[mcp_servers.agent_crew]\n'
        f'command = "{interpreter}"\n'
        f'args = {args_repr}\n'
        '[mcp_servers.agent_crew.env]\n'
        f'AGENT_CREW_DB = "{db_path}"\n'
        f'PYTHONPATH = "{pythonpath}"\n'
    )
    codex_home = os.path.join(worktree_path, ".codex_local")
    os.makedirs(codex_home, exist_ok=True)
    path = os.path.join(codex_home, "config.toml")
    with open(path, "w") as f:
        f.write(toml_text)
    _bootstrap_codex_auth(codex_home)
    return os.path.abspath(path)


def _write_mcp_config_gemini(worktree_path: str, db_path: str) -> str:
    """Gemini auto-discovers ``<worktree>/.gemini/settings.json`` when run
    from inside the worktree (project scope). We merge our `mcpServers`
    block into whatever else might already live there."""
    interpreter, args, pythonpath = _mcp_python_invocation()
    settings_dir = os.path.join(worktree_path, ".gemini")
    os.makedirs(settings_dir, exist_ok=True)
    path = os.path.join(settings_dir, "settings.json")
    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f) or {}
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["agent_crew"] = {
        "command": interpreter,
        "args": args,
        "env": {
            "AGENT_CREW_DB": db_path,
            "PYTHONPATH": pythonpath,
        },
    }
    existing["mcpServers"] = servers
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)
    return os.path.abspath(path)


def write_mcp_config(
    worktree_path: str,
    db_path: str,
    agent: str = "claude",
) -> str:
    """Write the per-worktree MCP config for ``agent``.

    Each agent CLI reads its MCP registration from a different place,
    so the default Claude-Code ``.mcp.json`` we used in #106 phase 2 was
    invisible to codex and gemini (Issue #110 phase 5):

    - claude → ``<worktree>/.mcp.json``
    - codex  → ``<worktree>/.codex_local/config.toml`` (with ``CODEX_HOME``
      env on launch — see ``_get_agent_cmd``)
    - gemini → ``<worktree>/.gemini/settings.json`` (auto-discovered when
      gemini is launched from the worktree)

    Falls back to the claude path for unknown agent names so legacy
    callers and ad-hoc one-off agents keep working.
    """
    if agent == "codex":
        return _write_mcp_config_codex(worktree_path, db_path)
    if agent == "gemini":
        return _write_mcp_config_gemini(worktree_path, db_path)
    return _write_mcp_config_claude(worktree_path, db_path)


def write_mcp_configs(
    worktrees: dict,
    db_path: str,
    roles: list[dict] | None = None,
) -> None:
    """Apply :func:`write_mcp_config` to every agent worktree using the
    correct per-agent config layout.

    When ``roles`` is provided (new role_based schema), uses each entry's
    agent name for the per-agent config layout. Otherwise assumes the
    worktrees dict is keyed by agent name (legacy schema).
    """
    for role, agent, wt_path in _iter_role_entries(worktrees, roles):
        del role  # config layout is agent-specific, role not used
        write_mcp_config(wt_path, db_path, agent=agent)


def write_sessions_json(
    path: str,
    agents: list[dict],
    worktrees: dict[str, str] | None = None,
    roles: list[dict] | None = None,
) -> None:
    """Write sessions.json with the full launch command per agent.

    ``worktrees`` (optional) maps agent name → worktree path. When provided,
    the stored command includes per-worktree env prefixes (e.g.
    ``TELEGRAM_STATE_DIR``) so session-recovery relaunches don't fall back to
    the real bot token (#168).

    ``roles`` (optional) is the new role_based schema's role list. When given,
    the i-th ``agents`` entry's worktree is looked up via ``roles[i]`` so
    duplicate agent names (e.g. claude implementer + claude reviewer) resolve
    to distinct worktrees by position.
    """
    enriched = []
    for i, agent in enumerate(agents):
        name = agent.get("name", "")
        wt_path: str | None = None
        if roles and i < len(roles):
            wt_path = roles[i].get("worktree")
        if wt_path is None:
            wt_path = (worktrees or {}).get(name)
        cmd = _get_agent_cmd(name, wt_path)
        enriched.append({**agent, "cmd": cmd, "started_at": time.time(), "failures": 0})
    session.save_sessions(path, enriched)


def _is_port_listening(port: int) -> bool:
    """Return True if something is accepting connections on 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False


def _collect_active_ports(base: str | None = None) -> set[int]:
    """Return ports from ~/.agent_crew/*/port files whose servers are still listening."""
    if base is None:
        base = os.path.expanduser("~/.agent_crew")
    active: set[int] = set()
    if not os.path.isdir(base):
        return active
    for entry in os.scandir(base):
        if not entry.is_dir():
            continue
        port_file = os.path.join(entry.path, "port")
        if not os.path.isfile(port_file):
            continue
        try:
            port = int(open(port_file).read().strip())
        except (ValueError, OSError):
            continue
        if _is_port_listening(port):
            active.add(port)
    return active


def find_free_port(start: int = 8100) -> int:
    """Find a free port, blacklisting ports held by alive agent_crew servers.

    Scans ~/.agent_crew/*/port files and skips any port whose server is still
    listening. Ports from dead (non-listening) projects remain eligible.
    Binds the socket to verify (SO_REUSEADDR off) to avoid TOCTOU.
    """
    blacklisted = _collect_active_ports()
    port = start
    while True:
        if port in blacklisted:
            port += 1
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1


def write_port_file(path: str, port: int) -> None:
    with open(path, "w") as f:
        f.write(str(port))
