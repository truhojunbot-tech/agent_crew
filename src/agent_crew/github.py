"""GitHub integration for crew run workflow."""

import json
import subprocess
from typing import Optional


def check_gh_installed() -> bool:
    """Check if gh CLI is installed and accessible."""
    try:
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_repo() -> Optional[str]:
    """Auto-detect repo from git remote origin."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        remote = result.stdout.strip()
        # Extract owner/repo from github.com:owner/repo.git or https://github.com/owner/repo.git
        if "github.com" in remote:
            if remote.endswith(".git"):
                remote = remote[:-4]
            parts = remote.split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"
    except Exception:
        pass
    return None


def create_issue(title: str, body: str, repo: Optional[str] = None) -> Optional[str]:
    """Create a GitHub issue and return the issue number."""
    if not check_gh_installed():
        return None

    if not repo:
        repo = get_repo()
    if not repo:
        return None

    try:
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Extract issue number from output (https://github.com/owner/repo/issues/123)
            output = result.stdout.strip()
            if "/issues/" in output:
                return output.split("/issues/")[-1]
    except Exception:
        pass
    return None


def create_pr(
    title: str,
    body: str,
    branch: str,
    base: str = "main",
    repo: Optional[str] = None,
) -> Optional[str]:
    """Create a GitHub PR and return the PR number."""
    if not check_gh_installed():
        return None

    if not repo:
        repo = get_repo()
    if not repo:
        return None

    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Extract PR number from output (https://github.com/owner/repo/pull/123)
            output = result.stdout.strip()
            if "/pull/" in output:
                return output.split("/pull/")[-1]
    except Exception:
        pass
    return None


def post_review_comment(
    pr_number: int,
    verdict: Optional[str],
    summary: str,
    findings: list,
    task_id: str,
    reviewer: str = "agent",
    repo: Optional[str] = None,
) -> bool:
    """Post a review result as a PR comment via gh CLI. Returns True on success."""
    if not check_gh_installed():
        return False
    if not repo:
        repo = get_repo()
    if not repo:
        return False

    verdict_label = "✅ approve" if verdict == "approve" else "🔄 request_changes"
    lines = [f"[agent_crew review] verdict: {verdict_label}", ""]
    if findings:
        lines.append("**Findings:**")
        for f in findings:
            lines.append(f"- {f}")
        lines.append("")
    if summary:
        lines.append(f"**Summary:** {summary}")
        lines.append("")
    lines.append(f"> reviewer: {reviewer} | task: {task_id}")
    body = "\n".join(lines)

    try:
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def post_pr_comment(pr_number: int, body: str, repo: Optional[str] = None) -> bool:
    """Post an arbitrary comment on a PR. Returns True on success.

    `post_review_comment` renders a review verdict; this is the plain channel
    for the automation itself to speak — e.g. saying that the automated fix
    budget is spent and the PR is now waiting on a human (#244).
    """
    if not check_gh_installed():
        return False
    if not repo:
        repo = get_repo()
    if not repo:
        return False
    try:
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def pr_head_sha(pr_number: int, repo: Optional[str] = None,
                timeout: float = 20.0) -> str:
    """The commit a PR currently points at, or ``""`` when it cannot be read.

    Uses the last entry of `--json commits`: this `gh` build has no
    `headRefOid` field, and asking for one fails the whole call.
    """
    if not pr_number or not check_gh_installed():
        return ""
    if not repo:
        repo = get_repo()
    if not repo:
        return ""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "commits"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return ""
        commits = (json.loads(r.stdout or "{}") or {}).get("commits") or []
        return (commits[-1].get("oid") or "") if commits else ""
    except Exception:
        return ""


def post_discussion_comment(
    issue_number: int,
    topic: str,
    synthesis: str,
    repo: Optional[str] = None,
) -> bool:
    """Post a `crew discuss` synthesis as a comment on a GitHub issue (#219).
    Returns True on success.

    `crew discuss` only ever wrote the synthesis to a local file
    (--output, default synthesis.md) — a caller that isn't sitting at the
    terminal that ran it (a bot invoking it as a subprocess, a scheduled
    job) had no way to actually receive the result short of separately
    reading that file. --post-to <issue> gives it an active delivery path,
    the same way review/test results already post to PRs.
    """
    if not check_gh_installed():
        return False
    if not repo:
        repo = get_repo()
    if not repo:
        return False

    body = f"[agent_crew discuss] {topic}\n\n{synthesis}"

    try:
        result = subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def branch_has_pr(branch: str, repo: Optional[str] = None) -> bool:
    """Return True iff `branch` has an open OR closed GitHub PR (#216).

    Used to decide whether a review retry is even winnable before spending
    an agent invocation on it — `gh pr list --head <branch>` is a cheap,
    deterministic check the dispatcher can run itself, instead of
    dispatching a whole review task only for the agent to independently
    rediscover "no PR exists for this branch" for the second or third time
    in a row. Fails open (returns True — "assume a retry might help") on
    any gh/network error, since the goal here is only to skip a provably
    futile retry, never to block a legitimate one on an unrelated hiccup.
    """
    if not branch or not check_gh_installed():
        return True
    if not repo:
        repo = get_repo()
    if not repo:
        return True
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch,
             "--state", "all", "--json", "number", "--limit", "1"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return True
        return bool(json.loads(result.stdout or "[]"))
    except Exception:
        return True


def get_pr_url(repo: Optional[str], pr_number: str) -> str:
    """Format a PR URL from repo and PR number."""
    if not repo:
        repo = get_repo() or "owner/repo"
    return f"https://github.com/{repo}/pull/{pr_number}"


def merge_pr(
    pr_number: int,
    merge_method: str = "squash",
    repo: Optional[str] = None,
) -> bool:
    """Merge a GitHub PR via gh CLI. Returns True on success.

    merge_method must be one of: squash, merge, rebase.
    Failures are swallowed and return False so callers never crash the pipeline (#171).
    """
    if not check_gh_installed():
        return False
    if not repo:
        repo = get_repo()
    if not repo:
        return False
    try:
        result = subprocess.run(
            [
                "gh", "pr", "merge", str(pr_number),
                f"--{merge_method}",
                "--repo", repo,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False
