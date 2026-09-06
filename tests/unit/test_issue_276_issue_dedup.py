"""#276 — dedup must not depend on every enqueue path's diligence.

Issue #272 was implemented twice: `implement-4e08dbe4` at 10:52:15Z from a
direct enqueue, then `impl-watch-87641e2b` at 11:00:04Z from the watcher, while
the first was still in flight. A whole implementer invocation on work already
being done.

Neither guard was broken. `active_issue_numbers()` builds its set from
`context["issue"]`, and the 10:52 task carried the number only in its
description (`Implement #272: …`) with `context = {"reviewed_sha": …}`. The
open-PR guard could not help either — PR #275 was not opened until 11:53:25Z,
53 minutes after the claim.

## Why the parser is anchored, and not a general `#\\d+` scan

Measured over all 5085 task rows on this host, of which 4721 have no
`context.issue`:

    ^implement #N            →   4 distinct numbers,    7 rows
    any #N minus "PR #N"     → 987 distinct numbers, 3829 rows
    "issue #N" anywhere      → 121 distinct numbers,  426 rows

The broad rule would suppress claims across nearly a thousand issue numbers —
it matches spec section numbers, list markers and prose — which would disable
the watcher rather than tune it. The `issue #N` shape is dominated by *discuss*
tasks (`Discuss: Review quota-ops issue #13 …`), and a panel discussion is not
work in flight on the issue.

The anchored rule matched exactly the seven genuine `Implement #N` rows and
nothing else in that corpus. ⛔That asymmetry is the whole argument: a missed
suppression costs a duplicate provider invocation, but a false suppression
silently stops the watcher claiming a real issue, and at 987 numbers it would
stop it claiming almost anything.
"""

import json

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, issue_from_description
from agent_crew.watch import active_issue_numbers, select_candidates, tasks_by_issue

#: Real descriptions from this host's task DBs, verbatim.
CORPUS_HITS = [
    ("Implement #272: tester role duplicates full CI suite on the same host", 272),
    ("Implement #224: add continuous GitHub issue ingestion/claim loop", 224),
    ("Implement #247: ops: verify #238 context-cap/reset and retry-waste fix", 247),
    ("implement #1: lowercase, because a description is free text", 1),
    ("  Implement #99 — an em dash instead of a colon", 99),
]

CORPUS_MISSES = [
    "Fix PR #251",
    "Fix PR #5462 — review round 2",
    "Fix branch fix/263-no-github-writes-from-tests: address review on PR #274 (#268).",
    "Discuss: Review quota-ops issue #13 branch fix/pane-aware-proactive-compact",
    "Review PR #218 (issue #217) — adds timeout=30 to the git subprocess calls",
    "Reported via blackboard from alpha-engine, see alpha_engine#5541 for measurement",
    "Implement GitHub issue ingestion",
    "스펙 §3.13.4 입력의 노드별 실효 슬롯 배선",
    "",
]


# ── 1. the parser ─────────────────────────────────────────────────────


@pytest.mark.parametrize("description, expected", CORPUS_HITS)
def test_an_implement_task_names_its_issue(description, expected):
    assert issue_from_description(description) == expected


@pytest.mark.parametrize("description", CORPUS_MISSES)
def test_everything_else_names_nothing(description):
    """★★The precision half, and the one that matters.

    `Fix PR #251` and `on PR #274` are PR numbers; `alpha_engine#5541` is
    another repo's issue; `issue #13` is a discuss task. Reading any of them as
    "this issue is being worked on" would suppress a legitimate claim."""
    assert issue_from_description(description) is None


@pytest.mark.parametrize("value", [None, 0, 12, b"Implement #1"])
def test_a_non_string_description_is_not_parsed(value):
    """Never let bookkeeping raise — the caller is a dedup pass, not a parser."""
    assert issue_from_description(value) is None


# ── 2. the read side ──────────────────────────────────────────────────


def _enqueue(q, task_id, description, *, context=None, project=""):
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement", description=description,
                          branch="main", context=context if context is not None else {},
                          project=project))


