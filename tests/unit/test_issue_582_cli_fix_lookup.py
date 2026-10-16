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


class _Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


def test_adopts_fix_created_after_45_seconds_while_outbox_pending(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cli, "time", clock)
    fallback = []
    import agent_crew.pipeline as pipeline
    monkeypatch.setattr(pipeline, "auto_enqueue_fix", lambda *a, **k: fallback.append(True))

    class Queue:
        def list_tasks(self):
            return [_fix("rev-1")] if clock.now >= 45 else []

        def outbox_get(self, parent_task_id):
            assert parent_task_id == "rev-1"
            return {"state": "pending"}

    fixes = cli._await_review_fix(Queue(), "rev-1", poll_interval=5.0)
    assert [task.task_id for task in fixes] == ["fix-1"]
    assert clock.now == 45.0
    assert fallback == []


def test_terminal_outbox_without_fix_falls_back_immediately(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cli, "time", clock)
    fallback = []
    import agent_crew.pipeline as pipeline
    monkeypatch.setattr(pipeline, "auto_enqueue_fix", lambda *a, **k: fallback.append(True))

    class Queue:
        def list_tasks(self):
            return []

        def outbox_get(self, parent_task_id):
            return {"state": "applied" if clock.now >= 10 else "replaying"}

    assert cli._await_review_fix(Queue(), "rev-1", poll_interval=5.0) == []
    assert clock.now == 10.0
    assert fallback == [True]


def test_no_outbox_row_waits_at_most_300_seconds(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cli, "time", clock)
    fallback = []
    import agent_crew.pipeline as pipeline
    monkeypatch.setattr(pipeline, "auto_enqueue_fix", lambda *a, **k: fallback.append(True))

    class Queue:
        def list_tasks(self):
            return []

        def outbox_get(self, parent_task_id):
            return None

    assert cli._await_review_fix(Queue(), "rev-1", poll_interval=7.0) == []
    assert clock.now == 300.0
    assert fallback == [True]
