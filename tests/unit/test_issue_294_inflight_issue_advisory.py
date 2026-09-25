"""#294 — a direct enqueue could not see work the watcher was already running.

#276 taught `watch.active_issue_numbers()` to see an issue named only in a
task's description, so the WATCHER stopped claiming an issue another path had
in flight. The guard lives in `watch.py` and nothing else consulted it, so the
protection was one-directional. Measured 2026-09-13:

    impl-watch-ac467aac  03:20:04  crew triage --watch  {"issue": 292, "source": "watch"}
    implement-066c91a2   03:31:38  direct enqueue       {"issue": 292}

The second was enqueued while the first was still `in_progress` and 15 minutes
before the resulting PR existed, and ran a full implementer invocation against
work already in progress. It even carried `context.issue` — the structured field
#276 relies on. Nothing on that path read it.

⛔Surfaced, not blocked, and the distinction is the design. Several tasks
  legitimately share one issue — the implement task, its review, its fix rounds,
  its test — so refusing duplicates by issue would break the cascade this repo
  is built on. Only a second *implement* for an issue that already has a
  non-terminal implement is the expensive case, and even that is advisory: the
  caller is told at the one moment the collision is cheap to act on.
"""

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.watch import active_tasks_for_issue


def _server(tmp_db, *, unused_tcp_port):
    return create_app(db_path=tmp_db, pane_map={}, port=unused_tcp_port,
                      watchdog_disabled=True, anomaly_disabled=True,
                      push_fn=lambda *a, **k: None)


def _post(client, task_id, *, task_type="implement", issue=None, description="go",
          project="demo"):
    context = {} if issue is None else {"issue": issue}
    return client.post("/tasks", json={
        "task_id": task_id, "task_type": task_type, "description": description,
        "branch": "main", "priority": 3, "context": context, "project": project})


# ── 1. the lookup ─────────────────────────────────────────────────────


def test_an_in_flight_task_is_found(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="go", branch="main", context={"issue": 292}))
    assert active_tasks_for_issue(q, 292) == ["impl-a"]


def test_a_terminal_task_is_not_in_flight(tmp_db):
    from agent_crew.protocol import TaskResult

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="go", branch="main", context={"issue": 292}))
    q.submit_result("impl-a", TaskResult(task_id="impl-a", status="completed",
                                         summary="done"))
    assert active_tasks_for_issue(q, 292) == []


def test_the_description_fallback_is_shared_with_the_watcher(tmp_db):
    """⛔#276's parser, reused rather than reimplemented. Two copies of "which
    issue is this task about" would drift, and the drift would be invisible."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="Implement #292: something",
                          branch="main", context={"reviewed_sha": "abc"}))
    assert active_tasks_for_issue(q, 292) == ["impl-a"]


def test_the_task_type_filter(tmp_db):
    q = TaskQueue(tmp_db)
    for task_id, task_type in (("impl-a", "implement"), ("rev-a", "review")):
        q.enqueue(TaskRequest(task_id=task_id, task_type=task_type, description="go",
                              branch="main", context={"issue": 292}))
    assert active_tasks_for_issue(q, 292, task_type="implement") == ["impl-a"]
    assert active_tasks_for_issue(q, 292, task_type="review") == ["rev-a"]
    assert sorted(active_tasks_for_issue(q, 292)) == ["impl-a", "rev-a"]


def test_a_different_issue_does_not_match(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 999}))
    assert active_tasks_for_issue(q, 292) == []


def test_an_unreadable_queue_is_not_an_error(tmp_db):
    """⛔Bookkeeping never breaks an enqueue. This is advisory information; a
    failure to gather it must not cost the caller their task."""
    class _Broken:
        def list_all_with_status(self):
            raise RuntimeError("db is on fire")

    assert active_tasks_for_issue(_Broken(), 292) == []


# ── 2. the advisory on POST /tasks ────────────────────────────────────


def test_the_reported_collision_is_now_surfaced(tmp_db, *, unused_tcp_port):
    """★★The exact #294 scenario: a watch task in flight, then a direct
    enqueue for the same issue."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-watch-ac467aac", task_type="implement",
                          description="Implement #292: ...", branch="main",
                          context={"issue": 292, "source": "watch"}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "implement-066c91a2", issue=292).json()
    assert body["in_flight_for_issue"] == ["impl-watch-ac467aac"]


