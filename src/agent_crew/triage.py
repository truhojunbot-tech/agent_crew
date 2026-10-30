import json
import logging
import re
import subprocess
import time
import uuid

from agent_crew.protocol import GateRequest, TaskRequest

logger = logging.getLogger(__name__)
_PHASE_LABEL_RE = re.compile(r"phase:(\d+)", re.IGNORECASE)

# Severity tiers for deterministic pre-sort (Issue #131).
# Lower score = higher priority. LLM only sees candidates of the same tier,
# so dependency-aware logic still applies within each tier.
_SEVERITY_TIER: list[set[str]] = [
    {"blocker", "blocked", "critical", "p0"},
    {"p1", "high"},
]


def _severity_score(labels: list[str]) -> int:
    label_set = {l.lower() for l in labels}
    for score, tier in enumerate(_SEVERITY_TIER):
        if tier & label_set:
            return score
    return len(_SEVERITY_TIER)


def parse_dependencies(issue: dict) -> dict:
    """Read GitHub's declared parent/blocker links and phase label."""
    parents = []
    for parent in [issue.get("parent"), *(issue.get("trackedInIssues") or []),
                   *(issue.get("blockedBy") or [])]:
        number = parent.get("number") if isinstance(parent, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            if number not in parents:
                parents.append(number)
    phase = None
    for label in issue.get("labels") or []:
        name = label.get("name", "") if isinstance(label, dict) else label
        match = _PHASE_LABEL_RE.fullmatch(name) if isinstance(name, str) else None
        if match:
            phase = int(match.group(1))
            break
    return {"parents": parents, "phase": phase}


def parse_issues(data: list[dict]) -> list[dict]:
    result = []
    for item in data:
        deps = parse_dependencies(item)
        links = [item.get("parent"), *(item.get("trackedInIssues") or []),
                 *(item.get("blockedBy") or [])]
        result.append({
            "number": item["number"],
            "title": item["title"],
            "labels": [label["name"] for label in item.get("labels", [])],
            "body": item.get("body", "") or "",
            "parents": deps["parents"],
            "open_parents": [link["number"] for link in links
                             if isinstance(link, dict) and link.get("state") == "OPEN"
                             and isinstance(link.get("number"), int)],
            "phase": deps["phase"],
            "dependency_unknown": bool(item.get("dependency_unknown")),
        })
    return result


def filter_processed(issues: list[dict]) -> list[dict]:
    return [i for i in issues if "agent_crew:done" not in i.get("labels", [])]


def filter_blocked(issues: list[dict], closed_issue_numbers: set[int]) -> list[dict]:
    """Drop issues whose declared parents are still open.

    `closed_issue_numbers` is the set of issue numbers that are already
    closed (i.e. completed). An issue with parent #54 is unblocked once #54
    is in that set.
    """
    open_set = {i["number"] for i in issues}
    eligible = []
    for issue in issues:
        if issue.get("dependency_unknown"):
            continue
        parents = issue.get("parents") or []
        blocking = [
            p for p in parents
            if (p in open_set or p in issue.get("open_parents", []))
            and p not in closed_issue_numbers
        ]
        if not blocking:
            eligible.append(issue)
    return eligible


def fetch_closed_issue_numbers(repo: str, limit: int = 200) -> set[int]:
    """Return the set of recently-closed issue numbers for a repo."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list", "--repo", repo,
                "--state", "closed",
                "--limit", str(limit),
                "--json", "number",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return set()
    try:
        data = json.loads(result.stdout)
    except Exception:
        return set()
    return {item["number"] for item in data if "number" in item}


def fetch_recent_merge_history(repo: str, limit: int = 10) -> str:
    """Format a one-line summary of the most recently merged PRs.

    Returned text becomes the `merge_history` block in the triage prompt
    (replacing the operator-supplied default of "none"). We never raise —
    on any failure we return "none" and the prompt degrades gracefully.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list", "--repo", repo,
                "--state", "merged",
                "--limit", str(limit),
                "--json", "number,title",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
    except Exception:
        return "none"
    if not data:
        return "none"
    lines = [f"- #{item['number']}: {item['title']}" for item in data]
    return "\n".join(lines)


def build_prompt(issues: list[dict], merge_history: str) -> str | None:
    if not issues:
        return None
    lines = ["## Open Issues\n"]
    for issue in issues:
        labels = ", ".join(issue.get("labels", [])) or "none"
        meta_parts = [f"labels: {labels}"]
        if issue.get("phase") is not None:
            meta_parts.append(f"phase: {issue['phase']}")
        if issue.get("parents"):
            parents_fmt = ", ".join(f"#{p}" for p in issue["parents"])
            meta_parts.append(f"parents: {parents_fmt}")
        lines.append(
            f"- #{issue['number']}: {issue['title']} ({'; '.join(meta_parts)})"
        )
    lines.append(f"\n## Recent Merge History\n{merge_history}")
    lines.append(
        "\n## Task\n"
        "Pick the next issue to work on. Prefer issues whose dependencies "
        "are already satisfied (lowest phase number first; an issue's "
        "parents have either been merged or are absent from the open list "
        "above). Among the eligible candidates, choose the one that "
        "unblocks the most downstream work — earlier-phase issues over "
        "later-phase ones, parents over leaves, foundational modules over "
        "consumers.\n"
        "Respond with:\n"
        "ISSUE: <number>\n"
        "DESCRIPTION: <one-line task description>"
    )
    return "\n".join(lines)


def parse_response(text: str) -> dict | None:
    issue_match = re.search(r"^ISSUE:\s*(\d+)\s*$", text, re.MULTILINE)
    desc_match = re.search(r"^DESCRIPTION:\s*(.+)\s*$", text, re.MULTILINE)
    if not issue_match or not desc_match:
        return None
    return {
        "issue": int(issue_match.group(1)),
        "description": desc_match.group(1).strip(),
    }


def get_project_git_origin(project_path: str) -> str | None:
    """Return the 'origin' remote URL for the git repo at project_path, or None."""
    result = subprocess.run(
        ["git", "-C", project_path, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def validate_repo_origin(repo: str, project_path: str) -> tuple[bool, str]:
    """Check that --repo matches the git remote origin of project_path.

    repo: owner/name form (e.g. 'org/myrepo')
    Returns (True, "") on match, (False, error_message) on mismatch.
    """
    origin = get_project_git_origin(project_path)
    if origin is None:
        return True, ""  # no remote configured — can't validate, allow through
    # Normalise: strip trailing .git, extract owner/name from URL
    normalised = origin.rstrip("/")
    if normalised.endswith(".git"):
        normalised = normalised[:-4]
    # Both "https://github.com/org/repo" and "git@github.com:org/repo" forms
    # end with "/org/repo" or ":org/repo" — take the last two path components.
    parts = normalised.replace(":", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        origin_slug = f"{parts[-2]}/{parts[-1]}"
    else:
        origin_slug = normalised
    if repo == origin_slug:
        return True, ""
    return False, (
        f"repo mismatch: --repo {repo} but project origin is {origin_slug} ({origin})"
    )


def fetch_issues_from_gh(repo: str, *, timeout: float = 30.0) -> list[dict]:
    base = ["gh", "issue", "list", "--repo", repo, "--state", "open",
            "--limit", "200", "--json"]
    result = subprocess.run(base + ["id,number,title,labels,body,url"],
                            capture_output=True, text=True, check=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("issue discovery failed")
    rows = json.loads(result.stdout or "[]")
    if not rows:
        return rows

    # The installed gh may not expose parent/sub-issue fields through --json.
    # GraphQL's Issue.parent, trackedInIssues, and blockedBy are declared links; batching
    # node IDs avoids one API call per issue on every watcher cycle.
    by_id = {row.get("id"): row for row in rows if isinstance(row.get("id"), str)}
    for row in rows:
        row["dependency_unknown"] = True
    if not by_id:
        return rows
    ids = list(by_id)
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        query = ("query { nodes(ids:" + json.dumps(chunk) + ") { ... on Issue { "
                 "id number parent { number state } trackedInIssues(first:100) { "
                 "nodes { number state } pageInfo { hasNextPage } } "
                 "blockedBy(first:100) { nodes { number state } "
                 "pageInfo { hasNextPage } } } } }")
        try:
            lookup = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                                    capture_output=True, text=True, check=True, timeout=timeout)
            payload = json.loads(lookup.stdout or "{}")
            nodes = (payload.get("data") or {}).get("nodes")
            if lookup.returncode or payload.get("errors") or not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, dict) or node.get("id") not in by_id:
                    continue
                tracked = node.get("trackedInIssues")
                blocked = node.get("blockedBy")
                if any(not isinstance(links, dict)
                       or not isinstance(links.get("nodes"), list)
                       or (links.get("pageInfo") or {}).get("hasNextPage") is not False
                       for links in (tracked, blocked)):
                    continue
                row = by_id[node["id"]]
                row["parent"] = node.get("parent")
                row["trackedInIssues"] = tracked["nodes"]
                row["blockedBy"] = blocked["nodes"]
                row["dependency_unknown"] = False
        except Exception:
            continue
    return rows


def run(
    queue,
    repo: str,
    agent_fn,
    branch: str = "main",
    merge_history: str = "none",
) -> dict | None:
    """Full triage pipeline: fetch issues → filter → prompt → agent → gate.

    Returns {"gate_id": str, "parsed": dict, "branch": str} or None if no issues.
    agent_fn(prompt: str) -> str: callable that returns the triage agent's response text.

    Dependency-aware selection (Issue #82):
    - GitHub parent/blocker links and phase labels provide ordering.
    - Issues whose declared parent is still open are filtered out — the
      triage agent only sees unblocked candidates.
    - When the caller passes the default merge_history ("none"), we
      auto-fetch the project's recent merged PRs so the agent sees what's
      already done instead of operating in a vacuum.
    """
    raw = fetch_issues_from_gh(repo)
    issues = parse_issues(raw)
    for issue in issues:
        if issue["dependency_unknown"]:
            logger.warning("triage: dependency_unknown for issue #%s", issue["number"])
    filtered = filter_processed(issues)
    closed = fetch_closed_issue_numbers(repo)
    eligible = filter_blocked(filtered, closed)
    # Pre-sort by severity so the LLM only sees same-tier candidates (#131).
    eligible.sort(key=lambda i: (_severity_score(i["labels"]),
                                 i["phase"] if i.get("phase") is not None else 999))
    top_score = _severity_score(eligible[0]["labels"]) if eligible else 0
    top_tier = [i for i in eligible if _severity_score(i["labels"]) == top_score]
    if merge_history == "none":
        merge_history = fetch_recent_merge_history(repo)
    prompt = build_prompt(top_tier, merge_history)
    if prompt is None:
        return None
    response_text = agent_fn(prompt)
    parsed = parse_response(response_text)
    if parsed is None:
        return None
    gate = GateRequest(
        id=f"gate-triage-{uuid.uuid4().hex[:8]}",
        type="approval",
        message=f"Triage selected issue #{parsed['issue']}: {parsed['description']}",
    )
    gate_id = queue.create_gate(gate)
    return {"gate_id": gate_id, "parsed": parsed, "branch": branch}


def enqueue_task(queue, triage_result: dict, *, project: str = "") -> str:
    """Enqueue an implement task from an approved triage result.

    ``project`` names the project the task is admitted under; a triage result
    carries an issue and a branch but no project, so it otherwise comes from
    the queue's own identity (``<base>/<project>/tasks.db``, §7.1 step 2, s4j).
    Before s4j this adapter named none and admission raised out of it.
    """
    parsed = triage_result["parsed"]
    branch = triage_result.get("branch", "main")
    req = TaskRequest(
        task_id=f"impl-triage-{uuid.uuid4().hex[:8]}",
        task_type="implement",
        description=parsed["description"],
        branch=branch,
        context={"issue": parsed["issue"], **({"risk_tier": parsed["risk_tier"]}
                   if "risk_tier" in parsed else {})},
        project=(str(project or "").strip()
                 or str(triage_result.get("project") or "").strip()
                 or getattr(queue, "project_identity", "") or ""),
    )
    return queue.enqueue(req, ingress="cron.triage")


def check_gate_timeout(queue, timeout_seconds: float) -> list[str]:
    """Auto-reject pending gates older than timeout_seconds. Returns rejected IDs."""
    now = time.time()
    rejected = []
    for gate in queue.list_gates(status="pending"):
        if now - gate.created_at >= timeout_seconds:
            queue.resolve_gate(gate.id, approved=False)
            rejected.append(gate.id)
    return rejected
