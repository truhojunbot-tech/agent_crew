"""#929: successor semantics survive opaque task and gate identifiers."""

import json
import sqlite3
import time

import pytest

from agent_crew import context_pack, pipeline
from agent_crew.protocol import GateRequest, TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


def test_context_pack_uses_fix_round_field_not_id():
    items = [
        context_pack.Artifact("issue", "issue://1", context_pack.TYPE_ISSUE),
        context_pack.Artifact("ac", "issue://1#ac", context_pack.TYPE_AC),
    ]
    named_fix = context_pack.ContextPack("fix-looking-id", "implementer", "lexical", items=items)
    opaque_fix = context_pack.ContextPack("opaque-17", "implementer", "lexical", items=items)
    assert context_pack.is_sufficient(named_fix, task_type="implement").ok
    assert not context_pack.is_sufficient(opaque_fix, task_type="implement", fix_round=2).ok


def test_opaque_tier3_gate_resumes_its_declared_target(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.create_gate(GateRequest(id="opaque-gate", type="approval", message="approve",
                                  kind="test", target_task_id="review-opaque"))
    queue.resolve_gate("opaque-gate", approved=True)
    monkeypatch.setattr(pipeline, "auto_enqueue_test", lambda q, task_id, **kw: task_id)
    assert pipeline.resume_tier3_gate(queue, "opaque-gate") == "review-opaque"


def test_opaque_fix_round_carries_prior_review_findings(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="review-before", task_type="review",
                              description="review", branch="feat/x",
                              context={"risk_tier": 1, "pr_number": 1}))
    queue.submit_result("review-before", TaskResult(
        task_id="review-before", status="completed", summary="changes needed",
        verdict="request_changes", findings=["HIGH src/x.py:1 - fix the edge case"],
    ))
    queue.enqueue(TaskRequest(task_id="opaque-fix", task_type="implement",
                              description="fix", branch="feat/x",
                              context={"risk_tier": 1, "fix_round": 2,
                                       "prev_task_id": "review-before", "pr_number": 1}))
    review_id = pipeline.auto_enqueue_review(queue, "opaque-fix", pr_number=1,
                                             pr_state_fn=lambda *_: "open")
    assert review_id is not None
    instructions = queue.get_task(review_id).context["instructions"]
    assert "PRIOR REVIEW FINDINGS" in instructions
    assert "fix the edge case" in instructions


def test_legacy_metadata_backfill_is_idempotent(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    for task_id in ("fix-review-x-r2", "retry-impl-x-a3",
                    "retry-retry-impl-x-a1-a2"):
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                  description="old", branch="feat/x",
                                  context={"risk_tier": 1}))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET context = ? WHERE task_id = ?",
                     (json.dumps({"original_task_id": "retry-impl-x-a1"}),
                      "retry-retry-impl-x-a1-a2"))
        conn.execute("DELETE FROM task_metadata_migrations WHERE name = 'opaque_task_ids_v1'")
        conn.execute("INSERT INTO gates (id, type, message, status, created_at) "
                     "VALUES ('risk-tier3-test-review-x', 'approval', 'old', 'approved', ?)",
                     (time.time(),))
    queue = TaskQueue(db)
    assert queue.get_task("fix-review-x-r2").context["fix_round"] == 2
    assert queue.get_task("retry-impl-x-a3").context["retry_of"] == "impl-x"
    nested = queue.get_task("retry-retry-impl-x-a1-a2").context
    assert (nested["retry_of"], nested["lineage_root_task_id"]) == (
        "retry-impl-x-a1", "impl-x")
    gate = next(g for g in queue.list_gates() if g.id == "risk-tier3-test-review-x")
    assert (gate.kind, gate.target_task_id) == ("test", "review-x")
    before = json.dumps(queue.get_task("fix-review-x-r2").context, sort_keys=True)
    queue = TaskQueue(db)
    assert json.dumps(queue.get_task("fix-review-x-r2").context, sort_keys=True) == before


def test_legacy_gate_table_gets_kind_and_target_columns(tmp_path):
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE gates (id TEXT PRIMARY KEY, type TEXT NOT NULL, "
                     "message TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL)")
        conn.execute("INSERT INTO gates VALUES (?, 'approval', 'old', 'approved', ?)",
                     ("risk-tier3-impl-old", time.time()))
    queue = TaskQueue(db)
    gate = queue.list_gates()[0]
    assert (gate.kind, gate.target_task_id) == ("review", "impl-old")


@pytest.mark.parametrize("ingress,context", [
    ("cascade.fix", {"prev_task_id": "review-x"}),
    ("retry.failed_task", {"original_task_id": "impl-x"}),
])
def test_successor_without_required_metadata_is_rejected(tmp_path, ingress, context):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task = TaskRequest(task_id="opaque", task_type="implement", description="work",
                       branch="feat/x", context={"risk_tier": 1, **context})
    with pytest.raises(ValueError):
        queue.enqueue(task, ingress=ingress,
                      _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
