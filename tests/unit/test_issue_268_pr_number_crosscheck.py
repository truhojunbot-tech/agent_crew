"""#268 — a result may not silently rename the PR it was dispatched for.

alpha_engine#5288: a reviewer opened a PR it was never asked about, reviewed
*that* one, and posted the verdict on the PR it *was* asked about. Two of 363
reviews, both adjacent-number misreads. The alpha_engine repo defends at merge
time by diffing the comment body's file list against the real diff; this is the
same accident caught one layer earlier and much cheaper — at the moment the
result is submitted, before anything has been written to GitHub.

The hole was one line in `submit_result`:

    _review_pr = result.pr_number or ctx.get("pr_number")

`result.pr_number` won outright. An agent honest enough to report the PR it
actually read produced *no signal at all* when that differed from the request —
the mismatch was the one thing the server was in a position to notice, and the
`or` threw it away.

What this fixture pins:

  * agreement (in any spelling) and every "only one side knows" case still
    flow, because the first implement → PR hop legitimately has a pr_number on
    exactly one side;
  * disagreement is held: the result is stored — ⛔never discarded, an
    audit trail is not what we trim — with `needs_human`, both numbers side by
    side, and the whole cascade stopped: no GitHub comment, no fix, no test,
    no merge, on either transport.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_crew.pipeline import (
    PR_MISMATCH_MARKER,
    hold_mismatched_pr_result,
    pr_number_mismatch,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

BRANCH = "agent/codex/268-x"
REQUESTED = 268          # the PR the task was dispatched for
REPORTED = 269           # the adjacent number the reviewer actually read
FINDING = "HIGH src/agent_crew/server.py:3310 - `or` hides the disagreement"


# ── 1. the comparison itself ──────────────────────────────────────────


@pytest.mark.parametrize("reported, requested", [
    (268, 268),
    ("268", 268),
    (268, "268"),
    ("#268", 268),          # agents write PR numbers the way humans do
    (" 268 ", "#268"),
])
def test_agreement_in_any_spelling_is_not_a_mismatch(reported, requested):
    assert pr_number_mismatch(reported, requested) is None


def test_disagreement_reports_both_numbers_requested_first():
    assert pr_number_mismatch(REPORTED, REQUESTED) == (REQUESTED, REPORTED)


@pytest.mark.parametrize("reported, requested", [
    (None, 268),            # reviewer didn't echo the number — normal
    (268, None),            # ★the first implement → PR hop: only the result knows
    (None, None),
    ("", 268),
    (268, ""),
    ("not-a-pr", 268),      # unparseable: we cannot cross-check, so we don't
    (268, "not-a-pr"),
    (0, 268),
    (268, -1),
    (True, 268),            # ⛔bool is an int in Python; `True` is not PR #1
    (268, True),
])
def test_no_cross_check_is_possible_so_nothing_is_claimed(reported, requested):
    """⛔Absence is not disagreement. Treating "one side is silent" as a
    mismatch would hold every implement result that opens its own PR — the
    single most common path in the crew."""
    assert pr_number_mismatch(reported, requested) is None


# ── 2. what holding a result does to it ───────────────────────────────


def _result(**kw):
    base = dict(task_id="review-268", status="completed", summary="approve: LGTM",
                verdict="approve", findings=[FINDING], pr_number=REPORTED)
    base.update(kw)
    return TaskResult(**base)


def test_an_agreeing_result_passes_through_untouched():
    r = _result(pr_number=REQUESTED)
    held, mismatch = hold_mismatched_pr_result("review-268", r, {"pr_number": REQUESTED})
    assert mismatch is None
    assert held is r


def test_a_mismatched_result_is_held_for_a_human():
    held, mismatch = hold_mismatched_pr_result(
        "review-268", _result(), {"pr_number": REQUESTED})
    assert mismatch == (REQUESTED, REPORTED)
    assert held.status == "needs_human"


def test_the_held_summary_names_both_pr_numbers_and_the_reported_status():
    """The stored row is the only thing a human reads after the fact. If it
    doesn't say which PR was asked for and which was answered, holding the
    result has told them nothing they can act on."""
    held, _ = hold_mismatched_pr_result("review-268", _result(), {"pr_number": REQUESTED})
    assert PR_MISMATCH_MARKER in held.summary
    assert f"#{REQUESTED}" in held.summary and f"#{REPORTED}" in held.summary
    assert "completed" in held.summary, "the status the agent actually reported is lost"
    assert "approve: LGTM" in held.summary, "the agent's own summary was discarded"


def test_holding_preserves_the_evidence_it_is_holding():
    """⛔Verdict, findings and the *reported* PR number all survive. The point
    is to make the disagreement legible, not to erase the reviewer's work —
    a human needs to see which PR the findings are actually about."""
    held, _ = hold_mismatched_pr_result("review-268", _result(), {"pr_number": REQUESTED})
    assert held.verdict == "approve"
    assert held.findings == [FINDING]
    assert held.pr_number == REPORTED


def test_a_failed_result_is_held_too():
    """A mismatched `failed` must not reach the retry/fallback path either —
    rerouting a task whose result named the wrong PR just spends another agent
    on the same confusion."""
    held, mismatch = hold_mismatched_pr_result(
        "review-268", _result(status="failed", verdict=None, findings=[]),
        {"pr_number": REQUESTED})
    assert mismatch == (REQUESTED, REPORTED)
    assert held.status == "needs_human"
    assert "failed" in held.summary


def test_a_missing_context_is_not_a_mismatch():
    for ctx in ({}, None, {"issue": 268}):
        assert hold_mismatched_pr_result("review-268", _result(), ctx)[1] is None


# ── 3. the HTTP transport ─────────────────────────────────────────────


class _Push:
    def __init__(self):
        self.calls = []

    def __call__(self, pane, text):
        self.calls.append((pane, text))


def _server(tmp_db, push):
    return create_app(db_path=tmp_db,
                      pane_map={"implementer": "%1", "reviewer": "%2", "tester": "%3"},
                      port=8105, push_fn=push, watchdog_disabled=True,
                      anomaly_disabled=True)


def _enqueue(c, task_id, task_type, ctx):
    return c.post("/tasks", json={"task_id": task_id, "task_type": task_type,
                                  "description": "work", "branch": BRANCH,
                                  "priority": 3, "context": ctx, "project": ""})


def _submit(c, task_id, **kw):
    body = dict(task_id=task_id, status="completed", summary="request_changes: broken",
                verdict="request_changes", findings=[FINDING], pr_number=REPORTED)
    body.update(kw)
    return c.post(f"/tasks/{task_id}/result", json=body)


def test_http_a_mismatched_review_is_stored_and_stops_there(tmp_db, monkeypatch, github_writes):
    """★★The accident, driven through the real endpoint."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    # ⛔`post_review_comment` shells out to `gh` itself and returns False on any
    #   exception, so patching a wrapper it never calls would intercept nothing
    #   and the assertion would pass for the wrong reason. Patch what the
    #   handler actually imports.
    monkeypatch.setattr(
        "agent_crew.github.post_review_comment",
        lambda *a, **k: pytest.fail("posted a verdict for a PR the reviewer never read"))
    push = _Push()
    with TestClient(_server(tmp_db, push)) as c:
        _enqueue(c, "review-mm", "review", {"pr_number": REQUESTED})
        dispatched = len(push.calls)   # the original dispatch is not the cascade
        assert _submit(c, "review-mm").status_code == 200
        stored = c.get("/tasks/review-mm").json()

    # ⛔Held, not dropped: everything the reviewer said is still on the row.
    assert stored["status"] == "needs_human"
    assert stored["verdict"] == "request_changes"
    assert stored["findings"] == [FINDING]
    assert PR_MISMATCH_MARKER in stored["summary"]

    q = TaskQueue(tmp_db)
    assert not [t for t in q.list_tasks() if t.task_type == "implement"], \
        "a fix task was spawned from a result that named a different PR"
    assert len(push.calls) == dispatched, \
        "work was pushed to an agent off a mismatched result"


