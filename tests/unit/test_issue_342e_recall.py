"""#342: root recall applicability and exact predecessor decisions."""

import json
import sqlite3

from agent_crew.memory_runtime import MemoryRecord, MemoryScope, RuntimeMemoryProvider, SQLiteMemoryStorage
from agent_crew.memory_capture import task_lineage
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import _lineage_recall_observation
from tests.unit.test_issue_322_memory_provider import _dispatch_snapshot


def _evidence(queue, task_id):
    return json.loads(queue.get_tokenomics_shadow_receipt(task_id)["evidence_json"])


def test_root_dispatch_marks_recall_not_applicable_through_approve(
    tmp_path, monkeypatch, unused_tcp_port,
):
    case = tmp_path / "dispatch"
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    _dispatch_snapshot(case, monkeypatch, RuntimeMemoryProvider(storage),
                       unused_tcp_port=unused_tcp_port)
    queue = TaskQueue(str(case / "tasks.db"))
    dispatch_evidence = _evidence(queue, "shadow-322")
    assert dispatch_evidence["recall_not_applicable"] is True
    assert dispatch_evidence["required_context_recalled"] is None

    # The fake dispatched worker exits without a result, so complete a second
    # root through the ordinary queue result path to exercise evidence refresh.
    queue.enqueue(TaskRequest("root-done", "implement", "root", project="project-a"))
    queue.record_attribution("root-done")
    queue.record_required_context_recalled("root-done", None, recall_not_applicable=True)
    queue.submit_result("root-done", TaskResult("root-done", "completed", "done"))
    queue.enqueue(TaskRequest("review-root", "review", "review", project="project-a",
                              context={"prev_task_id": "root-done"}))
    queue.submit_result("review-root", TaskResult("review-root", "completed", "approved",
                                                   verdict="approve"))
    evidence = _evidence(queue, "root-done")
    assert evidence["recall_not_applicable"] is True
    assert evidence["required_context_recalled"] is None
    assert evidence["independent_review_correct"] is True


def test_predecessor_receipt_keeps_existing_evidence_shape(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("fix", "implement", "fix", project="agent_crew",
                              context={"prev_task_id": "prior"}))
    queue.record_attribution("fix")
    queue.record_required_context_recalled("fix", False)
    queue.submit_result("fix", TaskResult("fix", "completed", "done"))
    evidence = _evidence(queue, "fix")
    assert evidence["required_context_recalled"] is False
    assert "recall_not_applicable" not in evidence


def test_exact_decision_survives_attribution_scope_change(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    queue.enqueue(TaskRequest("prior", "review", "review", project="agent_crew",
                              context={"issue": 813}))
    written_scope = MemoryScope(project="agent_crew", task_id="prior", issue="813",
                                worktree="/old/claude", provider_session="session-old",
                                context_generation=261)
    storage.put(MemoryRecord("decision", "task:prior:decision", {"task_id": "prior"},
                             written_scope))
    queue.record_attribution("prior", project="agent_crew", worktree_path="/new/claude",
                             provider_session_id="", context_generation=262)
    assert _lineage_recall_observation(storage, queue.db_path, "agent_crew",
                                       ("prior",)) is True
    assert _lineage_recall_observation(storage, queue.db_path, "agent_crew",
                                       ("prior", "missing")) is False


def test_superseded_decision_does_not_count_as_recalled(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    queue.enqueue(TaskRequest("prior", "review", "review", project="agent_crew"))
    storage.put(MemoryRecord("decision", "task:prior:decision", {"task_id": "prior"},
                             MemoryScope(project="agent_crew", task_id="prior")))
    with sqlite3.connect(storage.path) as conn:
        conn.execute("ALTER TABLE adr001_memory ADD COLUMN superseded_by TEXT")
        conn.execute("UPDATE adr001_memory SET superseded_by='later' WHERE key='task:prior:decision'")
    assert _lineage_recall_observation(storage, queue.db_path, "agent_crew",
                                       ("prior",)) is False


def test_same_pr_history_is_not_required_causal_recall(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    queue.enqueue(TaskRequest("older-review", "review", "older", project="agent_crew",
                              pr_number=814, context={"pr_number": 814}))
    queue.submit_result("older-review", TaskResult("older-review", "completed", "done"))
    queue.enqueue(TaskRequest("impl", "implement", "impl", project="agent_crew",
                              pr_number=814, context={"pr_number": 814}))
    queue.submit_result("impl", TaskResult("impl", "completed", "done"))
    queue.enqueue(TaskRequest("review", "review", "review", project="agent_crew",
                              pr_number=814,
                              context={"prev_task_id": "impl", "pr_number": 814}))
    queue.submit_result("review", TaskResult("review", "completed", "approved",
                                              verdict="approve"))
    queue.enqueue(TaskRequest("fix", "implement", "fix", project="agent_crew",
                              pr_number=814,
                              context={"prev_task_id": "review", "pr_number": 814}))
    for task_id in ("impl", "review"):
        storage.put(MemoryRecord("decision", f"task:{task_id}:decision",
                                 {"task_id": task_id},
                                 MemoryScope(project="agent_crew", task_id=task_id)))

    broad, _ = task_lineage(queue.db_path, "fix")
    causal, _ = task_lineage(queue.db_path, "fix", include_same_pr=False)
    assert broad == ("review", "impl", "older-review")
    assert causal == ("review", "impl")
    assert _lineage_recall_observation(storage, queue.db_path, "agent_crew", causal) is True
    assert _lineage_recall_observation(storage, queue.db_path, "agent_crew", broad) is False
    assert _lineage_recall_observation(storage, queue.db_path, "agent_crew",
                                       ("review", "missing")) is False
