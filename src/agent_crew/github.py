"""GitHub integration for crew run workflow."""

import json
import logging
import re
import subprocess
from typing import Optional


logger = logging.getLogger(__name__)

_MAX_GH_STDERR_LOG_CHARS = 300


def _extract_repo_from_url(url: str) -> Optional[str]:
    """Extract `owner/repo` from a GitHub remote URL (https or ssh)."""
    if not url:
        return None
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com/" in url:
        return url.split("github.com/", 1)[1] or None
    if "github.com:" in url:
        return url.split("github.com:", 1)[1] or None
    return None


def _log_gh_failure(op: str, repo: Optional[str], pr_number: Optional[int], result) -> None:
    """Safe, bounded failure record — never logs credentials/tokens (gh's own
    subprocess stderr for a resolution/API error contains neither)."""
    stderr = (getattr(result, "stderr", "") or "").strip().replace("\n", " ")
    stderr = stderr[:_MAX_GH_STDERR_LOG_CHARS]
    logger.warning(
        f"github.{op}: gh failed repo={repo!r} pr={pr_number!r} "
        f"exit_code={getattr(result, 'returncode', None)} stderr={stderr!r}"
    )


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


def get_repo(cwd: Optional[str] = None) -> Optional[str]:
    """Auto-detect repo from git remote origin, inside `cwd`.

    ⛔Pass `cwd` whenever the caller knows which checkout it means. Without it
      this reads the SERVER process's working directory, which is the instance
      directory — and that is not merely empty, it is a DIFFERENT repository:
      from a different instance checkout this returns that checkout's repo.
      A caller that then asks about a PR number is
      asking the wrong repo, which answers nothing, or worse answers about an
      unrelated PR of the same number (review of PR #255).
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
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

    # ⛔Only the two contract verdicts get a label. Anything else is a malformed
    #   review result and must not be published as if the reviewer had decided.
    if verdict == "approve":
        verdict_label = "✅ approve"
    elif verdict == "request_changes":
        verdict_label = "🔄 request_changes"
    else:
        return False
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
        if result.returncode != 0:
            _log_gh_failure("post_review_comment", repo, pr_number, result)
        return result.returncode == 0
    except Exception:
        return False


def pr_state(pr_number: int, repo: Optional[str] = None,
             timeout: float = 20.0, cwd: Optional[str] = None) -> str:
    """``"open" | "merged" | "closed" | "unknown"`` for a PR (#250).

    ``unknown`` is a real answer, not a failure to report: `gh` missing, no
    repo, a network blip or a malformed response all mean *we could not
    establish the PR's state*, which callers must not treat as "open".
    """
    if not pr_number or not check_gh_installed():
        return "unknown"
    if not repo:
        repo = get_repo(cwd=cwd)
    if not repo:
        return "unknown"
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo,
             "--json", "state,mergedAt,closedAt"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return "unknown"
        data = json.loads(r.stdout or "{}")
    except Exception:
        return "unknown"
    if data.get("mergedAt"):
        return "merged"
    state = (data.get("state") or "").upper()
    if state == "OPEN":
        return "open"
    if state in ("MERGED", "CLOSED"):
        return "merged" if state == "MERGED" else "closed"
    return "unknown"


def pr_has_comment_containing(pr_number: int, needle: str,
                              repo: Optional[str] = None,
                              timeout: float = 20.0) -> Optional[bool]:
    """Does this PR already carry a comment containing `needle`?

    ``None`` when it cannot be determined — the caller decides what to do with
    that, and must not read it as "no".
    """
    if not pr_number or not needle or not check_gh_installed():
        return None
    if not repo:
        repo = get_repo()
    if not repo:
        return None
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "comments"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            _log_gh_failure("pr_has_comment_containing", repo, pr_number, r)
            return None
        comments = (json.loads(r.stdout or "{}") or {}).get("comments") or []
    except Exception:
        return None
    return any(needle in (c.get("body") or "") for c in comments)


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
                timeout: float = 20.0, cwd: Optional[str] = None) -> str:
    """The commit a PR currently points at, or ``""`` when it cannot be read.

    Read the PR head from the REST API; this gh build rejects headRefOid.

    `repo` should be supplied by the caller as a slug or GitHub remote URL.
    The `cwd` fallback exists for callers that know a checkout but not its
    slug; falling through to neither means asking whatever repository the
    process happens to be standing in.
    """
    if not pr_number or not check_gh_installed():
        return ""
    if not repo:
        repo = get_repo(cwd=cwd)
    if not repo:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        repo = _extract_repo_from_url(repo) or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return ""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".head.sha"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return ""
        sha = (r.stdout or "").strip().lower()
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""
    except Exception:
        return ""


def branch_head_sha(branch: str, repo: str, timeout: float = 5.0) -> str:
    """Return a repository branch's current commit, or ``""`` if unknown."""
    if (not branch or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or branch.startswith("-") or "\n" in branch):
        return ""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", f"https://github.com/{repo}.git",
             f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=timeout,
        )
        sha = (result.stdout or "").split("\t", 1)[0].strip().lower()
        return sha if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else ""
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


