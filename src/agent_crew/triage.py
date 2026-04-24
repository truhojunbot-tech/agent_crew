import json
import re
import subprocess
import time
import uuid

from agent_crew.protocol import GateRequest, TaskRequest


def parse_issues(data: list[dict]) -> list[dict]:
    result = []
    for item in data:
        result.append({
            "number": item["number"],
            "title": item["title"],
            "labels": [label["name"] for label in item.get("labels", [])],
        })
    return result


def filter_processed(issues: list[dict]) -> list[dict]:
    return [i for i in issues if "agent_crew:done" not in i.get("labels", [])]


def build_prompt(issues: list[dict], merge_history: str) -> str | None:
    if not issues:
        return None
    lines = ["## Open Issues\n"]
    for issue in issues:
        labels = ", ".join(issue.get("labels", [])) or "none"
        lines.append(f"- #{issue['number']}: {issue['title']} (labels: {labels})")
    lines.append(f"\n## Recent Merge History\n{merge_history}")
    lines.append(
        "\n## Task\n"
        "Select the most important issue to work on next.\n"
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
        ["gh", "issue", "list", "--repo", repo, "--json", "number,title,labels"],
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
    """
    raw = fetch_issues_from_gh(repo)
    issues = parse_issues(raw)
    filtered = filter_processed(issues)
    prompt = build_prompt(filtered, merge_history)
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
