import json
import re
import subprocess
import time
import uuid
from typing import Optional

from agent_crew.protocol import GateRequest, TaskRequest

# Dependency markers we recognise inside an issue body. All are matched
# case-insensitively against the body text.
_PARENT_PATTERNS: tuple[str, ...] = (
    r"parent\s*:\s*#(\d+)",            # "Parent: #54"
    r"depends?\s+on\s+#(\d+)",         # "depends on #54", "depend on #54"
    r"blocked\s+by\s+#(\d+)",          # "blocked by #54"
)
_PHASE_PATTERN = r"phase\s*[:\-]?\s*(\d+)"  # "Phase 2", "Phase: 2", "Phase-2"

_PARENT_RE = re.compile("|".join(_PARENT_PATTERNS), re.IGNORECASE)
_PHASE_RE = re.compile(_PHASE_PATTERN, re.IGNORECASE)


def parse_dependencies(body: Optional[str]) -> dict:
    """Extract dependency hints from an issue body.

    Returns ``{"parents": [int, ...], "phase": int | None}``. The same body
    can mention several parents (Parent + Depends on); we return them all
    deduplicated. Phase is the first numeric Phase-marker we hit.
    """
    if not body:
        return {"parents": [], "phase": None}
    parents: list[int] = []
    seen: set[int] = set()
    for match in _PARENT_RE.finditer(body):
        # Each pattern in the alternation has exactly one capture group, so
        # we walk the groups and pick the first non-None number.
        for grp in match.groups():
            if grp:
                num = int(grp)
                if num not in seen:
                    seen.add(num)
                    parents.append(num)
                break
    phase_match = _PHASE_RE.search(body)
    phase = int(phase_match.group(1)) if phase_match else None
    return {"parents": parents, "phase": phase}


def parse_issues(data: list[dict]) -> list[dict]:
    result = []
    for item in data:
        deps = parse_dependencies(item.get("body"))
        result.append({
            "number": item["number"],
            "title": item["title"],
            "labels": [label["name"] for label in item.get("labels", [])],
            "body": item.get("body", "") or "",
            "parents": deps["parents"],
            "phase": deps["phase"],
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
        parents = issue.get("parents") or []
        # An open parent that is not in our own list of remaining open issues
        # might still be considered blocking. We treat parent as resolved
        # when it is either closed or absent from the current open set.
        blocking = [
            p for p in parents
            if p in open_set and p not in closed_issue_numbers
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


def fetch_issues_from_gh(repo: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--json", "number,title,labels,body"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


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
    - Issue body is parsed for "Parent: #N", "depends on #N", "Phase N" markers.
    - Issues whose declared parent is still open are filtered out — the
      triage agent only sees unblocked candidates.
    - When the caller passes the default merge_history ("none"), we
      auto-fetch the project's recent merged PRs so the agent sees what's
      already done instead of operating in a vacuum.
    """
    raw = fetch_issues_from_gh(repo)
    issues = parse_issues(raw)
    filtered = filter_processed(issues)
    closed = fetch_closed_issue_numbers(repo)
    eligible = filter_blocked(filtered, closed)
    if merge_history == "none":
        merge_history = fetch_recent_merge_history(repo)
    prompt = build_prompt(eligible, merge_history)
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


def enqueue_task(queue, triage_result: dict) -> str:
    """Enqueue an implement task from an approved triage result."""
    parsed = triage_result["parsed"]
    branch = triage_result.get("branch", "main")
    req = TaskRequest(
        task_id=f"impl-triage-{uuid.uuid4().hex[:8]}",
        task_type="implement",
        description=parsed["description"],
        branch=branch,
        context={"issue": parsed["issue"]},
    )
    return queue.enqueue(req)


def check_gate_timeout(queue, timeout_seconds: float) -> list[str]:
    """Auto-reject pending gates older than timeout_seconds. Returns rejected IDs."""
    now = time.time()
    rejected = []
    for gate in queue.list_gates(status="pending"):
        if now - gate.created_at >= timeout_seconds:
            queue.resolve_gate(gate.id, approved=False)
            rejected.append(gate.id)
    return rejected