def test_http_the_response_says_the_result_was_held(tmp_db, monkeypatch, github_writes):
    """A 200 that looks exactly like a clean accept teaches the agent nothing.
    The body has to carry the disagreement back — that is the only channel to
    the process that submitted it."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "review-body", "review", {"pr_number": REQUESTED})
        body = _submit(c, "review-body").json()

    assert body.get("held") == "pr_number_mismatch"
    assert body.get("requested_pr") == REQUESTED
    assert body.get("reported_pr") == REPORTED


def test_http_an_agreeing_review_still_cascades(tmp_db, monkeypatch, github_writes):
    """⛔The control. A gate that also stops the normal path is not a gate."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda *a, **k: True)
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "review-ok", "review", {"pr_number": REQUESTED})
        assert _submit(c, "review-ok", pr_number=REQUESTED).json().get("held") is None
        assert c.get("/tasks/review-ok").json()["status"] == "completed"

    fixes = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]
    assert len(fixes) == 1 and FINDING in fixes[0].description


def test_http_an_implement_result_may_still_name_the_pr_it_just_opened(tmp_db, monkeypatch, github_writes):
    """★★The path this gate could most easily have broken: the implement task
    was dispatched with no PR at all, and its result reports the PR it created.
    One-sided is not a mismatch."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "impl-new", "implement", {"issue": 268})
        r = _submit(c, "impl-new", verdict=None, findings=[], summary="opened the PR")
        assert r.json().get("held") is None

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1, "the first implement → review hop was blocked"
    assert reviews[0].context.get("pr_number") == REPORTED


def test_http_a_mismatched_approval_produces_no_follow_up_at_all(tmp_db, monkeypatch, github_writes):
    """The worst case in #268's chain: an approve that names the wrong PR is one
    hop from merging code nobody reviewed (`no_tester` merges on approval).

    ⛔The assertion is "no follow-up of ANY type", not just "no merge". Two
      mechanisms stop the merge — the hold and `_resolve_verdict`, which reads
      any non-`completed` status as request_changes — so a merge-only assertion
      passes even with the cascade guard deleted, and would then be pinning the
      downgrade rather than the gate. The downgrade turns the approval into a
      *fix*, which is still work spawned off a result that named another PR.
      Measured: this assertion survives removing either half of the gate alone
      and dies when both go — the redundancy is the point, and it is only
      visible if the assertion covers every follow-up type."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda *a, **k: True)
    monkeypatch.setattr("agent_crew.github.merge_pr",
                        lambda *a, **k: pytest.fail("merged off a mismatched approval"))
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "review-appr", "review",
                 {"pr_number": REQUESTED, "no_tester": True})
        _submit(c, "review-appr", verdict="approve", findings=[], summary="approve: LGTM")

    spawned = [t.task_type for t in TaskQueue(tmp_db).list_tasks()
               if t.task_id != "review-appr"]
    assert spawned == [], f"a mismatched approval spawned {spawned}"