def test_the_task_is_still_enqueued(tmp_db, *, unused_tcp_port):
    """⛔Advisory, not a gate. Blocking would break the cascade — and worse,
    would make the caller's decision for them at the one moment they have the
    context to make it themselves."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        response = _post(client, "impl-b", issue=292)
    assert response.status_code == 201
    assert "impl-b" in {t.task_id for t in TaskQueue(tmp_db).list_tasks()}


def test_the_collision_is_logged_with_the_in_flight_id(tmp_db, caplog, *, unused_tcp_port):
    import logging

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    with caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
            _post(client, "impl-b", issue=292)
    assert any("impl-a" in r.message and "292" in r.message
               for r in caplog.records if r.levelno >= logging.WARNING), caplog.text


def test_a_review_alongside_an_implement_is_not_a_collision(tmp_db, *, unused_tcp_port):
    """⛔The cascade's normal shape. implement → review → fix → test all name one
    issue; flagging them would make the advisory noise, and noise is ignored."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "rev-a", task_type="review", issue=292).json()
    assert body["in_flight_for_issue"] == []


def test_a_finished_implement_is_not_a_collision(tmp_db, *, unused_tcp_port):
    from agent_crew.protocol import TaskResult

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    q.submit_result("impl-a", TaskResult(task_id="impl-a", status="completed",
                                         summary="done"))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-b", issue=292).json()
    assert body["in_flight_for_issue"] == []


def test_a_task_with_no_issue_reports_no_collision(tmp_db, *, unused_tcp_port):
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-a").json()
    assert body["in_flight_for_issue"] == []


def test_the_key_is_always_present(tmp_db, *, unused_tcp_port):
    """⛔Always a list, never absent. A consumer should not have to distinguish
    "no collision" from "this server version does not report collisions" — an
    advisory nobody can rely on finding is one nobody will read."""
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        first = _post(client, "impl-a", issue=292).json()
        second = _post(client, "impl-b", issue=292).json()
    assert first["in_flight_for_issue"] == []
    assert second["in_flight_for_issue"] == ["impl-a"]
    assert first["task_id"] == "impl-a" and second["task_id"] == "impl-b"


def test_the_new_task_does_not_report_itself(tmp_db, *, unused_tcp_port):
    """The check runs before the enqueue, so the task being created can never
    appear in its own collision list."""
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-solo", issue=292).json()
    assert "impl-solo" not in body["in_flight_for_issue"]


