"""Runtime cross-project anomaly checks (Issue #80).

The setup-time `validate_repo_origin()` cannot catch agents that drift to a
different repo after they're running. This module polls the bot account's
recent GitHub events and flags comments posted to repos outside the expected
set, alerting via the notify helper from #79.
"""
import json
import os
import subprocess
from typing import Any, Callable, Iterable, Optional

import httpx

from agent_crew.notify import notify_telegram

COMMENT_EVENT_TYPES = (
    "IssueCommentEvent",
    "PullRequestReviewCommentEvent",
    "CommitCommentEvent",
    "PullRequestReviewEvent",
)


def _fetch_user_events(
    username: str,
    *,
    token: Optional[str] = None,
    per_page: int = 30,
) -> list[dict]:
    """Fetch recent public events for the given GitHub user.

    Returns an empty list on any error so the caller never has to handle
    network or auth failures.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/users/{username}/events"
    try:
        r = httpx.get(
            url,
            headers=headers,
            params={"per_page": per_page},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


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


def auto_detect_expected_repos(state_path: str) -> list[str]:
    """Read state.json, scan each worktree's `git remote get-url origin`,
    return a sorted list of `owner/repo` strings."""
    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception:
        return []
    repos: set[str] = set()
    worktrees = state.get("worktrees") or {}
    if not isinstance(worktrees, dict):
        return []
    for path in worktrees.values():
        if not isinstance(path, str) or not path:
            continue
        try:
            r = subprocess.run(
                ["git", "-C", path, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            continue
        if r.returncode != 0:
            continue
        repo = _extract_repo_from_url(r.stdout.strip())
        if repo:
            repos.add(repo)
    return sorted(repos)


def _build_alert_message(
    username: str,
    expected: Iterable[str],
    details: list[dict],
) -> str:
    expected_list = ", ".join(sorted(set(expected))) or "(none)"
    lines = [
        f"⚠️ agent_crew wrong-repo alert ({username})",
        f"Anomalies: {len(details)}",
        f"Expected: {expected_list}",
        "Details:",
    ]
    for d in details[:5]:
        lines.append(
            f"  - {d.get('type', '?')} on {d.get('repo', '?')}: {d.get('url') or '(no url)'}"
        )
    if len(details) > 5:
        lines.append(f"  … and {len(details) - 5} more")
    return "\n".join(lines)


def check_wrong_repo(
    expected_repos: Optional[list[str]] = None,
    *,
    username: Optional[str] = None,
    state_path: Optional[str] = None,
    fetch_events: Optional[Callable[..., list[dict]]] = None,
    notify: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    """Check recent comment events of the bot account for cross-project leakage.

    Args:
        expected_repos: Allow-list of `owner/repo`. If None, falls back to
            `auto_detect_expected_repos(state_path)`.
        username: Bot GitHub login. Defaults to `AGENT_CREW_GH_USERNAME`.
        state_path: Path to the per-project state.json (used for auto-detect).
        fetch_events: Injectable events fetcher (for tests). Defaults to the
            real GitHub API call.
        notify: Injectable notifier (for tests). Defaults to `notify_telegram`.

    Returns:
        {
            "checked": <int>,           # number of comment events inspected
            "anomalies": <int>,         # how many were outside expected_repos
            "notified": <bool>,         # whether the alert was sent
            "details": <list[dict]>,    # one entry per anomaly
            "reason": <str | None>,     # short skip reason if checked == 0
        }
    """
    resolved_username = (
        username
        or os.getenv("AGENT_CREW_GH_USERNAME")
        or os.getenv("GITHUB_BOT_USERNAME")
    )
    if not resolved_username:
        return {
            "checked": 0,
            "anomalies": 0,
            "notified": False,
            "details": [],
            "reason": "missing username",
        }

    if expected_repos is None and state_path:
        expected_repos = auto_detect_expected_repos(state_path)
    if not expected_repos:
        return {
            "checked": 0,
            "anomalies": 0,
            "notified": False,
            "details": [],
            "reason": "no expected_repos",
        }

    fetch = fetch_events or _fetch_user_events
    notifier = notify or notify_telegram
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")

    try:
        events = fetch(resolved_username, token=token)
    except Exception:
        events = []
    if not isinstance(events, list):
        events = []

    expected_set = set(expected_repos)
    details: list[dict] = []
    checked = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in COMMENT_EVENT_TYPES:
            continue
        checked += 1
        repo_info = event.get("repo") or {}
        repo_name = repo_info.get("name", "") if isinstance(repo_info, dict) else ""
        if repo_name and repo_name not in expected_set:
            payload = event.get("payload") or {}
            comment = payload.get("comment") if isinstance(payload, dict) else None
            url = ""
            if isinstance(comment, dict):
                url = comment.get("html_url") or ""
            elif isinstance(payload, dict):
                review = payload.get("review")
                if isinstance(review, dict):
                    url = review.get("html_url") or ""
            details.append(
                {
                    "type": event.get("type"),
                    "repo": repo_name,
                    "url": url,
                    "created_at": event.get("created_at"),
                }
            )

    notified = False
    if details:
        message = _build_alert_message(resolved_username, expected_set, details)
        try:
            notified = bool(notifier(message))
        except Exception:
            notified = False

    return {
        "checked": checked,
        "anomalies": len(details),
        "notified": notified,
        "details": details,
        "reason": None,
    }
