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


def get_pr_url(repo: Optional[str], pr_number: str) -> str:
    """Format a PR URL from repo and PR number."""
    if not repo:
        repo = get_repo() or "owner/repo"
    return f"https://github.com/{repo}/pull/{pr_number}"
