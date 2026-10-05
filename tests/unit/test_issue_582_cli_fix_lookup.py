"""#582: `crew run` must find the server-cascade fix that lands after the
review result, and its local fallback must name the repo and a checkout."""
from types import SimpleNamespace

from agent_crew import cli


def _fix(review_id):
    return SimpleNamespace(task_id="fix-1", task_type="implement",
                           context={"prev_task_id": review_id})


class _LateQueue:
    """The fix appears only after a few polls, like the server cascade."""

    def __init__(self, review_id, appear_after):
        self.calls = 0
        self.review_id = review_id
        self.appear_after = appear_after

    def list_tasks(self):
        self.calls += 1
        return [_fix(self.review_id)] if self.calls > self.appear_after else []


def test_polls_for_late_cascade_fix_before_fallback(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    called = []
    import agent_crew.pipeline as pipeline
    monkeypatch.setattr(pipeline, "auto_enqueue_fix",
                        lambda *a, **k: called.append((a, k)))
    q = _LateQueue("rev-1", appear_after=3)

    fixes = cli._await_review_fix(q, "rev-1", timeout=30.0, poll_interval=0.0)

    assert [t.task_id for t in fixes] == ["fix-1"]
    assert called == []


def test_fallback_passes_repo_and_repo_cwd(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    calls = []
    q = _LateQueue("rev-1", appear_after=10**9)

    def fake_auto_enqueue_fix(queue, review_id, **kwargs):
        calls.append((review_id, kwargs))
        q.appear_after = 0

    import agent_crew.pipeline as pipeline
    monkeypatch.setattr(pipeline, "auto_enqueue_fix", fake_auto_enqueue_fix)

    fixes = cli._await_review_fix(q, "rev-1", repo="o/r", repo_cwd="/wt/codex",
                                  timeout=0.0, poll_interval=0.0)

    assert calls == [("rev-1", {"repo": "o/r", "repo_cwd": "/wt/codex"})]
    assert [t.task_id for t in fixes] == ["fix-1"]