def pr_number_for_branch(branch: str, repo: Optional[str] = None,
                          timeout: float = 15.0) -> Optional[int]:
    """Like `branch_has_pr`, but returns the PR number instead of a bool.

    #496: a `claude_cloud` dispatch has no local callback to this
    dispatcher, so a PR is discovered the same way `branch_has_pr` already
    does — by branch name — and the number is what the review cascade
    (`pipeline.auto_enqueue_review`) actually needs. Unlike `branch_has_pr`,
    this fails CLOSED (returns ``None``) on any error: "unknown" must never
    be read as "PR exists", the way it can be for a skip-a-futile-retry
    decision.
    """
    if not branch or not check_gh_installed():
        return None
    if not repo:
        repo = get_repo()
    if not repo:
        return None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch,
             "--state", "all", "--json", "number", "--limit", "1"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return None
        rows = json.loads(result.stdout or "[]")
        if not rows:
            return None
        number = rows[0].get("number")
        return int(number) if isinstance(number, int) else None
    except Exception:
        return None


def branch_head_commit_message(branch: str, repo: Optional[str] = None,
                                timeout: float = 15.0) -> Optional[str]:
    """The HEAD commit message on a remote branch, via `gh api` (no local
    fetch needed).

    #496: a cloud session's terminal outcome (ALREADY_FIXED, BLOCKED_FOR_CLOUD,
    NEEDS_DECISION, FAILED) does not require a PR, so PR discovery alone
    cannot observe it. This reads the same signal the issue's own contract
    asks a cloud session to leave behind (a final outcome line), from a
    branch that may exist with no open PR. Returns ``None`` — never a
    guess — on any error, missing branch, or missing `gh` installation.
    """
    if not branch or not check_gh_installed():
        return None
    if not repo:
        repo = get_repo()
    if not repo:
        return None
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{branch}", "--jq", ".commit.message"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return None
        message = (result.stdout or "").strip()
        return message or None
    except Exception:
        return None


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


def independent_review_succeeded(pr_number: int, repo: str) -> bool:
    """Require a successful independent-review commit status on the PR head.

    A missing status, failed API call, or changed head is not merge authority.
    GitHub's combined-status endpoint lists the latest state for each context.
    """
    if not repo or not check_gh_installed():
        return False
    try:
        sha = pr_head_sha(pr_number, repo=repo)
        if not sha:
            return False
        status = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{sha}/status"],
            capture_output=True, text=True, timeout=20)
        if status.returncode != 0:
            return False
        statuses = json.loads(status.stdout or "{}").get("statuses", [])
        return any(s.get("context") == "crew/independent-review"
                   and s.get("state") == "success" for s in statuses)
    except Exception:
        return False


def independent_review_for_head(queue, pr_number: int, repo: str,
                                review_task_id: str = "") -> tuple[str, str, str, str]:
    """Return (head SHA, reviewer agent, review task id, reason) for the latest independent approval.

    Empty SHA means there is no authority to publish a success status or merge.
    Attribution is dispatch evidence, not a provider name inferred from prose.
    """
    head = pr_head_sha(pr_number, repo=repo)
    if not head:
        return "", "", "", "PR head unavailable"
    latest = queue.latest_completed_review_for_pr(pr_number)
    if not latest:
        return "", "", "", "no completed review for PR"
    if review_task_id and latest.task_id != review_task_id:
        return "", "", "", f"latest completed review is {latest.task_id}"
    if latest.verdict != "approve":
        return "", "", "", f"latest review {latest.task_id} is {latest.verdict or 'without verdict'}"
    reviewed_sha = (latest.context or {}).get("reviewed_sha")
    if not isinstance(reviewed_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_sha):
        return "", "", "", f"review {latest.task_id} lacks reviewed_sha"
    if reviewed_sha.lower() != head:
        return "", "", "", f"PR head {head[:7]} differs from reviewed SHA {reviewed_sha[:7]}"
    reviewer = queue.get_attribution(latest.task_id) or {}
    parent_id = (latest.context or {}).get("prev_task_id")
    parent = queue.get_task(parent_id) if parent_id else None
    author = queue.get_attribution(parent_id) if parent and parent.task_type == "implement" else {}
    reviewer_agent, author_agent = reviewer.get("agent"), (author or {}).get("agent")
    if not reviewer_agent or not author_agent:
        return "", "", "", "author or reviewer attribution unavailable"
    if reviewer_agent == author_agent:
        return "", "", "", f"reviewer agent equals implementer agent ({reviewer_agent})"
    return head, reviewer_agent, latest.task_id, "independent approval"


def publish_independent_review_status(repo: str, sha: str, review_task_id: str,
                                      reviewer_agent: str) -> bool:
    """Publish validated review evidence as the protected-branch status."""
    if (not repo or not re.fullmatch(r"[0-9a-fA-F]{40}", sha or "") or
            not review_task_id or not reviewer_agent or not check_gh_installed()):
        return False
    description = f"{reviewer_agent} approved ({review_task_id})"[:140]
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "POST", f"repos/{repo}/statuses/{sha}",
             "-f", "state=success", "-f", "context=crew/independent-review",
             "-f", f"description={description}"],
            capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            _log_gh_failure("publish_independent_review_status", repo, None, result)
        return result.returncode == 0
    except Exception:
        return False