def test_a_description_only_task_is_visible_to_dedup(tmp_db):
    """★★The incident. This is the exact row that was invisible at 11:00:04Z."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "implement-4e08dbe4",
             "Implement #272: tester role duplicates full CI suite on the same host",
             context={"reviewed_sha": "c6e8211"})
    assert 272 in active_issue_numbers(q)


def _strip_backfill(tmp_db, task_id, context):
    """Rewrite a row's context as it would have been stored BEFORE #276.

    ⛔Necessary, not fussy. The write-side backfill now populates
      `context["issue"]` for anything enqueued through `TaskQueue`, so a test
      that enqueues normally can never exercise the read-side fallback —
      mutation confirmed it: deleting the fallback killed nothing. The rows
      that actually need it are the 4721 already on this host, written before
      the backfill existed, and this is what one of those looks like.
    """
    import sqlite3

    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE tasks SET context = ? WHERE task_id = ?",
                 (json.dumps(context), task_id))
    conn.commit()
    conn.close()


def test_a_row_written_before_the_backfill_is_still_deduped(tmp_db):
    """★★The read-side fallback's real job: rows already in the database.

    A fix that only populated new rows would leave every existing task
    invisible, which is most of them on any running host."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "implement-legacy", "Implement #272: written before #276 shipped",
             context={"reviewed_sha": "c6e8211"})
    _strip_backfill(tmp_db, "implement-legacy", {"reviewed_sha": "c6e8211"})

    assert q.get_task_context("implement-legacy") == {"reviewed_sha": "c6e8211"}
    assert 272 in active_issue_numbers(q)
    assert (tasks_by_issue(q) or {}).get(272) == "implement-legacy"


def test_the_watcher_will_not_offer_an_issue_that_is_already_in_flight(tmp_db):
    """★★End to end through the selection the watcher actually calls."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "implement-4e08dbe4", "Implement #272: tester role duplicates the suite",
             context={"reviewed_sha": "c6e8211"})

    offered = select_candidates([{"number": 272, "labels": []}, {"number": 999, "labels": []}],
                                claimed=set(), active_issues=active_issue_numbers(q))
    assert [i["number"] for i in offered] == [999]


def test_a_finished_task_stops_suppressing_its_issue(tmp_db):
    """⛔The fallback must inherit the terminal-status rule, not bypass it.
    Suppressing forever would be a worse bug than the one being fixed —
    silently un-claimable issues rather than one duplicate run."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-done", "Implement #272: done already", context={"reviewed_sha": "x"})
    q.submit_result("impl-done", TaskResult(task_id="impl-done", status="completed", summary="s"))
    assert 272 not in active_issue_numbers(q)


def test_the_structured_field_still_wins_when_both_are_present(tmp_db):
    """A description is free text; `context.issue` is a claim. If they
    disagree, the structured field is the answer and the parse is not
    consulted — the fallback exists for absence, not for arbitration."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-both", "Implement #272: mismatched on purpose", context={"issue": 900})
    active = active_issue_numbers(q)
    assert 900 in active and 272 not in active


def test_tasks_by_issue_gained_the_same_fallback(tmp_db):
    """⛔Same blind spot, same read of `context["issue"]`, one function over.
    Reconciliation asking "was a task ever created for this issue?" would have
    answered no for the very row that proves it was."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-recon", "Implement #272: reconcile me", context={"reviewed_sha": "x"})
    assert (tasks_by_issue(q) or {}).get(272) == "impl-recon"


# ── 3. the write side ─────────────────────────────────────────────────


def test_enqueue_records_the_issue_it_can_read(tmp_db):
    """Proposal 2: make the structured field true at the choke point, so the
    read-side parse rarely has to fire. Every path — HTTP, MCP, pipeline,
    cli — goes through `enqueue`."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-backfill", "Implement #272: backfilled", context={"reviewed_sha": "x"})
    stored = q.get_task_context("impl-backfill")
    assert stored["issue"] == 272
    assert stored["reviewed_sha"] == "x", "the caller's context was replaced, not extended"


def test_enqueue_does_not_overwrite_an_issue_the_caller_supplied(tmp_db):
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-keep", "Implement #272: caller knows better", context={"issue": 900})
    assert q.get_task_context("impl-keep")["issue"] == 900


def test_enqueue_adds_nothing_when_the_description_names_nothing(tmp_db):
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-quiet", "Fix PR #251", context={"reviewed_sha": "x"})
    assert "issue" not in q.get_task_context("impl-quiet")


def test_enqueue_does_not_mutate_the_callers_task(tmp_db):
    """⛔The caller may reuse the object — a fallback that enqueues the same
    TaskRequest twice would find its own backfill on the second pass."""
    q = TaskQueue(tmp_db)
    context = {"reviewed_sha": "x"}
    task = TaskRequest(task_id="impl-nomutate", task_type="implement",
                       description="Implement #272: hands off", branch="main", context=context)
    q.enqueue(task)
    assert "issue" not in context and "issue" not in task.context
