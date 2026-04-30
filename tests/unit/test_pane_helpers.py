"""Unit tests for server.py pane helper functions (#133, #134, #136).

- _pane_token_count: parses Claude Code token hint from pane content
- _pane_clear_context: sends /clear when token threshold exceeded
- _pane_dismiss_permission_prompt: auto-dismisses gemini permission prompts
- list_stale_pending: queue method for #136 re-dispatch logic
"""
import time
from unittest.mock import MagicMock, call, patch

from agent_crew.server import (
    _GEMINI_PERMISSION_RE,
    _TOKEN_COUNT_RE,
    _pane_clear_context,
    _pane_dismiss_permission_prompt,
    _pane_token_count,
)


# ---------------------------------------------------------------------------
# _pane_token_count  (#133)
# ---------------------------------------------------------------------------


def _fake_capture(stdout: str):
    m = MagicMock()
    m.stdout = stdout
    return m


def test_pane_token_count_k_suffix():
    """'save 544.1k tokens' → 544100"""
    with patch("agent_crew.server.subprocess.run", return_value=_fake_capture(
        "new task? /clear to save 544.1k tokens"
    )):
        assert _pane_token_count("%1") == 544_100


def test_pane_token_count_m_suffix():
    """'save 1.2M tokens' → 1200000"""
    with patch("agent_crew.server.subprocess.run", return_value=_fake_capture(
        "new task? /clear to save 1.2M tokens"
    )):
        assert _pane_token_count("%1") == 1_200_000


def test_pane_token_count_plain_integer():
    """'save 200000 tokens' → 200000"""
    with patch("agent_crew.server.subprocess.run", return_value=_fake_capture(
        "/clear to save 200,000 tokens"
    )):
        assert _pane_token_count("%1") == 200_000


def test_pane_token_count_no_hint():
    """No token hint → 0"""
    with patch("agent_crew.server.subprocess.run", return_value=_fake_capture(
        "claude is thinking..."
    )):
        assert _pane_token_count("%1") == 0


def test_pane_token_count_subprocess_failure():
    """subprocess.run raises → returns 0 without crashing"""
    with patch("agent_crew.server.subprocess.run", side_effect=OSError("no tmux")):
        assert _pane_token_count("%1") == 0


# ---------------------------------------------------------------------------
# _pane_clear_context  (#133)
# ---------------------------------------------------------------------------


def test_pane_clear_context_sends_slash_clear():
    """/clear is sent to the correct pane."""
    calls = []
    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return MagicMock()

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        with patch("agent_crew.server.time.sleep"):
            _pane_clear_context("%5")

    assert any("/clear" in " ".join(c) for c in calls)
    assert any("%5" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# _pane_dismiss_permission_prompt  (#134)
# ---------------------------------------------------------------------------


_GEMINI_PROMPT = (
    "Allow execution of tools requested by this agent?\n"
    "  1) No\n  2) Yes, allow for this session\n"
)


def test_pane_dismiss_returns_true_when_prompt_present():
    """Returns True and sends '2' when gemini permission prompt is visible."""
    sent = []

    def fake_run(cmd, **_kw):
        if "capture-pane" in cmd:
            m = MagicMock()
            m.stdout = _GEMINI_PROMPT
            return m
        sent.append(cmd)
        return MagicMock()

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        result = _pane_dismiss_permission_prompt("%7")

    assert result is True
    assert any("2" in c for c in sent)


def test_pane_dismiss_returns_false_when_no_prompt():
    """Returns False and does NOT send keys when no permission prompt."""
    sent = []

    def fake_run(cmd, **_kw):
        if "capture-pane" in cmd:
            m = MagicMock()
            m.stdout = "claude is processing..."
            return m
        sent.append(cmd)
        return MagicMock()

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        result = _pane_dismiss_permission_prompt("%7")

    assert result is False
    # send-keys must NOT have been called
    assert not any("send-keys" in " ".join(c) for c in sent)


def test_pane_dismiss_subprocess_failure_returns_false():
    with patch("agent_crew.server.subprocess.run", side_effect=OSError("no tmux")):
        assert _pane_dismiss_permission_prompt("%3") is False


# ---------------------------------------------------------------------------
# list_stale_pending  (#136)  — queue method test
# ---------------------------------------------------------------------------


def test_list_stale_pending_returns_old_tasks(tmp_db):
    """Pending tasks older than the threshold are returned."""
    from agent_crew.queue import TaskQueue

    q = TaskQueue(tmp_db)
    old_ts = time.time() - 200  # 200s ago
    from agent_crew.protocol import TaskRequest

    req = TaskRequest(
        task_id="stale-1",
        task_type="implement",
        description="old work",
        branch="main",
    )
    # Enqueue normally then update created_at to be old.
    q.enqueue(req)
    conn = q._connect()
    conn.execute("UPDATE tasks SET created_at = ? WHERE task_id = ?", (old_ts, "stale-1"))
    conn.commit()
    conn.close()

    stale = q.list_stale_pending(older_than_seconds=120, now=time.time())
    assert any(r["task_id"] == "stale-1" for r in stale)


def test_list_stale_pending_excludes_fresh_tasks(tmp_db):
    """Tasks enqueued recently are NOT returned as stale."""
    from agent_crew.queue import TaskQueue
    from agent_crew.protocol import TaskRequest

    q = TaskQueue(tmp_db)
    req = TaskRequest(
        task_id="fresh-1",
        task_type="implement",
        description="new work",
        branch="main",
    )
    q.enqueue(req)

    stale = q.list_stale_pending(older_than_seconds=120, now=time.time())
    assert not any(r["task_id"] == "fresh-1" for r in stale)


def test_list_stale_pending_excludes_in_progress(tmp_db):
    """in_progress tasks are never returned (only pending)."""
    from agent_crew.queue import TaskQueue
    from agent_crew.protocol import TaskRequest

    q = TaskQueue(tmp_db)
    old_ts = time.time() - 500

    req = TaskRequest(
        task_id="prog-1",
        task_type="implement",
        description="in progress work",
        branch="main",
    )
    q.enqueue(req)
    conn = q._connect()
    conn.execute(
        "UPDATE tasks SET status = 'in_progress', created_at = ? WHERE task_id = ?",
        (old_ts, "prog-1"),
    )
    conn.commit()
    conn.close()

    stale = q.list_stale_pending(older_than_seconds=120, now=time.time())
    assert not any(r["task_id"] == "prog-1" for r in stale)
