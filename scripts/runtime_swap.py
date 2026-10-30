#!/usr/bin/env python3
"""Repo-owned, evidence-gated crew runtime swap. Never changes live STOP state.

Usage: runtime_swap.sh PROJECT FULL_SHA preflight|go|post [--accept-env-diff KEY[,KEY]]
Use the project's private fleet.env and cea.env files. A rollback uses
this same command with the previous full SHA after a healthy swap. For recovery
after a failed relaunch, see docs/runtime_swap.md. No SIGKILL.
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent_crew.cea_launch import parse_env_file, resolve_cea_file
if __package__:
    from .spawn_from_env import launch_env, parse_env
else:
    from spawn_from_env import launch_env, parse_env

SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
PROJECT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("sha")
    parser.add_argument("step", choices=("preflight", "go", "post"))
    parser.add_argument("--accept-env-diff", default="", metavar="KEY[,KEY]")
    args = parser.parse_args(argv)
    if not PROJECT.fullmatch(args.project):
        parser.error("project must be a simple name")
    if not SHA.fullmatch(args.sha):
        parser.error("sha must be a full lowercase git object ID")
    names = args.accept_env_diff.split(",") if args.accept_env_diff else []
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
        parser.error("--accept-env-diff needs comma-separated key names")
    args.accept_env_diff = set(names)
    return args


def project_env_files(directory: Path) -> tuple[Path, Path]:
    fleet_file = directory / "fleet.env"
    if not fleet_file.is_file() or fleet_file.resolve() != directory.resolve() / "fleet.env":
        raise RuntimeError("fleet.env missing for project")
    cea_file = resolve_cea_file(directory)
    if cea_file.resolve() != directory.resolve() / "cea.env":
        raise RuntimeError("CEA env must be project-local cea.env")
    if not cea_file.is_file():
        raise RuntimeError("project-local cea.env missing")
    return fleet_file, cea_file


def check_env_diff(live_raw: bytes, declared: dict[str, str], evidence: Path,
                   accepted: set[str]) -> None:
    live_names = set(parse_env(complete_environ(live_raw)))
    declared_names = set(declared)
    diff = {"live_but_undeclared": sorted(live_names - declared_names),
            "declared_but_not_live": sorted(declared_names - live_names)}
    write_json(evidence / "env_diff.json", diff)
    names = (live_names - declared_names) | (declared_names - live_names)
    if names != accepted:
        raise RuntimeError("env diff requires --accept-env-diff naming exactly: "
                           + ", ".join(sorted(names)))


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def check_checkout(path: Path, sha: str) -> None:
    if not (path / "src" / "agent_crew").is_dir():
        raise RuntimeError(f"runtime dir missing: {path}")
    head = run("git", "-C", str(path), "rev-parse", "HEAD")
    if head != sha:
        raise RuntimeError(f"runtime HEAD {head} != {sha}")
    if run("git", "-C", str(path), "status", "--porcelain"):
        raise RuntimeError(f"runtime dirty: {path}")


def prepare_checkout(path: Path, sha: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # The source is the checked-out agent_crew repository, not Alfred scratch.
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(REPO), str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "checkout", "--quiet", "--detach", sha], check=True)
    check_checkout(path, sha)


def require_no_running_work(payload) -> None:
    if isinstance(payload, list):
        tasks = payload
    elif isinstance(payload, dict):
        tasks = payload.get("tasks")
    else:
        tasks = None
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        raise RuntimeError("task response malformed")
    running = [task for task in tasks if task.get("status") == "in_progress"]
    if running:
        raise RuntimeError(f"queue not idle: {len(running)} in-progress task(s)")


def api(port: int, path: str, project: str):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Agent-Crew-Project": project},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def listener_pid(port: int) -> int:
    result = run("ss", "-ltnp")
    for line in result.splitlines():
        if f"127.0.0.1:{port} " in line:
            found = re.search(r"pid=(\d+)", line)
            if found:
                return int(found.group(1))
    raise RuntimeError(f"no listener for 127.0.0.1:{port}")


def enter_client_group(broker_socket: str, argv: list[str], cea_file: Path) -> None:
    """Run preflight/go with the same socket group as spawn_from_env."""
    if not broker_socket:
        return
    gid = os.stat(Path(broker_socket).parent).st_gid
    if os.getegid() == gid:
        return
    group = grp.getgrgid(gid).gr_name
    command = ["env", f"HOME={Path.home()}",
               f"AGENT_CREW_SWAP_CEA_ENV_FILE={cea_file}",
               sys.executable, str(Path(__file__).resolve()), *argv]
    os.execvp("sg", ["sg", group, "-c", shlex.join(command)])
    raise RuntimeError("client-group relaunch returned unexpectedly")


def complete_environ(raw: bytes) -> bytes:
    """Never proceed to relaunch with an empty or truncated environment dump."""
    if not raw or not raw.endswith(b"\0"):
        raise RuntimeError("captured environment missing or incomplete")
    return raw


def read_live_environ(pid: int) -> bytes:
    """Capture only for comparison and recovery source discovery."""
    return complete_environ((Path("/proc") / str(pid) / "environ").read_bytes())


def counts(db_path: Path) -> list[tuple[str, int]]:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT status, count(*) FROM tasks GROUP BY status ORDER BY status").fetchall()


def pending_task_ids(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as db:
        return [row[0] for row in db.execute(
            "SELECT task_id FROM tasks WHERE status='pending' ORDER BY task_id")]


def paused(db_path: Path, health: dict) -> None:
    if health.get("stop", {}).get("paused") is not True:
        raise RuntimeError("runtime STOP must be paused before and after swap")
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT paused FROM runtime_stop WHERE id=1").fetchone()
    if row is None or row[0] != 1:
        raise RuntimeError("database runtime STOP is not paused")


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def startup_budget_seconds() -> float:
    budget = float(os.environ.get("AGENT_CREW_SWAP_STARTUP_BUDGET_SECONDS", "900"))
    if not 0 < budget <= 86400:
        raise ValueError("AGENT_CREW_SWAP_STARTUP_BUDGET_SECONDS must be positive and at most 86400")
    return budget


def wait_for_health(port: int, project: str, expected_sha: str, pid: int,
                    budget: float) -> dict:
    started = time.monotonic()
    next_notice = started + 30
    while True:
        if not process_alive(pid):
            raise RuntimeError(f"new process {pid} died before health")
        try:
            health = api(port, "/health", project)
        except (OSError, ValueError):
            health = None
        if health is not None:
            if health.get("project") != project:
                raise RuntimeError(f"relaunch project differs: {health.get('project')} != {project}")
            served_sha = health.get("build", {}).get("commit")
            if served_sha != expected_sha:
                raise RuntimeError(f"relaunch build SHA differs: {served_sha} != {expected_sha}")
            return health
        now = time.monotonic()
        if now - started >= budget:
            raise RuntimeError(f"startup budget expired after {budget:g}s")
        if now >= next_notice:
            print(f"STARTING pid={pid} elapsed={now - started:.0f}s", flush=True)
            next_notice = now + 30
        time.sleep(min(1, budget - (now - started)))


def spawn_runtime(env_dump: Path, cea_file: Path, log: Path, port: int,
                  source: Path, cwd: str) -> int:
    command = [sys.executable, str(SCRIPT_DIR / "spawn_from_env.py"),
               str(env_dump), str(cea_file), str(log), str(port), str(source)]
    result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    try:
        pid = int(result.stdout.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("spawn did not report a process PID") from exc
    if pid <= 0:
        raise RuntimeError("spawn reported an invalid process PID")
    return pid


def stop_spawned_runtime(pid: int, port: int) -> None:
    # A broker-group launch may leave an sg wrapper between the reported PID
    # and the actual listener. Stop the listener as well before rollback binds.
    try:
        listening_pid = listener_pid(port)
    except RuntimeError:
        listening_pid = None
    if listening_pid is not None and listening_pid != pid:
        os.kill(listening_pid, signal.SIGTERM)
    if process_alive(pid):
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        try:
            listener_pid(port)
        except RuntimeError:
            return
        time.sleep(1)
    raise RuntimeError("new listener still active after SIGTERM; no SIGKILL issued")


def previous_source(env_dump: Path, new_source: Path) -> Path:
    path = parse_env(complete_environ(env_dump.read_bytes())).get("PYTHONPATH", "")
    for entry in path.split(":"):
        source = Path(entry)
        if entry and source != new_source and (source / "agent_crew").is_dir():
            return source
    raise RuntimeError("captured PYTHONPATH has no previous runtime src")


def scrub_captured_env(evidence: Path) -> None:
    env_dump = evidence / "env.pre.nul"
    if env_dump.exists():
        (evidence / "env.pre.sha256").write_text(hashlib.sha256(env_dump.read_bytes()).hexdigest() + "\n")
        env_dump.unlink()


def mark_cea_expected(state_path: Path) -> None:
    """Keep recover's CEA requirement after the swap listener exits."""
    state = json.loads(state_path.read_text())
    state["cea_env_expected"] = True
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=state_path.parent,
                                         prefix=".state-", delete=False) as file:
            temporary = Path(file.name)
            json.dump(state, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary, state_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    args = parse_args(argv)
    home = Path.home()
    directory = home / ".agent_crew" / args.project
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text())
    port = int(state["port"])
    if port <= 0 or port > 65535:
        raise RuntimeError("invalid port in state.json")
    db_path = Path(state["db"]).resolve()
    if not db_path.is_relative_to(directory.resolve()):
        raise RuntimeError("DB path escapes project directory")
    checkout = home / "alfred" / "runtime" / f"agent_crew-{args.sha[:7]}"
    evidence = home / ".sev0-evidence" / f"crew-swap-{args.project}-{args.sha[:7]}"
    fleet_file, cea_file = project_env_files(directory)
    cea_env = parse_env_file(cea_file)
    if args.step in ("preflight", "go"):
        enter_client_group(cea_env.get("AGENT_CREW_CEA_BROKER_SOCKET", ""),
                           list(sys.argv[1:] if argv is None else argv), cea_file)
    prepare_checkout(checkout, args.sha)
    try:
        health = api(port, "/health", args.project)
    except (OSError, ValueError) as exc:
        if args.step == "post":
            raise RuntimeError(f"post refused: health unavailable: {exc}") from exc
        raise
    paused(db_path, health)
    require_no_running_work(api(port, "/tasks", args.project))
    try:
        pid = listener_pid(port)
    except RuntimeError:
        # /health on this exact port proves a listener even when ss hides its
        # PID across the sg group boundary. Only post can use this proof: go
        # still needs a verified PID to signal and preflight to read /proc.
        if args.step != "post":
            raise
        if health.get("build", {}).get("commit") != args.sha:
            raise RuntimeError(f"build SHA differs: {health.get('build', {}).get('commit')} != {args.sha}")
        pid = None
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.chmod(0o700)
    evidence.mkdir(exist_ok=True)
    evidence.chmod(0o700)
    if args.step == "preflight":
        captured = read_live_environ(pid)
        (evidence / "env.pre.nul").write_bytes(captured)
        (evidence / "env.pre.nul").chmod(0o600)
        check_env_diff(captured, launch_env(fleet_file, cea_file, checkout / "src"),
                       evidence, args.accept_env_diff)
        subprocess.run([sys.executable, str(SCRIPT_DIR / "spawn_from_env.py"), "--probe-broker",
                        str(fleet_file), str(cea_file), str(checkout / "src")], check=True)
        (evidence / "cmdline.pre.nul").write_bytes((Path("/proc") / str(pid) / "cmdline").read_bytes())
        (evidence / "cwd.pre").write_text(os.readlink(Path("/proc") / str(pid) / "cwd"))
        write_json(evidence / "health.pre.json", health)
        write_json(evidence / "preflight.json", {"sha": args.sha, "project": args.project,
                                                 "pid": pid, "port": port,
                                                 "fleet_sha256": hashlib.sha256(fleet_file.read_bytes()).hexdigest(),
                                                 "cea_sha256": hashlib.sha256(cea_file.read_bytes()).hexdigest(),
                                                 "at": datetime.now(timezone.utc).isoformat()})
        print(f"PREFLIGHT_OK evidence={evidence}")
        return 0
    preflight = json.loads((evidence / "preflight.json").read_text())
    if preflight["sha"] != args.sha or preflight["project"] != args.project or preflight["port"] != port:
        raise RuntimeError("preflight evidence does not match this swap")
    if args.step == "go":
        if preflight["pid"] != pid:
            raise RuntimeError("listener changed since preflight")
        if (preflight.get("fleet_sha256") and
                hashlib.sha256(fleet_file.read_bytes()).hexdigest() != preflight["fleet_sha256"]):
            raise RuntimeError("fleet.env changed since preflight")
        if (preflight.get("cea_sha256") and
                hashlib.sha256(cea_file.read_bytes()).hexdigest() != preflight["cea_sha256"]):
            raise RuntimeError("cea.env changed since preflight")
        diff_path = evidence / "env_diff.json"
        if diff_path.is_file():
            diff = json.loads(diff_path.read_text())
            required = set(diff["live_but_undeclared"]) | set(diff["declared_but_not_live"])
            if required != args.accept_env_diff:
                raise RuntimeError("go requires --accept-env-diff naming exactly: "
                                   + ", ".join(sorted(required)))
        if not (evidence / "env.pre.nul").is_file():
            raise RuntimeError("captured environment missing")
        complete_environ((evidence / "env.pre.nul").read_bytes())
        # Recheck immediately before the process boundary; permissions may have changed.
        subprocess.run([sys.executable, str(SCRIPT_DIR / "spawn_from_env.py"), "--probe-broker",
                        str(fleet_file), str(cea_file), str(checkout / "src")], check=True)
        # Back up before the irreversible process boundary. SQLite backup is consistent
        # even with WAL enabled; retain the original state and pause files verbatim.
        with sqlite3.connect(db_path) as source, sqlite3.connect(evidence / "tasks.db.pre") as target:
            source.backup(target)
        (evidence / "tasks.db.pre.sha256").write_text(
            hashlib.sha256((evidence / "tasks.db.pre").read_bytes()).hexdigest() + "\n")
        shutil.copy2(state_path, evidence / "state.json.pre")
        if (directory / "pause.json").exists():
            shutil.copy2(directory / "pause.json", evidence / "pause.json.pre")
        write_json(evidence / "counts.pre.json", counts(db_path))
        write_json(evidence / "pending.pre.json", pending_task_ids(db_path))
        log = evidence / "server.log"
        env_dump = evidence / "env.pre.nul"
        cwd = (evidence / "cwd.pre").read_text()
        previous_sha = json.loads((evidence / "health.pre.json").read_text())["build"]["commit"]
        old_source = previous_source(env_dump, checkout / "src")
        budget = startup_budget_seconds()
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(1)
            try:
                listener_pid(port)
            except RuntimeError:
                break
        else:
            raise RuntimeError("listener still active after SIGTERM; no SIGKILL issued")
        new_pid = None
        try:
            new_pid = spawn_runtime(fleet_file, cea_file, log, port, checkout / "src", cwd)
            after = wait_for_health(port, args.project, args.sha, new_pid, budget)
            paused(db_path, after)
            write_json(evidence / "health.post.json", after)
            if any(key.startswith("AGENT_CREW_CEA_") for key in cea_env):
                mark_cea_expected(state_path)
        except Exception:
            try:
                if new_pid is not None:
                    stop_spawned_runtime(new_pid, port)
                rollback_pid = spawn_runtime(fleet_file, cea_file, log, port, old_source, cwd)
                restored = wait_for_health(port, args.project, previous_sha, rollback_pid, budget)
                paused(db_path, restored)
                scrub_captured_env(evidence)
                print(f"ROLLBACK_OK served_sha={restored['build']['commit']}", flush=True)
            except Exception as rollback_error:
                try:
                    served_sha = api(port, "/health", args.project).get("build", {}).get("commit")
                except Exception:
                    served_sha = None
                print(f"ROLLBACK_FAILED served_sha={served_sha or 'unknown'} error={rollback_error}", flush=True)
            raise
        scrub_captured_env(evidence)
        print(f"GO_DONE evidence={evidence}; run post")
        return 0
    # post: all evidence gates are mandatory and failures are explicit.
    if health.get("build", {}).get("commit") != args.sha:
        raise RuntimeError(f"build SHA differs: {health.get('build', {}).get('commit')} != {args.sha}")
    post_counts = counts(db_path)
    if post_counts != [tuple(row) for row in json.loads((evidence / "counts.pre.json").read_text())]:
        raise RuntimeError("task status counts changed across swap")
    post_pending = pending_task_ids(db_path)
    if post_pending != json.loads((evidence / "pending.pre.json").read_text()):
        raise RuntimeError("pending task IDs changed across swap")
    write_json(evidence / "counts.post.json", post_counts)
    write_json(evidence / "pending.post.json", post_pending)
    write_json(evidence / "health.post.json", health)
    # The captured environment can carry provider tokens. It is needed only
    # until a verified post; retain its digest, not its secret contents.
    scrub_captured_env(evidence)
    print(f"POST_OK evidence={evidence}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
