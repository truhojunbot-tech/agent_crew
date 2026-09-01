#!/usr/bin/env python3
"""#239 — benchmark the Context Pack against current dispatch behaviour.

Builds a small task benchmark from *real completed issues* in this repo and
measures whether a pack actually retrieves the artifacts a task needed.

⛔The point is a measurable baseline, not a favourable demo. The lexical mode
  ships first precisely so a later semantic mode has something to beat; if the
  numbers below do not improve on `current`, that is the finding.

Usage:
    python scripts/benchmark_context_pack.py --repo owner/name [--limit 12]
    python scripts/benchmark_context_pack.py --offline   # no GitHub calls

Metrics per mode:
    required_recall   fraction of required artifacts the pack retrieved
    irrelevant_ratio  tokens spent on items outside the required set
    duplicate_ratio   tokens repeated across items
    stale_conflict    packs carrying a stale or conflicting artifact
    p50 / p95 ms      pack build latency
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_crew.context_pack import (  # noqa: E402
    MODE_LEXICAL,
    TYPE_AC,
    TYPE_ISSUE,
    IssueProvider,
    LexicalRepoProvider,
    RetrievalQuery,
    estimate_tokens,
    keywords_from,
    plan_pack,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def gh_closed_issues(repo: str, limit: int) -> list:
    try:
        r = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "closed",
             "--limit", str(limit), "--json", "number,title,body"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout or "[]")
    except Exception:
        return []


def required_artifacts(issue: dict) -> set:
    """What this task genuinely needed, derived from the issue itself.

    Conservative on purpose: only files the issue *names* count as required.
    Inferring more would let the benchmark grade its own homework.
    """
    import re
    body = f"{issue.get('title','')}\n{issue.get('body','')}"
    paths = set(re.findall(r"\b((?:src|tests|scripts|docs)/[\w./-]+\.\w+)", body))
    return {p for p in paths if os.path.exists(os.path.join(REPO_ROOT, p))}


def current_behaviour_tokens(issue: dict) -> int:
    """Today's dispatch: the whole issue body goes in, unsegmented."""
    return estimate_tokens(f"{issue.get('title','')}\n{issue.get('body','')}")


def run_mode(issue: dict, mode: str) -> dict:
    required = required_artifacts(issue)
    started = time.time()
    if mode == "current":
        # No retrieval at all — the honest description of today's behaviour.
        return {
            "tokens": current_behaviour_tokens(issue),
            "retrieved": set(),
            "required": required,
            "recall": 0.0 if required else 1.0,
            "irrelevant_ratio": 0.0,
            "duplicate_ratio": 0.0,
            "stale_or_conflict": False,
            "ms": (time.time() - started) * 1000.0,
            "has_ac": bool(IssueProvider.extract_ac(issue.get("body", ""))),
        }
    q = RetrievalQuery(
        task_id=f"bench-{issue['number']}", task_type="implement",
        role="implementer", repo="", repo_path=REPO_ROOT,
        issue_number=issue["number"], issue_title=issue.get("title", ""),
        issue_body=issue.get("body", ""),
        keywords=keywords_from(issue.get("title", ""), issue.get("body", "")),
    )
    pack = plan_pack(q, [IssueProvider(), LexicalRepoProvider()], mode=mode)
    retrieved = {a.uri for a in pack.items}
    hit = required & retrieved
    body_tokens = sum(a.est_tokens for a in pack.items
                      if a.artifact_type in (TYPE_ISSUE, TYPE_AC))
    other = [a for a in pack.items if a.artifact_type not in (TYPE_ISSUE, TYPE_AC)]
    irrelevant = sum(a.est_tokens for a in other if a.uri not in required)
    seen, dup = set(), 0
    for a in pack.items:
        key = (a.excerpt or "")[:200]
        if key and key in seen:
            dup += a.est_tokens
        seen.add(key)
    total = max(1, pack.total_tokens)
    return {
        "tokens": pack.total_tokens,
        "retrieved": retrieved,
        "required": required,
        "recall": (len(hit) / len(required)) if required else 1.0,
        "irrelevant_ratio": irrelevant / total,
        "duplicate_ratio": dup / total,
        "stale_or_conflict": bool(pack.stale_count or pack.conflicts),
        "ms": pack.latency_ms,
        "has_ac": any(a.artifact_type == TYPE_AC for a in pack.items),
        "_body_tokens": body_tokens,
    }


def summarise(rows: list) -> dict:
    if not rows:
        return {}
    lat = sorted(r["ms"] for r in rows)
    graded = [r for r in rows if r["required"]]
    return {
        "n": len(rows),
        "n_with_required_artifacts": len(graded),
        "required_recall": round(
            statistics.mean([r["recall"] for r in graded]), 3) if graded else None,
        "ac_present_rate": round(
            sum(1 for r in rows if r["has_ac"]) / len(rows), 3),
        "mean_tokens": round(statistics.mean([r["tokens"] for r in rows]), 1),
        "irrelevant_ratio": round(
            statistics.mean([r["irrelevant_ratio"] for r in rows]), 3),
        "duplicate_ratio": round(
            statistics.mean([r["duplicate_ratio"] for r in rows]), 3),
        "stale_conflict_rate": round(
            sum(1 for r in rows if r["stale_or_conflict"]) / len(rows), 3),
        "p50_ms": round(lat[len(lat) // 2], 1),
        "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
    }


OFFLINE_SAMPLE = [
    {"number": 224, "title": "add continuous GitHub issue ingestion/claim loop",
     "body": "src/agent_crew/watch.py needs a claim ledger.\n\n"
             "## Acceptance criteria\n- [ ] claims are duplicate-safe\n"},
    {"number": 231, "title": "pane-idle watchdog false-positives",
     "body": "src/agent_crew/cli.py idle detection is a flat 300s.\n\n"
             "## Acceptance criteria\n- [ ] quiet-but-alive is not failed\n"},
    {"number": 236, "title": "bound agy tester context growth",
     "body": "src/agent_crew/server.py caps the wrong store.\n\n"
             "## Acceptance criteria\n- [ ] context size is bounded\n"},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="truhojunbot-tech/agent_crew")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    issues = OFFLINE_SAMPLE if args.offline else gh_closed_issues(args.repo, args.limit)
    if not issues:
        print("no issues available (try --offline)", file=sys.stderr)
        return 1

    modes = ["current", MODE_LEXICAL]
    report = {m: summarise([run_mode(i, m) for i in issues]) for m in modes}
    report["_note"] = (
        "semantic/hybrid not implemented — the provider contract admits it "
        "without a dispatcher change, but no embedding backend ships, so no "
        "semantic row is reported rather than a fabricated one"
    )
    report["_sample"] = f"{len(issues)} issues from {'offline sample' if args.offline else args.repo}"

    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"\nContext Pack benchmark — {report['_sample']}\n")
    cols = ["required_recall", "ac_present_rate", "mean_tokens",
            "irrelevant_ratio", "duplicate_ratio", "stale_conflict_rate",
            "p50_ms", "p95_ms"]
    print(f"{'mode':10s} " + " ".join(f"{c:>18s}" for c in cols))
    for m in modes:
        r = report[m]
        print(f"{m:10s} " + " ".join(f"{str(r.get(c)):>18s}" for c in cols))
    print(f"\nnote: {report['_note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
