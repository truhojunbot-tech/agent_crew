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

    # Start server — include sys.path in PYTHONPATH so subprocess can import agent_crew
    db_file = os.path.join(proj_dir, "tasks.db")
    pythonpath = os.pathsep.join(p for p in sys.path if p)
    server_env = {**os.environ, "AGENT_CREW_DB": db_file, "PYTHONPATH": pythonpath}
    log_file = open(os.path.join(proj_dir, "server.log"), "w")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_crew.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        env=server_env,
        stdout=log_file,
        stderr=log_file,
    )

    # Wait until server is ready (up to 15 s) before returning
    if not _port_listening(port, timeout=15.0):
        server_proc.terminate()
        log_file.close()
        raise click.ClickException(
            f"server failed to start on port {port}. "
            f"Check {os.path.join(proj_dir, 'server.log')}"
        )

    # tmux session + panes
    session_name = f"crew_{project}"
    first_wt = next(iter(worktrees.values()))
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-c", first_wt],
                   capture_output=True)
    for i, (_, wt_path) in enumerate(worktrees.items()):
        if i > 0:
            subprocess.run(["tmux", "new-window", "-t", session_name, "-c", wt_path],
                           capture_output=True)

    # Start agent CLIs and send polling prompt
    setup_module.start_agents_in_panes(session_name, agent_list, port)

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
        start_dir = next(iter(worktrees.values())) if worktrees else proj_dir
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", start_dir],
            capture_output=True,
        )
        for i, (_, wt_path) in enumerate(worktrees.items()):
            if i > 0:
                subprocess.run(
                    ["tmux", "new-window", "-t", session_name, "-c", wt_path],
                    capture_output=True,
                )
        recovered.append("tmux")

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

    def _wait(task_id: str):
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            result = queue.get_result(task_id)
            if result is not None:
                return result
            time.sleep(0.5)
        raise click.ClickException(f"task {task_id!r} timed out after {wait_timeout}s")

    impl_id = enqueue_implement(queue, task, branch)
    click.echo(f"[1/{max_iter}] Implementing... ({impl_id})")

    for iteration in range(1, max_iter + 1):
        _wait(impl_id)
        click.echo(f"[{iteration}/{max_iter}] Implementation done.")

        review_id = enqueue_review(queue, task, branch, prev_task_id=impl_id)
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
            if not no_tester:
                test_id = enqueue_test(queue, task, branch)
                click.echo(f"[{iteration}/{max_iter}] Testing... ({test_id})")
                test_result = _wait(test_id)
                test_outcome = handle_test_result(test_result)
                if test_outcome == "passed":
                    click.echo(f"[{iteration}/{max_iter}] Tests passed. Loop complete.")
                    return
                else:
                    click.echo(f"[{iteration}/{max_iter}] Tests {test_outcome}. Re-implementing.")
                    impl_id = enqueue_implement(queue, task, branch,
                                               context={"retry": True})
                    continue
            else:
                click.echo(f"[{iteration}/{max_iter}] Loop complete (no tester).")
            return

        # request_changes: re-implement with feedback
        click.echo(f"[{iteration}/{max_iter}] Changes requested. Re-implementing.")
        feedback = build_feedback(review_result)
        impl_id = enqueue_implement(queue, task, branch, context={"feedback": feedback})

    click.echo(f"Max iterations ({max_iter}) reached without approval.")


@crew.command()
@click.argument("topic")
@click.option("--agents", default="analyst,critic,advocate,risk", help="Comma-separated panel agent names")
@click.option("--rounds", default=1, type=int, show_default=True, help="Number of discussion rounds")
@click.option("--then-run", is_flag=True, help="Trigger code-review loop after synthesis")
@click.option("--db", default="", help="SQLite DB path (standalone)")
@click.option("--project", default="", help="Project name (reads DB from state)")
@click.option("--base", default=_DEFAULT_BASE, show_default=True)
@click.option("--output", default="synthesis.md", show_default=True, help="Path to write synthesis")
@click.option("--branch", default="main", show_default=True)
@click.option("--timeout", default=600, type=int, show_default=True, help="Task wait timeout in seconds")
def discuss(topic: str, agents: str, rounds: int, then_run: bool,
            db: str, project: str, base: str, output: str, branch: str, timeout: int):
    """Start a panel discussion on TOPIC. TOPIC must not be empty."""
    if not topic.strip():
        raise click.UsageError("topic must not be empty")

    if not db:
        if not project:
            raise click.ClickException("--db or --project is required")
        state = _read_state(base, project)
        if state is None:
            raise click.ClickException(f"project {project!r} not found")
        db = state["db"]

    from agent_crew.discussion import assign_perspectives, build_synthesis, enqueue_panel_tasks
    from agent_crew.loop import enqueue_implement
    from agent_crew.queue import TaskQueue
    import time

    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    queue = TaskQueue(db)
    perspectives = assign_perspectives(agent_list)

    wait_timeout = float(timeout)

    def _wait_all(task_ids: list[str], timeout: float = 0.0) -> dict:
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
                return done
            time.sleep(0.1)
        raise click.ClickException(f"Discussion timed out after {effective_timeout}s")

    prior_synthesis = ""
    final_synthesis = ""

    for round_num in range(1, rounds + 1):
        context: dict = {"round": round_num}
        if round_num > 1 and prior_synthesis:
            context["prior_synthesis"] = prior_synthesis

        task_ids = enqueue_panel_tasks(queue, agent_list, topic, context)
        results_map = _wait_all(task_ids)

        panel_results = []
        for agent, tid in zip(agent_list, task_ids):
            r = results_map[tid]
            panel_results.append({
                "agent": agent,
                "perspective": perspectives[agent],
                "summary": r.summary,
            })

        final_synthesis = build_synthesis(
            panel_results,
            topic=topic,
            synthesis=f"Round {round_num} synthesis.",
            decision="Proceed as discussed.",
        )
        prior_synthesis = final_synthesis

    with open(output, "w") as f:
        f.write(final_synthesis)

    click.echo(f"Discussion complete. Synthesis written to {output}")

    if then_run:
        from agent_crew.loop import (
            DEFAULT_MAX_ITER,
            build_feedback,
            enqueue_review,
            handle_review_result,
        )

        max_iter = DEFAULT_MAX_ITER
        impl_id = enqueue_implement(queue, topic, branch)
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

            review_id = enqueue_review(queue, topic, branch, prev_task_id=impl_id)
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
            impl_id = enqueue_implement(queue, topic, branch, context={"feedback": feedback})

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
