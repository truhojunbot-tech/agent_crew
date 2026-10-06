#!/usr/bin/env python3
"""Repo-owned, evidence-gated crew runtime swap. Never changes live STOP state.

Usage: runtime_swap.sh PROJECT FULL_SHA preflight|go|post
Set AGENT_CREW_SWAP_CEA_ENV_FILE to a private KEY=VAL file. A rollback uses
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

SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
PROJECT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("sha")
    parser.add_argument("step", choices=("preflight", "go", "post"))
    args = parser.parse_args(argv)
    if not PROJECT.fullmatch(args.project):
        parser.error("project must be a simple name")
    if not SHA.fullmatch(args.sha):
        parser.error("sha must be a full lowercase git object ID")
    return args


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
    cea_file = resolve_cea_file(directory)
    if not cea_file.is_file():
        raise RuntimeError(f"CEA env file missing: {cea_file}")
    cea_env = parse_env_file(cea_file)
    if args.step in ("preflight", "go"):
        enter_client_group(cea_env.get("AGENT_CREW_CEA_BROKER_SOCKET", ""),
                           list(sys.argv[1:] if argv is None else argv), cea_file)
    prepare_checkout(checkout, args.sha)
    health = api(port, "/health", args.project)
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
        (evidence / "env.pre.nul").write_bytes(
            complete_environ((Path("/proc") / str(pid) / "environ").read_bytes()))
        (evidence / "env.pre.nul").chmod(0o600)
        subprocess.run([sys.executable, str(SCRIPT_DIR / "spawn_from_env.py"), "--probe-broker",
                        str(evidence / "env.pre.nul"), str(cea_file), str(checkout / "src")], check=True)
        (evidence / "cmdline.pre.nul").write_bytes((Path("/proc") / str(pid) / "cmdline").read_bytes())
        (evidence / "cwd.pre").write_text(os.readlink(Path("/proc") / str(pid) / "cwd"))
        write_json(evidence / "health.pre.json", health)
        write_json(evidence / "preflight.json", {"sha": args.sha, "project": args.project,
                                                 "pid": pid, "port": port,
                                                 "at": datetime.now(timezone.utc).isoformat()})
        print(f"PREFLIGHT_OK evidence={evidence}")
        return 0
    preflight = json.loads((evidence / "preflight.json").read_text())
    if preflight["sha"] != args.sha or preflight["project"] != args.project or preflight["port"] != port:
        raise RuntimeError("preflight evidence does not match this swap")
    if args.step == "go":
        if preflight["pid"] != pid:
            raise RuntimeError("listener changed since preflight")
        if not (evidence / "env.pre.nul").is_file():
            raise RuntimeError("captured environment missing")
        complete_environ((evidence / "env.pre.nul").read_bytes())
        # Recheck immediately before the process boundary; permissions may have changed.
        subprocess.run([sys.executable, str(SCRIPT_DIR / "spawn_from_env.py"), "--probe-broker",
                        str(evidence / "env.pre.nul"), str(cea_file), str(checkout / "src")], check=True)
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
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(1)
            try:
                listener_pid(port)
            except RuntimeError:
                break
        else:
            raise RuntimeError("listener still active after SIGTERM; no SIGKILL issued")
        log = evidence / "server.log"
        command = [sys.executable, str(SCRIPT_DIR / "spawn_from_env.py"),
                   str(evidence / "env.pre.nul"), str(cea_file), str(log), str(port),
                   str(checkout / "src")]
        subprocess.run(command, cwd=(evidence / "cwd.pre").read_text(), check=True)
        for _ in range(40):
            time.sleep(1)
            try:
                after = api(port, "/health", args.project)
                break
            except Exception:
                continue
        else:
            raise RuntimeError(f"health unavailable after relaunch; see {log}")
        paused(db_path, after)
        if after.get("build", {}).get("commit") != args.sha:
            raise RuntimeError(f"relaunch build SHA differs: {after.get('build', {}).get('commit')} != {args.sha}")
        write_json(evidence / "health.post.json", after)
        if any(key.startswith("AGENT_CREW_CEA_") for key in cea_env):
            mark_cea_expected(state_path)
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
    env_dump = evidence / "env.pre.nul"
    if env_dump.exists():
        (evidence / "env.pre.sha256").write_text(hashlib.sha256(env_dump.read_bytes()).hexdigest() + "\n")
        env_dump.unlink()
    print(f"POST_OK evidence={evidence}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
