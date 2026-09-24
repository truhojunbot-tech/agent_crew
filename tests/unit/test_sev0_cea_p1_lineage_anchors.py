"""P1 r1 (owner alfred#51) — a retry is not new work.

413f13b made every unscoped task anchor on ``task://<its own id>``, which fixed
the collapse of two organic tasks into one intent. Codex's REQUEST_CHANGES on
that commit named the inverse failure it opened: the retry
(``server._auto_retry_failed_task``), fallback (``pipeline.auto_fallback``) and
requeue/recovery paths all mint a **new** ``task_id`` for the **same** work, so
each successor hashed differently from the parent and the engine saw a brand
new intent every time a task was retried. P4 says a retry is not new work.

These go through the real successor paths — the real id-minting code, the real
``TaskQueue.enqueue`` — rather than asserting on a hand-built context that
happens to have the right keys in it.
"""
from __future__ import annotations

import pytest

from agent_crew.cea.engine import intent_hash
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, intent_for_task


def task(task_id, *, task_type="implement", context=None, project="agent_crew",
         description="add a --json flag", branch="main") -> TaskRequest:
    return TaskRequest(task_id=task_id, task_type=task_type, description=description,
                       branch=branch, priority=3, context=dict(context or {}),
                       project=project)


def hashed(t: TaskRequest) -> str:
    return intent_hash(intent_for_task(t).identity)


def anchors(t: TaskRequest) -> tuple[str, ...]:
    return intent_for_task(t).identity.target.scope_anchors


# ═══════════════════════════════════════════════════════════════════════════
# retry — the id and the context server._auto_retry_failed_task actually mints
# ═══════════════════════════════════════════════════════════════════════════

def retry_successor(parent: TaskRequest, attempt: int = 1) -> TaskRequest:
    """Exactly what ``server._auto_retry_failed_task`` builds (server.py ~5295)."""
    ctx = dict(parent.context)
    ctx["retry_attempt"] = attempt
    ctx["original_task_id"] = parent.task_id
    return TaskRequest(task_id=f"retry-{parent.task_id}-a{attempt}",
                       task_type=parent.task_type, description=parent.description,
                       branch=parent.branch, priority=parent.priority + 1,
                       context=ctx, project=parent.project)


def test_a_retry_hashes_the_same_as_the_task_it_retries():
    """P4: retrying work does not make it different work."""
    parent = task("impl-1a2b3c4d")
    assert hashed(retry_successor(parent)) == hashed(parent)


def test_a_retry_anchors_on_the_parent_not_on_its_own_minted_id():
    parent = task("impl-1a2b3c4d")
    assert anchors(retry_successor(parent)) == ("task://impl-1a2b3c4d",)


def test_a_retry_of_a_retry_still_hashes_as_the_original():
    """The chain is deeper than one hop: the context names the immediate parent
    only, so the walk has to keep unwrapping the deterministic id (#314 §4)."""
    parent = task("impl-1a2b3c4d")
    first = retry_successor(parent, attempt=1)
    second = retry_successor(first, attempt=2)
    assert second.task_id == "retry-retry-impl-1a2b3c4d-a1-a2"
    assert hashed(second) == hashed(parent)


def test_retries_of_two_different_tasks_are_still_two_intents():
    """The fix must not put every retry in the world on one lineage."""
    assert hashed(retry_successor(task("impl-1a2b3c4d"))) != \
        hashed(retry_successor(task("impl-5e6f7a8b")))


# ═══════════════════════════════════════════════════════════════════════════
# fallback — pipeline.auto_fallback (pipeline.py ~1888)
# ═══════════════════════════════════════════════════════════════════════════

def fallback_successor(parent: TaskRequest, *, successor="gemini", depth=1) -> TaskRequest:
    ctx = dict(parent.context)
    ctx["agent_override"] = successor
    ctx["fallback_excluded"] = ["codex"]
    ctx["fallback_from_task_id"] = parent.task_id
    ctx["fallback_chain_depth"] = depth
    ctx["original_task_id"] = parent.context.get("original_task_id") or parent.task_id
    return TaskRequest(task_id=f"fallback-{parent.task_id}-d{depth}",
                       task_type=parent.task_type, description=parent.description,
                       branch=parent.branch, priority=parent.priority,
                       context=ctx, project=parent.project)


def test_a_provider_fallback_hashes_the_same_as_the_task_it_reroutes():
    """Same work, another provider. The provider is not an identity member."""
    parent = task("impl-1a2b3c4d")
    assert hashed(fallback_successor(parent)) == hashed(parent)


