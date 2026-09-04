import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


#: Every function in `agent_crew.github` that MUTATES something on GitHub.
#: Reads (`get_repo`, `pr_state`, `pr_head_sha`, `branch_has_pr`, …) are left
#: alone: they are safe, and blocking them would push tests toward mocking the
#: whole module and losing coverage of the real call shapes.
GITHUB_WRITE_FUNCTIONS = (
    "post_pr_comment",
    "post_review_comment",
    "post_discussion_comment",
    "create_pr",
    "create_issue",
    "merge_pr",
)


class GitHubWriteFromTest(AssertionError):
    """Raised when the suite tries to mutate a real GitHub object."""


@pytest.fixture
def github_writes():
    """Opt in to OBSERVING GitHub writes instead of failing on them.

    A test that exercises a code path whose side effect is a GitHub comment
    requests this fixture and gets the recorded calls. Nothing reaches the
    network either way — this only changes whether an attempt is a defect or
    the thing under test.
    """
    return []


@pytest.fixture(autouse=True)
def _no_github_writes(request, github_writes_recorder, monkeypatch):
    """⛔The default suite may not write to GitHub. Ever.

    Not hypothetical caution. `tests/unit/test_issue_250_terminal_pr_gate.py`
    used `PR = 241` — a real, merged PR — and drove the real result handler,
    which posts a review comment for any review result carrying a pr_number.
    Nothing patched that path, so **every full-suite run posted three comments
    to PR #241**: 228 of that PR's 263 comments were fixture data, 76 from each
    of three test task ids. Two older tests were doing the same to PR #42 (#263).

    Individual tests remembering to patch is precisely what failed, so the block
    is autouse and sits at the boundary. Two escapes, both explicit:

      * request the `github_writes` fixture — the call is recorded and returns
        success, for tests whose subject IS the side effect;
      * mark `@pytest.mark.live_github` — no stubbing at all, for a deliberate
        smoke test that must point at a disposable target.
    """
    if "live_github" in request.keywords:
        yield
        return

    import agent_crew.github as gh

    observing = "github_writes" in request.fixturenames
    sink = request.getfixturevalue("github_writes") if observing else github_writes_recorder

    def _stub(name):
        def _call(*args, **kwargs):
            sink.append({"fn": name, "args": args, "kwargs": kwargs})
            if observing:
                return True
            # ⛔Raised AND recorded. Raising alone is not enough: the dispatcher
            #   wraps GitHub calls in `except Exception: logger.exception`, so a
            #   blocked write is swallowed and the offending test still passes —
            #   which is how three of them targeted a real PR through dozens of
            #   green runs. The teardown turns a swallowed attempt into a failure.
            raise GitHubWriteFromTest(
                f"agent_crew.github.{name}() was called from a test. The suite "
                f"must not mutate real GitHub objects — patch it, request the "
                f"`github_writes` fixture if the write is the thing under test, "
                f"or mark the test @pytest.mark.live_github with a disposable "
                f"target."
            )
        return _call

    for name in GITHUB_WRITE_FUNCTIONS:
        if hasattr(gh, name):
            monkeypatch.setattr(gh, name, _stub(name))
    yield
    if not observing and github_writes_recorder:
        calls = "\n  ".join(f"{c['fn']}(kwargs={c['kwargs']})"
                             for c in github_writes_recorder)
        raise GitHubWriteFromTest(
            "this test tried to mutate GitHub; the call was blocked, but the "
            f"attempt is the defect:\n  {calls}"
        )


@pytest.fixture
def github_writes_recorder():
    return []


@pytest.fixture(autouse=True)
def _mock_pane_alive_for_push(request):
    """Default all pane liveness checks to True in unit tests.

    Tests that need to simulate dead panes use monkeypatch to override
    agent_crew.server._pane_alive_for_push themselves.
    """
    if "no_pane_alive_mock" in request.keywords:
        yield
        return
    with patch("agent_crew.server._pane_alive_for_push", return_value=True):
        yield


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def task_queue(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture
def test_client(tmp_db):
    app = create_app(tmp_db)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def tmux_session():
    if not shutil.which("tmux"):
        pytest.skip("tmux not available")
    subprocess.run(["tmux", "new-session", "-d", "-s", "test_crew"], capture_output=True)
    yield "test_crew"
    subprocess.run(["tmux", "kill-session", "-t", "test_crew"], capture_output=True)


@pytest.fixture
def resolve_approved():
    """Valid resolve body approving a gate — {"status": "approved"}."""
    return {"status": "approved"}


@pytest.fixture
def resolve_rejected():
    """Valid resolve body rejecting a gate — {"status": "rejected"}."""
    return {"status": "rejected"}


@pytest.fixture
def stub_agents(tmp_path):
    scripts = {}
    for agent in ["claude", "codex"]:
        script = tmp_path / f"{agent}_stub.sh"
        script.write_text("#!/bin/sh\necho stub agent running\n")
        script.chmod(0o755)
        scripts[agent] = str(script)
    return scripts
