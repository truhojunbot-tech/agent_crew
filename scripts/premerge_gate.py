#!/usr/bin/env python3
"""Cron entry point for the agent_crew pre-merge full-suite gate (#726).

Run this from any directory. Only this repository's approved, ungated PR heads
are tested. Optional --blackboard-command (or AGENT_CREW_PREMERGE_BLACKBOARD_COMMAND)
receives one JSON result on stdin after the PR comment is posted.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


REPO = "truhojunbot-tech/agent_crew"
ROOT = Path(__file__).resolve().parent.parent
REVIEW = "[agent_crew review] verdict:"
MARKER = "[agent_crew premerge gate]"
SHA = re.compile(r"\b[0-9a-f]{40}\b", re.I)
SUMMARY = re.compile(r"^(?:=+\s*)?\d+ (?:passed|failed|error|skipped|xfailed|xpassed).*", re.I)
safe_rmtree = shutil.rmtree


class AlreadyRunning(RuntimeError):
    """A previous cron run still owns the lock."""


def run(args, **kwargs):
    kwargs.setdefault("timeout", 60)
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def checked(args, **kwargs):
    result = run(args, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stderr.strip()}")
    return result.stdout


@contextmanager
def single_run_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(str(path)) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _api_list(endpoint):
    rows = []
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        text = checked(["gh", "api", f"{endpoint}{separator}page={page}"], cwd=ROOT)
        batch = json.loads(text)
        rows.extend(batch)
        if len(batch) < 100:
            return rows
        page += 1


def needs_gate(pr, comments):
    """Require the latest review verdict to approve this exact head."""
    head = pr.get("head", {}).get("sha", "").lower()
    if not SHA.fullmatch(head):
        return False
    latest = None
    for comment in sorted(comments, key=lambda c: (c.get("created_at", ""), c.get("id", 0))):
        body = comment.get("body") or ""
        if body.startswith(REVIEW):
            latest = body
        if (MARKER in body or "Coordinator merge gate:" in body) and head in body.lower():
            return False
    if not latest:
        return False
    first_line = latest.splitlines()[0]
    if not re.fullmatch(r"\[agent_crew review\] verdict:\s*(?:✅\s*)?approve\s*", first_line):
        return False
    reviewed = re.search(r"\bat\s+([0-9a-f]{7,40})\b", latest.lower())
    return bool(reviewed and head.startswith(reviewed.group(1)))


def pytest_summary(output):
    for line in reversed(output.splitlines()):
        stripped = line.strip().strip("= ")
        if SUMMARY.match(stripped):
            return stripped
    return "pytest summary unavailable"


def gate_comment(head, main, summary, exit_code):
    return (f"{MARKER}\n"
            f"HEAD {head}\nMAIN {main}\n"
            f"pytest: {summary}\nEXIT {exit_code}\n")


def test_pr(number, head, main, work_dir):
    scratch = Path(tempfile.mkdtemp(prefix=f"ac_gate_{number}_", dir=work_dir))
    worktree = scratch / "worktree"
    basetemp = Path("/dev/shm") / f"ac_gate_{number}"
    added = False
    try:
        # A crashed previous run can leave the pytest basetemp behind.
        if basetemp.exists():
            safe_rmtree(basetemp)
        checked(["git", "worktree", "add", "--detach", str(worktree), main], cwd=ROOT)
        added = True
        squash = run(["git", "-C", str(worktree), "merge", "--squash", head], cwd=ROOT)
        if squash.returncode:
            return "pytest not run: squash conflict", squash.returncode
        cmd = ["timeout", "7200", "python3", "-m", "pytest", "-q", "--timeout=180",
               "-p", "no:cacheprovider", f"--basetemp={basetemp}", "-rf", "tests"]
        env = dict(os.environ, PYTHONPATH="src")
        result = run(cmd, cwd=worktree, env=env, timeout=7300)
        return pytest_summary(result.stdout + "\n" + result.stderr), result.returncode
    finally:
        if added:
            checked(["git", "worktree", "remove", "--force", str(worktree)], cwd=ROOT)
        if basetemp.exists():
            safe_rmtree(basetemp)
        safe_rmtree(scratch)


def post_blackboard(command, payload):
    if not command:
        return
    checked(shlex.split(command), input=json.dumps(payload), cwd=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", default="/tmp/agent_crew_premerge_gate.lock")
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--blackboard-command",
                        default=os.getenv("AGENT_CREW_PREMERGE_BLACKBOARD_COMMAND"))
    args = parser.parse_args(argv)
    try:
        with single_run_lock(args.lock_file):
            prs = _api_list(f"repos/{REPO}/pulls?state=open&per_page=100")
            main = None
            for pr in prs:
                number = pr["number"]
                head = pr["head"]["sha"]
                comments = _api_list(f"repos/{REPO}/issues/{number}/comments?per_page=100")
                if not needs_gate(pr, comments):
                    continue
                if main is None:
                    checked(["git", "fetch", "origin", "main"], cwd=ROOT)
                    main = checked(["git", "rev-parse", "origin/main"], cwd=ROOT).strip()
                checked(["git", "fetch", "origin", f"pull/{number}/head"], cwd=ROOT)
                fetched = checked(["git", "rev-parse", "FETCH_HEAD"], cwd=ROOT).strip()
                if fetched != head:
                    print(f"PR #{number}: head changed during selection; skipping", file=sys.stderr)
                    continue
                summary, exit_code = test_pr(number, head, main, args.work_dir)
                body = gate_comment(head, main, summary, exit_code)
                checked(["gh", "pr", "comment", str(number), "--repo", REPO,
                         "--body", body], cwd=ROOT)
                post_blackboard(args.blackboard_command, {
                    "repo": REPO, "pr_number": number, "head_sha": head,
                    "main_sha": main, "summary": summary, "exit_code": exit_code,
                })
                print(f"PR #{number}: {summary}; EXIT {exit_code}")
    except AlreadyRunning:
        print("premerge gate already running", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