# ── 4. the MCP transport ──────────────────────────────────────────────


def _mcp_submit(tmp_db, **kwargs):
    from agent_crew.mcp_server import build_mcp_server

    fn = build_mcp_server(tmp_db)._tool_manager._tools["submit_result"].fn
    return asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)


def test_mcp_a_mismatched_result_is_held_on_this_transport_too(tmp_db, monkeypatch):
    """★★A gate on one transport is a gate an agent can walk around by
    switching how it reports (#123 exists because that keeps happening)."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-mcp", task_type="review", description="review",
                          branch=BRANCH, context={"pr_number": REQUESTED}))
    q.dequeue(role="reviewer")

    out = _mcp_submit(tmp_db, task_id="review-mcp", status="completed",
                      summary="request_changes: broken", verdict="request_changes",
                      findings=[FINDING], pr_number=REPORTED)

    assert out.get("acknowledged") is True, out
    assert out.get("held") == "pr_number_mismatch"
    stored = {t.task_id: t for t in q.list_tasks()}["review-mcp"]
    assert stored.status == "needs_human"
    assert stored.findings == [FINDING]
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


def test_mcp_an_agreeing_result_still_cascades(tmp_db, monkeypatch):
    """⛔The MCP control."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-mcp-ok", task_type="review", description="review",
                          branch=BRANCH, context={"pr_number": REQUESTED}))
    q.dequeue(role="reviewer")

    out = _mcp_submit(tmp_db, task_id="review-mcp-ok", status="completed",
                      summary="request_changes: broken", verdict="request_changes",
                      findings=[FINDING], pr_number=REQUESTED)

    assert out.get("held") is None
    assert len([t for t in q.list_tasks() if t.task_type == "implement"]) == 1


# ── 5. the spellings have to survive the API boundary ─────────────────
#
# Review of PR #270, P2: `pr_number` was typed `Optional[int]`, so FastAPI
# rejected `"#268"` with a 422 *before* `_as_pr_number` ever ran. The helper
# accepted three spellings and the endpoint accepted two — and the one it threw
# away it threw away completely, which is the same 4xx-discards-the-result
# behaviour this PR argued against everywhere else. Normalisation belongs at
# the boundary, not behind it.


@pytest.mark.parametrize("submitted", [REQUESTED, str(REQUESTED), f"#{REQUESTED}", f" {REQUESTED} "])
def test_the_result_type_normalises_every_spelling_to_an_int(submitted):
    """⛔An `int` on the way out, whatever came in. The queue writes this to an
    INTEGER column and the cascade calls `int()` on it — a `str` that survived
    this far would be a second bug wearing the first one's clothes."""
    r = TaskResult(task_id="t", status="completed", summary="s", pr_number=submitted)
    assert r.pr_number == REQUESTED and isinstance(r.pr_number, int)