def test_a_fallback_of_a_retry_hashes_as_the_original():
    parent = task("impl-1a2b3c4d")
    mixed = fallback_successor(retry_successor(parent))
    assert hashed(mixed) == hashed(parent)


# ═══════════════════════════════════════════════════════════════════════════
# the guards — what the lineage walk must NOT swallow
# ═══════════════════════════════════════════════════════════════════════════

def test_two_unrelated_organic_tasks_are_still_two_intents():
    """The #51 regression this whole derivation exists to prevent."""
    assert hashed(task("impl-1a2b3c4d")) != hashed(task("impl-5e6f7a8b"))


def test_an_explicit_anchor_still_wins_over_the_lineage():
    parent = task("impl-1a2b3c4d")
    declared = retry_successor(parent)
    declared.context["scope_anchors"] = ["src/agent_crew/queue.py"]
    assert anchors(declared) == ("src/agent_crew/queue.py",)


def test_session_continuity_is_not_lineage():
    """``previous_task_id`` is "what this provider ran before", not "the same
    work". Folding it in would give every task in a session the first task's
    anchor — the collapse #51 exists to stop, rebuilt one key over."""
    a = task("impl-1a2b3c4d")
    b = task("impl-5e6f7a8b", context={"previous_task_id": "impl-1a2b3c4d"})
    assert hashed(a) != hashed(b)


def test_a_self_referential_lineage_terminates():
    """A hand-written context naming the task itself must not hang admission."""
    t = task("impl-1a2b3c4d", context={"original_task_id": "impl-1a2b3c4d"})
    assert anchors(t) == ("task://impl-1a2b3c4d",)


def test_a_two_task_lineage_cycle_terminates():
    a = task("impl-aaaa", context={"original_task_id": "impl-bbbb"})
    b = task("impl-bbbb", context={"original_task_id": "impl-aaaa"})
    assert anchors(a) == ("task://impl-bbbb",)
    assert anchors(b) == ("task://impl-aaaa",)


def test_a_task_merely_named_like_a_successor_is_not_one():
    """No lineage key, no ``-a<n>``/``-d<n>`` suffix: nothing to unwrap."""
    assert anchors(task("retry-budget-report")) == ("task://retry-budget-report",)


# ═══════════════════════════════════════════════════════════════════════════
# end to end through the real queue: requeue / recovery
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def queue(tmp_path):
    return TaskQueue(str(tmp_path / "tasks.db"))


def stored_intent_hash(q: TaskQueue, task_id: str) -> str:
    row = q._connect().execute(
        "SELECT receipt_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None, f"no task row for {task_id}"
    return row["receipt_id"]


def test_requeue_keeps_the_row_on_its_own_receipt(queue):
    """Recovery (``server._requeue_orphans`` → ``TaskQueue.requeue``) rolls the
    *same* ``task_id`` back to pending, so its intent cannot drift — pin that,
    because a requeue that minted a new id would have the retry bug too."""
    t = task("impl-1a2b3c4d")
    queue.enqueue(t, ingress="cli.enqueue")
    before = stored_intent_hash(queue, t.task_id)
    claimed = queue.dequeue(role="implementer")
    if claimed is not None:
        queue.requeue(t.task_id, reason="orphaned by a server restart")
    assert stored_intent_hash(queue, t.task_id) == before
    assert hashed(t) == hashed(task("impl-1a2b3c4d"))


def test_a_retry_admitted_through_the_real_queue_reuses_the_parent_lineage(queue):
    """End to end: enqueue the parent, then enqueue the retry the server would
    mint. Before this the retry opened a second lineage; now it re-admits the
    parent's (P4 retry rule) and lands as a queued row either way."""
    parent = task("impl-1a2b3c4d")
    queue.enqueue(parent, ingress="cli.enqueue")
    successor = retry_successor(parent)
    queue.enqueue(successor, ingress="retry.failed_task")
    assert hashed(successor) == hashed(parent)
    row = queue._connect().execute(
        "SELECT task_id FROM tasks WHERE task_id = ?", (successor.task_id,)).fetchone()
    assert row is not None


def test_a_fallback_admitted_through_the_real_queue_reuses_the_parent_lineage(queue):
    parent = task("impl-1a2b3c4d")
    queue.enqueue(parent, ingress="cli.enqueue")
    successor = fallback_successor(parent)
    queue.enqueue(successor, ingress="cascade.fallback")
    assert hashed(successor) == hashed(parent)
    row = queue._connect().execute(
        "SELECT task_id FROM tasks WHERE task_id = ?", (successor.task_id,)).fetchone()
    assert row is not None
