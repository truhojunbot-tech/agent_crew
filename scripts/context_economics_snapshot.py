#!/usr/bin/env python3
"""Read-only snapshot of the fleet's context economics (#269).

#269 asks for organic before/after evidence that #261's provider-session
binding and the Claude/Codex context caps are actually in effect on the
running fleet. "Actually in effect" is the hard part: a merged commit is not a
running process, and #248 exists because that distinction was repeatedly lost.

So this reports four things together, and refuses to report any of them alone:

  1. what each running dispatcher ACTUALLY imported — commit and source
     fingerprint straight off ``GET /provenance``, plus which ports do not
     answer it at all (those predate #248 and are therefore older still);
  2. provider store / rollout sizes measured with the SAME functions the
     dispatcher uses to decide a cap trip, so "over cap" here means what it
     means there;
  3. how the durable attribution rows are populated — policy mix and, per
     agent, what fraction carries a ``provider_session_id``. This is the
     column quota-core/quota-ops need to form a resume-vs-fresh cohort, and
     an agent at 0% cannot be joined to anything;
  4. cap/reset lifecycle events, joined to the generations they produced.

⛔Read-only by construction. It opens every SQLite DB with ``mode=ro``, never
  writes, and never touches a provider store. #269's own measurement rules
  forbid manufacturing a sample — do not "help" this script by lowering a cap
  or inflating a session; a bounded-growth window is a real result.

⛔"Refuses" is load-bearing and has to hold when the environment is broken, not
  only when it is healthy. If the cap functions cannot be imported *from this
  checkout*, the script raises `SnapshotUnavailable`, prints nothing and exits
  non-zero. It used to catch that ImportError, return no sizes and still emit a
  complete-looking snapshot with exit 0 (review of PR #271) — so a stale
  install could pass a post-deploy verification with no cap data at all, and
  the only signal was one stderr line that a `2>/dev/null` redirect eats. The
  run that produced this repo's own baseline artifact used exactly that
  redirect.

  Per-project degradations are different and are *recorded* rather than
  refused: a project predating the context-identity schema appears in the
  output with its `error`, because that is a fact about the fleet.

Usage:  python3 scripts/context_economics_snapshot.py [--json] [--days N]
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("AGENT_CREW_BASE", os.path.expanduser("~/.agent_crew"))
MB = 1048576


def _ts(value) -> float:
    """Epoch seconds from either a float or an ISO string.

    The two stores disagree: `task_attribution` keeps floats, the
    `context_events.jsonl` lines keep ISO strings. Comparing them without
    normalising raises `str > float`, which is how a naive window filter
    silently reports zero events.
    """
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def running_dispatchers() -> list[dict]:
    """Provenance for every port with a state.json, from the process itself."""
    out = []
    for state_path in sorted(glob.glob(os.path.join(BASE, "*", "state.json"))):
        project = os.path.basename(os.path.dirname(state_path))
        try:
            port = json.load(open(state_path)).get("port")
        except Exception:
            continue
        if not port:
            continue
        row = {"project": project, "port": port}
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/provenance", timeout=5) as r:
                body = json.load(r)
            if "commit" in body:
                row.update(commit=body["commit_short"],
                           fingerprint=body["code_fingerprint"][:16],
                           dirty=body["dirty"],
                           moved=body["checkout_moved_since_start"],
                           source_changed=body["source_changed_since_start"],
                           uptime_h=round(body["uptime_s"] / 3600, 1))
            else:
                row["commit"] = "pre-#248 (no /provenance)"
        except urllib.error.HTTPError:
            # ⛔A 404 is a RUNNING server, and reporting it as "not running"
            #   would be the exact false evidence this script exists to avoid.
            #   `/provenance` landed in #248, so a server that answers the port
            #   but not the route is running something older than #248 — which
            #   is itself the provenance answer.
            row["commit"] = "pre-#248 (no /provenance)"
        except (urllib.error.URLError, OSError, TimeoutError):
            row["commit"] = "not running"
        out.append(row)
    return out


class SnapshotUnavailable(RuntimeError):
    """The snapshot cannot be produced truthfully, so it will not be produced.

    Distinct from a partial result on purpose. Missing cap data is not a
    smaller answer to #269's criteria 3 and 4 — it is no answer, and an
    artifact that omits it while looking complete is worse than no artifact.
    """


def expected_package_dir() -> str:
    """``src/agent_crew`` next to this script — the checkout being verified."""
    return os.path.realpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "src", "agent_crew"))


def cap_functions() -> dict:
    """The dispatcher's own cap functions, or refuse to run.

    ⛔Two failures, one refusal. The import can fail outright, and it can
      *succeed against the wrong tree* — a bare `python3` inside an agent
      worktree here resolves `agent_crew` to a stale editable install elsewhere
      on the box, which is the more dangerous case because it yields numbers
      that look right. Measuring with code other than the checkout under
      verification is the #248 mistake in miniature, so both are hard failures.
    """
    hint = (f"Run `PYTHONPATH=src python3 scripts/{os.path.basename(__file__)}` "
            f"from the checkout you mean to measure.")
    try:
        import agent_crew  # noqa: PLC0415 — resolution is what we are checking
        from agent_crew.server import (  # noqa: PLC0415
            AGY_CONTEXT_MAX_MB, CLAUDE_CONTEXT_MAX_MB, CODEX_CONTEXT_MAX_MB,
            agy_context_exceeds_cap, claude_context_exceeds_cap,
            codex_context_exceeds_cap, codex_session_for_cwd,
        )
    except ImportError as e:
        raise SnapshotUnavailable(
            f"cannot import the cap functions from agent_crew.server ({e}). They "
            f"are what decides a cap trip, so without them there is no cap "
            f"evidence and no snapshot. {hint}") from e
    resolved = os.path.realpath(os.path.dirname(getattr(agent_crew, "__file__", "") or ""))
    expected = expected_package_dir()
    if resolved != expected:
        raise SnapshotUnavailable(
            f"agent_crew resolved to {resolved!r}, not this checkout's "
            f"{expected!r}. Measuring with a different tree than the one being "
            f"verified is how #248 happened, so this is a refusal and not a "
            f"warning. {hint}")
    return {
        "caps": {"claude": CLAUDE_CONTEXT_MAX_MB, "codex": CODEX_CONTEXT_MAX_MB,
                 "gemini": AGY_CONTEXT_MAX_MB},
        "claude": claude_context_exceeds_cap,
        "codex": codex_context_exceeds_cap,
        "gemini": agy_context_exceeds_cap,
        "codex_session_for_cwd": codex_session_for_cwd,
        "measured_with": resolved,
    }


def provider_sizes() -> list[dict]:
    """Store/rollout size per worktree, via the dispatcher's own cap functions.

    Raises ``SnapshotUnavailable`` rather than returning ``[]`` — an empty list
    here is not a smaller answer, it is no answer wearing one's clothes.
    """
    logging.disable(logging.CRITICAL)
    fns = cap_functions()
    claude_context_exceeds_cap = fns["claude"]
    codex_context_exceeds_cap = fns["codex"]
    agy_context_exceeds_cap = fns["gemini"]
    codex_session_for_cwd = fns["codex_session_for_cwd"]
    caps = fns["caps"]
    rows = []
    for state_path in sorted(glob.glob(os.path.join(BASE, "*", "state.json"))):
        project = os.path.basename(os.path.dirname(state_path))
        try:
            worktrees = (json.load(open(state_path)).get("worktrees") or {})
        except Exception:
            continue
        for agent, wt in worktrees.items():
            if agent not in caps or not os.path.isdir(wt):
                continue
            if agent == "codex":
                # Measure the session that would actually be resumed, not the
                # newest one — that alignment is the #260-review fix and the
                # difference between a real trip and a wrong one.
                over, info = codex_context_exceeds_cap(
                    wt, session_id=codex_session_for_cwd(wt))
            elif agent == "claude":
                over, info = claude_context_exceeds_cap(wt)
            else:
                over, info = agy_context_exceeds_cap(wt)
            rows.append({"project": project, "agent": agent,
                         "mb": round(info["bytes"] / MB, 2),
                         "cap_mb": caps[agent], "over": bool(over),
                         "session": info.get("conversation_id") or ""})
    return rows


def attribution(days: float) -> tuple[list[dict], dict]:
    """Policy mix per project, and session-id coverage per agent."""
    window = time.time() - days * 86400
    per_project, per_agent = [], {}
    for db in sorted(glob.glob(os.path.join(BASE, "*", "tasks.db"))):
        project = os.path.basename(os.path.dirname(db))
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            policy = dict(conn.execute(
                "SELECT COALESCE(NULLIF(context_policy,''),'(null)'), COUNT(*) "
                "FROM task_attribution WHERE created_at>? GROUP BY 1", (window,)))
            gens = conn.execute(
                "SELECT COUNT(DISTINCT context_id) FROM task_attribution "
                "WHERE created_at>? AND COALESCE(context_generation,0)>1",
                (window,)).fetchone()[0]
            per_project.append({"project": project, "policy": policy,
                                "contexts_past_gen1": gens})
            for agent, n, with_sid in conn.execute(
                    "SELECT agent, COUNT(*), SUM(CASE WHEN "
                    "COALESCE(provider_session_id,'')<>'' THEN 1 ELSE 0 END) "
                    "FROM task_attribution WHERE created_at>? GROUP BY 1", (window,)):
                slot = per_agent.setdefault(agent, {"dispatches": 0, "with_session_id": 0})
                slot["dispatches"] += n
                slot["with_session_id"] += with_sid or 0
            conn.close()
        except sqlite3.Error as e:
            # A project predating the context-identity schema is a fact about
            # the fleet, not an error to swallow silently.
            per_project.append({"project": project, "policy": {}, "error": str(e)})
    for slot in per_agent.values():
        slot["pct"] = (round(100 * slot["with_session_id"] / slot["dispatches"], 1)
                       if slot["dispatches"] else 0.0)
    return per_project, per_agent


def cap_events(days: float) -> list[dict]:
    """`provider_context_capped` / `context_reset` lines inside the window."""
    window = time.time() - days * 86400
    out = []
    for path in sorted(glob.glob(os.path.join(BASE, "*", "context_events.jsonl"))):
        for line in open(path):
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("event_type") not in ("provider_context_capped", "context_reset"):
                continue
            if _ts(event.get("ts")) < window:
                continue
            out.append({k: event.get(k) for k in
                        ("ts", "event_type", "project", "agent", "provider",
                         "context_id", "context_generation", "conversation_id",
                         "bytes", "cap_mb", "task_id")})
    return sorted(out, key=lambda e: str(e.get("ts")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=float, default=7.0, help="attribution window")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # ⛔Cap functions FIRST, and outside the snapshot dict. Nothing is printed
    #   until they resolve, so a broken or foreign environment produces a
    #   non-zero exit and an empty stdout rather than a complete-looking
    #   artifact with the cap evidence quietly missing (review of PR #271).
    try:
        fns = cap_functions()
        sizes = provider_sizes()
    except SnapshotUnavailable as e:
        print(f"REFUSING TO REPORT: {e}", file=sys.stderr)
        return 2

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        # Which code did the measuring, recorded in the artifact itself —
        # the #248 lesson applied to the output rather than only to the input.
        "measured_with": fns["measured_with"],
        "dispatchers": running_dispatchers(),
        "provider_sizes": sizes,
    }
    snapshot["attribution"], snapshot["session_id_coverage"] = attribution(args.days)
    snapshot["cap_events"] = cap_events(args.days)

    if args.json:
        print(json.dumps(snapshot, indent=2))
        return 0

    print(f"# context economics snapshot — {snapshot['captured_at']} "
          f"(window {args.days}d)\n")
    print(f"measured with: {snapshot['measured_with']}\n")
    print("## running dispatchers (what the process imported, not what is on disk)")
    for d in snapshot["dispatchers"]:
        extra = (f"fp={d['fingerprint']} uptime={d['uptime_h']}h "
                 f"moved={d['moved']} src_changed={d['source_changed']}"
                 if "fingerprint" in d else "")
        print(f"  {d['project']:<20} :{d['port']}  {d['commit']:<26} {extra}")

    print("\n## provider store / rollout size vs the cap the dispatcher applies")
    for r in sorted(snapshot["provider_sizes"], key=lambda r: -r["mb"]):
        flag = "  ← OVER CAP" if r["over"] else ""
        print(f"  {r['project']:<20} {r['agent']:<7} {r['mb']:9.2f} MB / "
              f"{r['cap_mb']:.0f} MB{flag}")

    print("\n## context policy mix")
    for r in snapshot["attribution"]:
        if r.get("error"):
            print(f"  {r['project']:<20} (no context-identity schema: {r['error']})")
        else:
            print(f"  {r['project']:<20} {r['policy']}  "
                  f"contexts past gen1: {r['contexts_past_gen1']}")

    print("\n## provider_session_id coverage (the join key for a resume cohort)")
    for agent, slot in sorted(snapshot["session_id_coverage"].items()):
        print(f"  {agent:<8} {slot['with_session_id']:>5}/{slot['dispatches']:<6} "
              f"= {slot['pct']:5.1f}%")

    print(f"\n## cap / reset events in the window: {len(snapshot['cap_events'])}")
    for e in snapshot["cap_events"]:
        size = f"{e['bytes'] / MB:.1f} MB" if e.get("bytes") else ""
        print(f"  {e['ts']}  {e['event_type']:<24} {e['project']}/{e['agent']} "
              f"gen={e['context_generation']} {size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
