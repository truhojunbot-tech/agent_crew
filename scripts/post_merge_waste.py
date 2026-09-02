#!/usr/bin/env python3
"""Count cascade work that happened AFTER a PR became terminal (#250).

Context Efficiency reports treat every review/fix invocation as pipeline cost.
Work performed after a merge is a different category: it cannot improve the
artifact, so it belongs in "waste", not in "cost of landing this PR". This
separates the two.

    python3 scripts/post_merge_waste.py 241 [245 ...]

Per PR it reports:
  * automated exhaustion comments, split pre/post terminal state — each is
    emitted once per COMPLETED review result, so the post-terminal count is a
    FLOOR on post-terminal reviewer invocations;
  * cascade tasks in the local crew DBs naming the PR, split the same way.

⛔Token/wall-clock cost is reported only where durable attribution exists. For
  #241 it does not: the lineages behind the post-merge comments are in no
  surviving state directory. This prints what is countable and says so, rather
  than estimating a number nobody can check.
"""

import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys

MARKER = "[agent_crew] Automated fix rounds exhausted"
LINEAGE = re.compile(r"Latest review task: `([^`]+)`")
CREW_BASE = os.path.expanduser(os.getenv("AGENT_CREW_BASE", "~/.agent_crew"))


def _terminal_at(pr: int):
    r = subprocess.run(["gh", "pr", "view", str(pr), "--json", "mergedAt,closedAt,state"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, "unknown"
    d = json.loads(r.stdout or "{}")
    stamp = d.get("mergedAt") or d.get("closedAt")
    if not stamp:
        return None, (d.get("state") or "OPEN").lower()
    return (datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")),
            "merged" if d.get("mergedAt") else "closed")


def _comments(pr: int):
    r = subprocess.run(["gh", "pr", "view", str(pr), "--json", "comments"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return (json.loads(r.stdout or "{}") or {}).get("comments") or []


def _local_tasks(pr: int):
    out = []
    for db in sorted(glob.glob(os.path.join(CREW_BASE, "*", "tasks.db"))):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            rows = c.execute("select task_id, task_type, created_at from tasks "
                             "where context like ?", (f'%"pr_number": {pr}%',)).fetchall()
        except Exception:
            continue
        out.extend((db.split(os.sep)[-2], dict(r)) for r in rows)
    return out


def report(pr: int) -> dict:
    terminal_at, state = _terminal_at(pr)
    comments = [c for c in _comments(pr) if MARKER in (c.get("body") or "")]

    def at(c):
        return datetime.datetime.fromisoformat(c["createdAt"].replace("Z", "+00:00"))

    post = [c for c in comments if terminal_at and at(c) > terminal_at]
    tasks = _local_tasks(pr)
    post_tasks = [t for _, t in tasks if terminal_at and datetime.datetime.fromtimestamp(
        t["created_at"], datetime.timezone.utc) > terminal_at]
    lineages = {m.group(1) for c in post for m in [LINEAGE.search(c.get("body") or "")] if m}

    print(f"PR #{pr}: {state}" + (f" at {terminal_at:%Y-%m-%d %H:%M:%S}Z" if terminal_at else ""))
    print(f"  automated exhaustion comments : {len(comments)}"
          f"  (pre-terminal {len(comments) - len(post)}, POST-terminal {len(post)})")
    if post:
        print(f"  post-terminal lineages        : {len(lineages)} distinct review tasks")
        print(f"  post-terminal window          : {at(post[0]):%H:%M:%S}Z .. {at(post[-1]):%H:%M:%S}Z"
              f"  ({(at(post[-1]) - terminal_at).total_seconds() / 3600:.1f}h after terminal)")
    print(f"  cascade tasks in local crew DBs: {len(tasks)} (POST-terminal {len(post_tasks)})")
    if post and not post_tasks:
        print("  NOTE: post-terminal comments exist with no local task rows — those")
        print("        lineages ran under a crew state directory that no longer exists,")
        print("        so per-task token/time cost cannot be attributed. The comment")
        print("        count is a floor on post-terminal reviewer invocations.")
    return {"pr": pr, "state": state, "comments": len(comments),
            "post_terminal_comments": len(post), "post_terminal_lineages": len(lineages),
            "post_terminal_tasks": len(post_tasks)}


if __name__ == "__main__":
    for pr in [int(a) for a in sys.argv[1:]] or [241]:
        report(pr)
        print()
