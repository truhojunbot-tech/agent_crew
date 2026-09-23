"""#294's in-flight advisory — removed by the SEV-0 CEA fold. This is its record.

#294 measured a real duplicate (2026-09-13)::

    impl-watch-ac467aac  03:20:04  crew triage --watch  {"issue": 292, "source": "watch"}
    implement-066c91a2   03:31:38  direct enqueue       {"issue": 292}

The second ran a full implementer invocation against work already in progress.
The fix at the time was an advisory on `POST /tasks`: resolve the issue number,
list non-terminal tasks carrying it, log a warning, return them as
`in_flight_for_issue`. Never a gate.

⛔That is the thing the ADR removes (§11.1 row 14, fixture CXC-1), and the
  reason is not that #294 was wrong about the duplicate. It is that the advisory
  was a **second duplicate matcher standing beside admission** — a control plane
  that matched on a *label* (the issue number) while the real decision was made
  elsewhere. A label-matcher is wrong in both directions: two tasks meaning the
  same thing under different issue numbers were never flagged, and the four
  tasks that legitimately share one issue (implement, its review, its fix
  rounds, its test) were flagged every time, which is how an advisory becomes
  noise and then becomes ignored.

Duplicate suppression moves to the engine's `intent_hash` (Π P4), computed from
what a task *is* rather than what it is labelled, and it decides rather than
advises. Until that unique partial index lands, admission answers the question
not at all — a stated gap beats an advisory nobody can act on.

This file therefore asserts the **absence**: the endpoint no longer computes or
reports a collision, and the helper no longer exists. `watch.active_issue_numbers`
is deliberately untouched — the watcher's "is anything in flight for this issue"
is a scheduling question about its own next move, not an admission decision.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

import agent_crew.server as server_module
import agent_crew.watch as watch_module
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, task_issue_number
from agent_crew.server import create_app


def _server(tmp_db):
    return create_app(db_path=tmp_db, pane_map={}, port=0,
                      watchdog_disabled=True, anomaly_disabled=True,
                      push_fn=lambda *a, **k: None)


def _post(client, task_id, *, task_type="implement", issue=None, description="go",
          project="demo"):
    context = {} if issue is None else {"issue": issue}
    return client.post("/tasks", json={
        "task_id": task_id, "task_type": task_type, "description": description,
        "branch": "main", "priority": 3, "context": context, "project": project})


# ── the advisory is gone ────────────────────────────────────────────────────


def test_the_helper_no_longer_exists():
    """CXC-1's static half, from the other side: not merely unused by the
    server, but deleted, so it cannot be re-imported by the next call site."""
    assert not hasattr(watch_module, "active_tasks_for_issue")


def test_the_server_does_not_reference_it():
    assert "active_tasks_for_issue" not in inspect.getsource(server_module)


def test_a_colliding_enqueue_is_admitted_and_reports_nothing(tmp_db):
    """The exact #294 shape: a second implement for an issue that already has a
    non-terminal implement. Still admitted — it always was — and now the
    response says nothing about it, because nothing here is entitled to."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", project="demo", context={"issue": 292}))
    with TestClient(_server(tmp_db)) as client:
        response = _post(client, "impl-b", issue=292)
    assert response.status_code == 201
    body = response.json()
    assert body == {"task_id": "impl-b"}, \
        "POST /tasks returns the id and nothing else; no advisory key survives"


def test_no_response_ever_carries_the_advisory_key(tmp_db):
    """⛔Asserted on the collision case *and* the clean case. A key that is
    absent only when empty is still a key a client will code against."""
    with TestClient(_server(tmp_db)) as client:
        assert "in_flight_for_issue" not in _post(client, "impl-a", issue=292).json()
        assert "in_flight_for_issue" not in _post(client, "impl-b", issue=292).json()
        assert "in_flight_for_issue" not in _post(client, "impl-c").json()


def test_the_issue_resolver_itself_is_untouched():
    """`task_issue_number` stays: `enqueue` still backfills `context.issue` from
    the description (#276), which is provenance the receipt will carry. Removing
    the advisory removed a *decision*, not the ability to name an issue."""
    task = TaskRequest(task_id="t", task_type="implement",
                       description="Implement #292: go", branch="main", context={})
    assert task_issue_number(task) == 292
    task = TaskRequest(task_id="t", task_type="implement", description="go",
                       branch="main", context={"issue": True})
    assert task_issue_number(task) is None, "a bool is not a claim about an issue"


def test_the_backfill_still_happens_on_enqueue(tmp_db):
    """The behaviour #276 added survives the removal: an issue named only in the
    description is still stored, so nothing downstream loses the number."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="Implement #292: go", branch="main", project="demo", context={}))
    assert q.get_task_context("impl-a").get("issue") == 292