def test_a_bookkeeping_failure_does_not_break_the_enqueue(tmp_db, monkeypatch, *, unused_tcp_port):
    """⛔The whole feature is advisory, so it must never be able to cost a task."""
    import agent_crew.server as sv

    monkeypatch.setattr(sv, "active_tasks_for_issue",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        response = _post(client, "impl-a", issue=292)
    assert response.status_code == 201
    assert response.json()["in_flight_for_issue"] == []


# ── 3. the advisory must resolve the issue the way the queue will ─────


def test_a_description_only_enqueue_still_sees_the_collision(tmp_db, *, unused_tcp_port):
    """★★The review's finding. `POST /tasks` read `context.issue` and nothing
    else, while `enqueue` backfilled that same field from the description
    moments later — so this task reported no collision and was then stored as
    the very issue it collided with. Exactly the shape #276 exists for."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="Implement #292: go", branch="main",
                          context={"issue": 292}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-b",
                     description="Implement #292: the same thing again").json()
    assert body["in_flight_for_issue"] == ["impl-a"]


def test_both_sides_may_be_description_only(tmp_db, *, unused_tcp_port):
    """The in-flight task's own issue is backfilled by `enqueue`, so neither
    side needs the structured field for the advisory to work."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="Implement #292: go", branch="main",
                          context={}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-b", description="Implement #292: again").json()
    assert body["in_flight_for_issue"] == ["impl-a"]


def test_one_resolver_answers_for_both_call_sites():
    """⛔Two copies of "which issue is this task about" would drift, and the
    drift would be invisible until it produced a wrong advisory."""
    from agent_crew.queue import task_issue_number

    task = TaskRequest(task_id="t", task_type="implement",
                       description="Implement #292: go", branch="main", context={})
    assert task_issue_number(task) == 292

    task.context = {"issue": 900}
    assert task_issue_number(task) == 900, "the structured field must win"

    task.context = {}
    task.description = "Fix PR #292"
    assert task_issue_number(task) is None, \
        "#276's parser is anchored on purpose — a PR number is not an issue"


def test_the_structured_field_still_wins(tmp_db, *, unused_tcp_port):
    """Precedence is unchanged: a description is free text, a context key is a
    claim. A task that says one thing and claims another is the claim."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="go", branch="main", context={"issue": 900}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-b", issue=900,
                     description="Implement #292: misleading").json()
    assert body["in_flight_for_issue"] == ["impl-a"]


def test_the_advisory_and_the_stored_row_agree(tmp_db, *, unused_tcp_port):
    """★★The invariant behind the fix: whatever the advisory decided this task's
    issue was, that is the issue the row is stored under. These disagreeing is
    the bug — the advisory looked for nothing while the row joined issue 292."""
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        _post(client, "impl-b", description="Implement #292: go")
    assert TaskQueue(tmp_db).get_task_context("impl-b").get("issue") == 292


def test_a_description_that_names_nothing_resolves_to_nothing(tmp_db, *, unused_tcp_port):
    """No issue means no lookup — not a lookup for issue 0 or for everything."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="go", branch="main", context={"issue": 292}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        body = _post(client, "impl-b", description="just do the thing").json()
    assert body["in_flight_for_issue"] == []


def test_a_boolean_is_not_an_issue_number():
    """`True` is an `int` in Python, and issue #1 is a real issue.

    ⛔Without the `bool` rejection `context={"issue": True}` resolves to 1 and
      the advisory reports collisions with whatever is in flight for issue #1 —
      a task it has nothing to do with. The same confusion already cost #270 a
      round. Fall through to the description instead."""
    from agent_crew.queue import task_issue_number

    task = TaskRequest(task_id="t", task_type="implement",
                       description="Implement #294: go", branch="main",
                       context={"issue": True})
    assert task_issue_number(task) == 294, "a bool is not a claim about an issue"

    task.description = "no issue named here"
    assert task_issue_number(task) is None


# ── 4. the row the queue writes must be the resolver's answer ─────────


def _enqueued_issue(tmp_db, *, description, context, task_id="impl-a"):
    """What `enqueue` actually persisted as this task's issue."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                          description=description, branch="main", context=context))
    return q.get_task_context(task_id)


def test_enqueue_stores_the_parsed_number_when_the_context_says_nothing(tmp_db):
    assert _enqueued_issue(tmp_db, description="Implement #294: go",
                           context={})["issue"] == 294


def test_enqueue_stores_the_parsed_number_over_a_boolean(tmp_db):
    """★★The review's finding. `bool` is a subclass of `int`, so the gate
    `not isinstance(context.get("issue"), int)` is False for `True` — the
    backfill never ran and the row kept `issue: true`, while
    `task_issue_number()` (which does reject bool) told the advisory 294.

    That is the one-resolver invariant broken at the only place it is observable:
    the advisory and the stored row disagreeing about the same task, which is the
    entire defect PR #295 exists to close."""
    assert _enqueued_issue(tmp_db, description="Implement #294: go",
                           context={"issue": True})["issue"] == 294


def test_a_boolean_with_no_issue_in_the_description_is_not_stored(tmp_db):
    """⛔`True` must not survive as the issue either. Left in place it joins as
    issue #1 on every `WHERE issue = ?` path — #276's watcher guard and #294's
    advisory both — attributing this task to a real, unrelated issue."""
    stored = _enqueued_issue(tmp_db, description="no issue named here",
                             context={"issue": True})
    assert "issue" not in stored, f"stored a bool as an issue number: {stored!r}"


def test_a_real_structured_issue_is_never_overwritten(tmp_db):
    """⛔The control. Precedence must survive the fix: a valid claim wins over
    the description, and nothing here may rewrite it."""
    assert _enqueued_issue(tmp_db, description="Implement #294: misleading",
                           context={"issue": 900})["issue"] == 900


def test_the_row_and_the_resolver_never_disagree(tmp_db):
    """The invariant itself, stated once over the cases that differ.

    ⛔Asserted against `task_issue_number` rather than against literals, so this
      keeps biting if the resolver's own rules change."""
    from agent_crew.queue import task_issue_number

    for n, (description, context) in enumerate((
        ("Implement #294: go", {}),
        ("Implement #294: go", {"issue": True}),
        ("Implement #294: go", {"issue": 900}),
        ("no issue named here", {"issue": True}),
        ("no issue named here", {}),
    )):
        expected = task_issue_number(TaskRequest(
            task_id="probe", task_type="implement", description=description,
            branch="main", context=dict(context)))
        stored = _enqueued_issue(tmp_db, description=description,
                                 context=dict(context),
                                 task_id=f"impl-{n}").get("issue")
        assert stored == expected, (
            f"{description!r} + {context!r}: row says {stored!r}, "
            f"resolver says {expected!r}")


def test_a_context_only_enqueue_can_read_is_not_discarded(tmp_db):
    """⛔The drop must remove non-numbers, not everything the resolver missed.

    `TaskRequest` is a plain dataclass — `context: dict` is a annotation, not a
    coercion — so the two sites can genuinely see different things. Given a
    sequence of pairs, `task_issue_number()` reads it as no context at all
    (`isinstance(..., dict)` is False) while `enqueue` coerces it with
    `dict(...)` and sees issue 292. Dropping on `resolved is None` alone would
    throw that 292 away, which is the same class of bug as the one this PR
    closes: two readings of one task's issue, and the row losing the argument.
    """
    stored = _enqueued_issue(tmp_db, description="no issue named here",
                             context=[("issue", 292)])
    assert stored.get("issue") == 292, \
        f"discarded an issue number enqueue could read: {stored!r}"