def test_an_empty_pr_number_string_means_no_pr_rather_than_an_error():
    """`""` used to be a 422, which threw away a whole result over a field the
    agent was telling us it had nothing to put in."""
    assert TaskResult(task_id="t", status="completed", summary="s", pr_number="").pr_number is None


@pytest.mark.parametrize("junk", ["not-a-pr", "#", "12x", "-3"])
def test_a_string_that_names_no_pr_is_still_rejected(junk):
    """⛔The relaxation is for *spelling*, not for junk. A value that cannot
    name a PR is a malformed request, not a mismatch, and the caller has to
    hear about it — silently storing `None` would drop the very signal #268
    exists to preserve."""
    with pytest.raises(ValueError, match="pr_number"):
        TaskResult(task_id="t", status="completed", summary="s", pr_number=junk)


@pytest.mark.parametrize("value", [None, 0, REQUESTED])
def test_non_string_values_are_left_exactly_as_they_were(value):
    """⛔No behaviour change for anything that already worked. `0` in
    particular stays `0` — falsy, so the cascade reads it as "no PR", which is
    what it did before this normalisation existed."""
    r = TaskResult(task_id="t", status="completed", summary="s", pr_number=value)
    assert r.pr_number == value


def test_http_a_hash_prefixed_pr_number_agrees_and_cascades(tmp_db, monkeypatch, github_writes):
    """★★The regression the review asked for: `#268` end-to-end through the
    endpoint, agreeing with the dispatched PR."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda *a, **k: True)
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "review-hash", "review", {"pr_number": REQUESTED})
        r = _submit(c, "review-hash", pr_number=f"#{REQUESTED}")
        assert r.status_code == 200, r.text
        assert r.json().get("held") is None
        assert c.get("/tasks/review-hash").json()["pr_number"] == REQUESTED

    assert len([t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]) == 1


def test_http_a_hash_prefixed_pr_number_can_still_mismatch(tmp_db, monkeypatch, github_writes):
    """⛔Accepting the spelling must not accept the disagreement with it."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "review-hash-mm", "review", {"pr_number": REQUESTED})
        body = _submit(c, "review-hash-mm", pr_number=f"#{REPORTED}").json()

    assert body.get("held") == "pr_number_mismatch"
    assert (body.get("requested_pr"), body.get("reported_pr")) == (REQUESTED, REPORTED)


def test_http_a_malformed_pr_number_is_still_a_request_error(tmp_db, monkeypatch, github_writes):
    """The 422 stays where it belongs — on requests that are actually malformed."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    with TestClient(_server(tmp_db, _Push())) as c:
        _enqueue(c, "review-junk", "review", {"pr_number": REQUESTED})
        assert _submit(c, "review-junk", pr_number="not-a-pr").status_code == 422


def test_mcp_accepts_the_same_spellings(tmp_db, monkeypatch):
    """⛔Parity, for the same reason the hold itself has parity: a boundary that
    normalises on one transport and not the other is a boundary an agent
    crosses by changing how it reports."""
    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-mcp-hash", task_type="review", description="review",
                          branch=BRANCH, context={"pr_number": REQUESTED}))
    q.dequeue(role="reviewer")

    out = _mcp_submit(tmp_db, task_id="review-mcp-hash", status="completed",
                      summary="request_changes: broken", verdict="request_changes",
                      findings=[FINDING], pr_number=f"#{REQUESTED}")

    assert out.get("acknowledged") is True and out.get("held") is None, out
    assert {t.task_id: t for t in q.list_tasks()}["review-mcp-hash"].pr_number == REQUESTED


def test_the_mcp_tool_schema_actually_advertises_the_string_form(tmp_db):
    """⛔The behavioural test above calls the tool function directly, so it
    passes even with the annotation narrowed back to `Optional[int]` —
    normalisation lives in the dataclass and fires either way. A real MCP
    client is validated against the PUBLISHED schema before the function is
    reached, so the declaration needs its own assertion or widening it is
    untested. (Found by mutation: narrowing the signature killed nothing.)"""
    from agent_crew.mcp_server import build_mcp_server

    tool = build_mcp_server(tmp_db)._tool_manager._tools["submit_result"]
    types = {t.get("type") for t in tool.parameters["properties"]["pr_number"]["anyOf"]}
    assert {"integer", "string", "null"} <= types, types
