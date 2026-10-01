"""#342: cascade attribution keeps the root's declared risk without hiding escalation."""

from agent_crew.pipeline import (auto_enqueue_fix, auto_enqueue_review,
                                 auto_enqueue_test, auto_fallback_failed_task,
                                 successor_context)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


ROUTINE = {
    "safety_or_live_change": False,
    "broad_architecture_change": False,
    "bounded_routine_fix": True,
    "human_gate_required": False,
}


def _root(queue, *, declared=True):
    queue.enqueue(TaskRequest(
        "risk-root", "implement", "implement the task", branch="feature/risk",
        context={"risk_declaration": ROUTINE} if declared else {},
    ))
    queue.submit_result("risk-root", TaskResult(
        "risk-root", "completed", "done", pr_number=51))


def _review(queue):
    review_id = auto_enqueue_review(
        queue, "risk-root", pr_number=51, pr_state_fn=lambda _: "open")
    assert review_id
    return review_id


def _attribution(queue, task_id):
    queue.record_attribution(task_id)
    return queue.get_attribution(task_id)


def test_review_and_fix_inherit_explicit_root_declaration(tmp_db):
    queue = TaskQueue(tmp_db)
    _root(queue)
    review_id = _review(queue)
    review = _attribution(queue, review_id)
    assert review["risk_declaration_source"] == "explicit"
    assert review["risk_declaration_confidence"] == "high"
    assert review["bounded_routine_fix"] == 1
    assert review["safety_or_live_change"] == 0
    assert queue.get_task_context(review_id)["risk_declaration"]["inherited_from"] == "risk-root"

    queue.submit_result(review_id, TaskResult(
        review_id, "completed", "fix it", verdict="request_changes",
        findings=["Add the missing test"], pr_number=51))
    fix_id = auto_enqueue_fix(queue, review_id, pr_state_fn=lambda _: "open",
                              repo="owner/repo")
    assert fix_id
    fix = _attribution(queue, fix_id)
    assert fix["risk_declaration_source"] == "explicit"
    assert fix["risk_declaration_confidence"] == "high"
    assert fix["bounded_routine_fix"] == 1
    assert queue.get_task_context(fix_id)["risk_declaration"]["inherited_from"] == "risk-root"


def test_auto_fix_with_server_finding_keeps_root_risk_facts(tmp_db):
    """A filename in copied review feedback is not a new architecture declaration."""
    queue = TaskQueue(tmp_db)
    _root(queue)
    review_id = _review(queue)
    queue.submit_result(review_id, TaskResult(
        review_id, "completed", "The HTTP task route needs an identity check.",
        verdict="request_changes",
        findings=["src/agent_crew/server.py:6376: reject a mismatched project"],
        pr_number=51))
    fix_id = auto_enqueue_fix(queue, review_id, pr_state_fn=lambda _: "open",
                              repo="owner/repo")
    assert fix_id
    fix = _attribution(queue, fix_id)
    assert fix["risk_declaration_source"] == "explicit"
    assert fix["risk_declaration_confidence"] == "high"
    assert fix["broad_architecture_change"] == 0
    assert queue.get_task_context(fix_id)["risk_declaration"]["inherited_from"] == "risk-root"


def test_test_task_inherits_explicit_root_declaration(tmp_db):
    queue = TaskQueue(tmp_db)
    _root(queue)
    review_id = _review(queue)
    queue.submit_result(review_id, TaskResult(
        review_id, "completed", "approved", verdict="approve", pr_number=51))
    test_id = auto_enqueue_test(queue, review_id, pr_state_fn=lambda _: "open")
    assert test_id
    attribution = _attribution(queue, test_id)
    assert attribution["risk_declaration_source"] == "explicit"
    assert attribution["risk_declaration_confidence"] == "high"
    assert attribution["bounded_routine_fix"] == 1
    assert queue.get_task_context(test_id)["risk_declaration"]["inherited_from"] == "risk-root"


def test_root_without_explicit_declaration_does_not_inherit(tmp_db):
    queue = TaskQueue(tmp_db)
    _root(queue, declared=False)
    review_id = _review(queue)
    attribution = _attribution(queue, review_id)
    assert attribution["risk_declaration_source"] == "unknown"
    assert attribution["bounded_routine_fix"] is None

    queue.submit_result(review_id, TaskResult(
        review_id, "completed", "fix the missing test",
        verdict="request_changes", findings=["Add the missing test"], pr_number=51))
    fix_id = auto_enqueue_fix(queue, review_id, pr_state_fn=lambda _: "open",
                              repo="owner/repo")
    assert fix_id
    assert _attribution(queue, fix_id)["risk_declaration_source"] != "explicit"


def test_retry_successor_preserves_root_explicit_declaration(tmp_db):
    """The server retry path uses successor_context before re-admission."""
    queue = TaskQueue(tmp_db)
    _root(queue)
    retry_context = successor_context(queue.get_task_context("risk-root"))
    retry_context["original_task_id"] = "risk-root"
    queue.enqueue(TaskRequest(
        "retry-risk-root-a1", "implement", "retry the task", branch="feature/risk",
        context=retry_context,
    ))
    retry = _attribution(queue, "retry-risk-root-a1")
    assert retry["risk_declaration_source"] == "explicit"
    assert retry["risk_declaration_confidence"] == "high"
    assert retry["bounded_routine_fix"] == 1


def test_risky_descendant_heuristic_overrides_inherited_routine(tmp_db):
    queue = TaskQueue(tmp_db)
    _root(queue)
    review_id = _review(queue)
    queue.submit_result(review_id, TaskResult(
        review_id, "completed", "fix it", verdict="request_changes",
        findings=["Deploy the changed service"], pr_number=51))
    fix_id = auto_enqueue_fix(queue, review_id, pr_state_fn=lambda _: "open",
                              repo="owner/repo")
    assert fix_id
    attribution = _attribution(queue, fix_id)
    assert attribution["safety_or_live_change"] == 1
    assert attribution["human_gate_required"] == 1
    assert attribution["risk_declaration_source"] == "heuristic"


def test_fallback_successor_context_marks_root_declaration_inherited(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        "risk-root", "implement", "implement the task", branch="feature/risk",
        context={"risk_declaration": ROUTINE},
    ))
    handled = auto_fallback_failed_task(
        queue, "risk-root", TaskResult("risk-root", "failed", "usage limit hit"),
        "implement", pane_map={"implementer": "%91", "claude": "%91",
                                "codex": "%92", "gemini": "%93"})
    assert handled
    fallback = next(t for t in queue.list_tasks() if t.task_id.startswith("fallback-"))
    attribution = _attribution(queue, fallback.task_id)
    assert attribution["risk_declaration_source"] == "explicit"
    assert attribution["risk_declaration_confidence"] == "high"
    assert queue.get_task_context(fallback.task_id)["risk_declaration"]["inherited_from"] == "risk-root"
