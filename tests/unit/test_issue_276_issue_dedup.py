"""Issue deduplication uses declared task context, not description text (#928)."""

import json

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, task_issue_number
from agent_crew.watch import active_issue_numbers, select_candidates, tasks_by_issue

#: Former parser hits; all are ignored without declared context.
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


# ── 1. prose is not identity ──────────────────────────────────────────


@pytest.mark.parametrize("description, expected", CORPUS_HITS)
def test_an_implement_description_does_not_name_its_issue(description, expected):
    task = TaskRequest(task_id="impl-prose", task_type="implement", description=description)
    assert task_issue_number(task) is None


@pytest.mark.parametrize("description", CORPUS_MISSES)
def test_everything_else_names_nothing(description):
    """PR numbers and cross-repo references cannot suppress issue intake."""
    task = TaskRequest(task_id="impl-prose", task_type="implement", description=description)
    assert task_issue_number(task) is None


@pytest.mark.parametrize("value", [None, 0, 12, b"Implement #1"])
def test_a_non_string_description_is_not_parsed(value):
    assert task_issue_number(TaskRequest(task_id="impl-prose", task_type="implement",
                                         description=value)) is None


# ── 2. the read side ──────────────────────────────────────────────────


def _enqueue(q, task_id, description, *, context=None, project=""):
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement", description=description,
                          branch="main", context=context if context is not None else {},
                          project=project))


def test_a_description_only_task_does_not_suppress_an_issue(tmp_db):
    q = TaskQueue(tmp_db)
    _enqueue(q, "implement-4e08dbe4",
             "Implement #272: tester role duplicates full CI suite on the same host",
             context={"reviewed_sha": "c6e8211"})
    assert 272 not in active_issue_numbers(q)


def _strip_backfill(tmp_db, task_id, context):
    """Model a legacy row without a declared issue number."""
    import sqlite3

    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE tasks SET context = ? WHERE task_id = ?",
                 (json.dumps(context), task_id))
    conn.commit()
    conn.close()


def test_a_legacy_row_without_declared_issue_is_not_deduped(tmp_db):
    q = TaskQueue(tmp_db)
    _enqueue(q, "implement-legacy", "Implement #272: written before #276 shipped",
             context={"reviewed_sha": "c6e8211"})
    _strip_backfill(tmp_db, "implement-legacy", {"reviewed_sha": "c6e8211"})

    assert q.get_task_context("implement-legacy") == {"reviewed_sha": "c6e8211"}
    assert 272 not in active_issue_numbers(q)
    assert (tasks_by_issue(q) or {}).get(272) is None


def test_the_watcher_will_not_offer_an_issue_that_is_already_in_flight(tmp_db):
    """★★End to end through the selection the watcher actually calls."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "implement-4e08dbe4", "Implement #272: tester role duplicates the suite",
             context={"issue": 272, "reviewed_sha": "c6e8211"})

    offered = select_candidates([{"number": 272, "labels": []}, {"number": 999, "labels": []}],
                                claimed=set(), active_issues=active_issue_numbers(q))
    assert [i["number"] for i in offered] == [999]


def test_a_finished_task_stops_suppressing_its_issue(tmp_db):
    """Completed declared work no longer occupies its issue."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-done", "Implement #272: done already", context={"issue": 272})
    q.submit_result("impl-done", TaskResult(task_id="impl-done", status="completed", summary="s"))
    assert 272 not in active_issue_numbers(q)


def test_the_structured_field_still_wins_when_both_are_present(tmp_db):
    """The declared issue wins over a different number in prose."""
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-both", "Implement #272: mismatched on purpose", context={"issue": 900})
    active = active_issue_numbers(q)
    assert 900 in active and 272 not in active


def test_tasks_by_issue_uses_the_declared_issue(tmp_db):
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-recon", "Implement #272: reconcile me", context={"issue": 272})
    assert (tasks_by_issue(q) or {}).get(272) == "impl-recon"


# ── 3. the write side ─────────────────────────────────────────────────


def test_enqueue_records_declared_issue_aliases(tmp_db):
    q = TaskQueue(tmp_db)
    _enqueue(q, "impl-backfill", "Implement #272: backfilled",
             context={"issue_number": 272, "reviewed_sha": "x"})
    stored = q.get_task_context("impl-backfill")
    assert stored["issue"] == 272
    assert stored["issue_number"] == 272
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
    q = TaskQueue(tmp_db)
    context = {"issue_number": 272, "reviewed_sha": "x"}
    task = TaskRequest(task_id="impl-nomutate", task_type="implement",
                       description="Implement #272: hands off", branch="main", context=context)
    q.enqueue(task)
    assert "issue" not in context and "issue" not in task.context
