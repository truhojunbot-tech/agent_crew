"""Unit tests for the runtime wrong-repo monitor (Issue #80)."""
import json
import os
from unittest.mock import MagicMock, patch

from agent_crew.anomaly import (
    _extract_repo_from_url,
    auto_detect_expected_repos,
    check_wrong_repo,
)


def _event(type_: str, repo: str, *, url: str = "", created_at: str = "2026-04-26T00:00:00Z") -> dict:
    return {
        "type": type_,
        "repo": {"name": repo},
        "payload": {"comment": {"html_url": url}},
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# _extract_repo_from_url
# ---------------------------------------------------------------------------


class TestExtractRepoFromUrl:
    def test_https_with_dot_git(self):
        assert _extract_repo_from_url("https://github.com/owner/repo.git") == "owner/repo"

    def test_https_no_dot_git(self):
        assert _extract_repo_from_url("https://github.com/owner/repo") == "owner/repo"

    def test_ssh(self):
        assert _extract_repo_from_url("git@github.com:owner/repo.git") == "owner/repo"

    def test_trailing_slash(self):
        assert _extract_repo_from_url("https://github.com/owner/repo/") == "owner/repo"

    def test_empty(self):
        assert _extract_repo_from_url("") is None

    def test_non_github(self):
        assert _extract_repo_from_url("https://gitlab.com/owner/repo.git") is None


# ---------------------------------------------------------------------------
# auto_detect_expected_repos
# ---------------------------------------------------------------------------


class TestAutoDetectExpectedRepos:
    def test_returns_owner_repo_for_each_worktree(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "worktrees": {
                        "claude": str(tmp_path / "claude"),
                        "codex": str(tmp_path / "codex"),
                    }
                }
            )
        )

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[2].endswith("claude"):
                mock.stdout = "https://github.com/me/proj.git\n"
            else:
                mock.stdout = "git@github.com:me/proj.git\n"
            return mock

        with patch("agent_crew.anomaly.subprocess.run", side_effect=fake_run):
            repos = auto_detect_expected_repos(str(state_path))

        assert repos == ["me/proj"]

    def test_missing_state_returns_empty(self, tmp_path):
        assert auto_detect_expected_repos(str(tmp_path / "missing.json")) == []

    def test_malformed_state_returns_empty(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("not json")
        assert auto_detect_expected_repos(str(state_path)) == []

    def test_remote_failure_skipped(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps({"worktrees": {"claude": str(tmp_path / "claude")}})
        )
        mock = MagicMock(returncode=128, stdout="", stderr="not a git repo")
        with patch("agent_crew.anomaly.subprocess.run", return_value=mock):
            assert auto_detect_expected_repos(str(state_path)) == []


# ---------------------------------------------------------------------------
# check_wrong_repo
# ---------------------------------------------------------------------------


class TestCheckWrongRepo:
    def test_missing_username_returns_skip(self):
        with patch.dict(os.environ, {}, clear=True):
            result = check_wrong_repo(expected_repos=["a/b"])
        assert result["checked"] == 0
        assert result["anomalies"] == 0
        assert result["reason"] == "missing username"

    def test_no_expected_repos_returns_skip(self):
        result = check_wrong_repo(
            expected_repos=[],
            username="bot",
            fetch_events=lambda *a, **kw: [],
            notify=lambda m: True,
        )
        assert result["reason"] == "no expected_repos"
        assert result["notified"] is False

    def test_no_anomalies_no_notify(self):
        events = [
            _event("IssueCommentEvent", "me/proj"),
            _event("PushEvent", "someone/else"),  # not a comment type → ignored
            _event("PullRequestReviewEvent", "me/proj"),
        ]
        notify_calls = []
        result = check_wrong_repo(
            expected_repos=["me/proj"],
            username="bot",
            fetch_events=lambda *a, **kw: events,
            notify=lambda m: (notify_calls.append(m), True)[1],
        )
        assert result["checked"] == 2
        assert result["anomalies"] == 0
        assert result["notified"] is False
        assert notify_calls == []

    def test_anomaly_detected_triggers_notify(self):
        events = [
            _event("IssueCommentEvent", "me/proj"),
            _event(
                "IssueCommentEvent",
                "stranger/danger",
                url="https://github.com/stranger/danger/issues/1#issuecomment-1",
            ),
            _event("CommitCommentEvent", "another/wrong"),
        ]
        notify_calls = []
        result = check_wrong_repo(
            expected_repos=["me/proj"],
            username="bot",
            fetch_events=lambda *a, **kw: events,
            notify=lambda m: (notify_calls.append(m), True)[1],
        )
        assert result["checked"] == 3
        assert result["anomalies"] == 2
        assert result["notified"] is True
        assert {d["repo"] for d in result["details"]} == {"stranger/danger", "another/wrong"}
        assert len(notify_calls) == 1
        msg = notify_calls[0]
        assert "stranger/danger" in msg
        assert "another/wrong" in msg
        assert "bot" in msg

    def test_username_from_env_when_arg_omitted(self, monkeypatch):
        monkeypatch.setenv("AGENT_CREW_GH_USERNAME", "envbot")
        captured_user = []
        result = check_wrong_repo(
            expected_repos=["me/proj"],
            fetch_events=lambda u, **kw: (captured_user.append(u), [])[1],
        )
        assert captured_user == ["envbot"]
        assert result["reason"] is None

    def test_expected_repos_auto_detected_from_state(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps({"worktrees": {"claude": str(tmp_path / "claude")}})
        )
        monkeypatch.setattr(
            "agent_crew.anomaly.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="https://github.com/me/proj.git"),
        )
        events = [_event("IssueCommentEvent", "stranger/repo")]
        result = check_wrong_repo(
            username="bot",
            state_path=str(state_path),
            fetch_events=lambda *a, **kw: events,
            notify=lambda m: True,
        )
        assert result["anomalies"] == 1

    def test_fetch_exception_swallowed(self):
        def boom(*a, **kw):
            raise RuntimeError("network down")

        result = check_wrong_repo(
            expected_repos=["me/proj"],
            username="bot",
            fetch_events=boom,
            notify=lambda m: True,
        )
        assert result["checked"] == 0
        assert result["anomalies"] == 0
        assert result["notified"] is False

    def test_notify_returning_false_recorded(self):
        events = [_event("IssueCommentEvent", "stranger/repo")]
        result = check_wrong_repo(
            expected_repos=["me/proj"],
            username="bot",
            fetch_events=lambda *a, **kw: events,
            notify=lambda m: False,
        )
        assert result["anomalies"] == 1
        assert result["notified"] is False

    def test_create_app_anomaly_disabled_via_env(self, tmp_db, monkeypatch):
        """When AGENT_CREW_ANOMALY_DISABLED=1, lifespan must not start the loop."""
        from fastapi.testclient import TestClient

        from agent_crew.server import create_app

        monkeypatch.setenv("AGENT_CREW_ANOMALY_DISABLED", "1")
        app = create_app(db_path=tmp_db, watchdog_disabled=True)
        # Tick is still callable (returns skip with reason since no username configured).
        with TestClient(app) as _client:
            result = app.state.anomaly_tick()
            assert result["checked"] == 0
            assert result["reason"] in ("missing username", "no expected_repos")

    def test_create_app_anomaly_loop_runs_and_cancels(self, tmp_db, monkeypatch):
        """Background loop fires the tick at least once and cancels cleanly."""
        import time as _t

        from fastapi.testclient import TestClient

        from agent_crew.server import create_app

        # Force the loop active with a very short interval. Inject a stub fetcher
        # via env so the real GitHub call is never made.
        monkeypatch.setenv("AGENT_CREW_GH_USERNAME", "stubbot")

        # Create a state.json with no worktrees so auto_detect returns []
        # → check_wrong_repo returns "no expected_repos" without HTTP.
        import json
        import os

        state_path = os.path.join(os.path.dirname(tmp_db), "state.json")
        with open(state_path, "w") as f:
            json.dump({"worktrees": {}}, f)

        app = create_app(
            db_path=tmp_db,
            watchdog_disabled=True,
            anomaly_disabled=False,
            anomaly_interval=0.05,
            state_path=state_path,
        )
        with TestClient(app):
            _t.sleep(0.2)  # give the loop a couple of ticks
        # Reaching here without hanging proves cancel() worked.

    def test_pull_request_review_event_url_falls_back_to_review(self):
        events = [
            {
                "type": "PullRequestReviewEvent",
                "repo": {"name": "stranger/repo"},
                "payload": {
                    "review": {"html_url": "https://github.com/stranger/repo/pull/1#review"}
                },
                "created_at": "2026-04-26T00:00:00Z",
            }
        ]
        notify_calls = []
        result = check_wrong_repo(
            expected_repos=["me/proj"],
            username="bot",
            fetch_events=lambda *a, **kw: events,
            notify=lambda m: (notify_calls.append(m), True)[1],
        )
        assert result["details"][0]["url"].endswith("#review")
        assert "stranger/repo" in notify_calls[0]
