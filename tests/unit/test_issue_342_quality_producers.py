"""#342 quota-core #80 quality evidence must come from direct observations."""
import json
import sqlite3

from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import _lineage_recall_observation


def _evidence(queue, task_id):
    return json.loads(queue.get_tokenomics_shadow_receipt(task_id)['evidence_json'])


def test_exact_lineage_key_wins_when_ranked_result_omits_it(tmp_path):
    queue = TaskQueue(str(tmp_path / 'tasks.db'))
    queue.enqueue(TaskRequest('pred', 'implement', 'prior', project='agent_crew'))
    storage = SQLiteMemoryStorage(str(tmp_path / 'memory.db'))
    storage.put(MemoryRecord('decision', 'task:pred:decision', {'task_id': 'pred'},
                             MemoryScope(project='agent_crew', task_id='pred')))
    queue.enqueue(TaskRequest('scoped', 'implement', 'prior', project='agent_crew',
                              context={'issue': 342}))
    queue.record_attribution('scoped', worktree_path='/worktree',
                             provider_session_id='session-1', context_generation=2)
    storage.put(MemoryRecord('decision', 'task:scoped:decision', {'task_id': 'scoped'},
                             MemoryScope(project='agent_crew', task_id='scoped',
                                         issue='342', worktree='/worktree',
                                         provider_session='session-1', context_generation=2)))
    assert _lineage_recall_observation(storage, queue.db_path, 'agent_crew', ('pred',)) is True
    assert _lineage_recall_observation(storage, queue.db_path, 'agent_crew',
                                      ('pred', 'scoped')) is True
    assert _lineage_recall_observation(storage, queue.db_path, 'agent_crew', ('missing',)) is False
    assert _lineage_recall_observation(None, queue.db_path, 'agent_crew', ('pred',)) is None

    def broken(*_args, **_kwargs):
        raise OSError('store unavailable')
    storage.has_active_task_decision = broken
    assert _lineage_recall_observation(storage, queue.db_path, 'agent_crew', ('pred',)) is None


def _task(queue, task_id, task_type, *, parent=None):
    queue.enqueue(TaskRequest(task_id, task_type, task_id, project='agent_crew',
                              context={'prev_task_id': parent} if parent else {}))
    queue.record_attribution(task_id)


def _result(queue, task_id, *, verdict=None, status='completed'):
    queue.submit_result(task_id, TaskResult(task_id, status, 'done', verdict=verdict))


def test_review_verdict_updates_only_reviewed_receipt_and_survives_refresh(tmp_path):
    queue = TaskQueue(str(tmp_path / 'tasks.db'))
    _task(queue, 'impl', 'implement')
    _result(queue, 'impl')
    _task(queue, 'review-1', 'review', parent='impl')
    def before_commit(conn, _gate, _now):
        row = conn.execute("SELECT evidence_json FROM tokenomics_shadow_receipts "
                           "WHERE task_id='impl'").fetchone()
        assert json.loads(row[0])['independent_review_correct'] is False
    queue.submit_result('review-1', TaskResult('review-1', 'completed', 'done',
                                               verdict='request_changes'),
                        before_commit=before_commit)
    assert _evidence(queue, 'impl')['independent_review_correct'] is False
    assert _evidence(queue, 'review-1')['independent_review_correct'] is None

    _task(queue, 'fix', 'implement', parent='review-1')
    _result(queue, 'fix')
    _task(queue, 'review-2', 'review', parent='fix')
    _result(queue, 'review-2', verdict='approve')
    assert _evidence(queue, 'fix')['independent_review_correct'] is True
    assert _evidence(queue, 'impl')['independent_review_correct'] is False
    queue._refresh_shadow_after_commit('fix')
    assert _evidence(queue, 'fix')['independent_review_correct'] is True

    _task(queue, 'review-3', 'review', parent='impl')
    _result(queue, 'review-3', verdict='approve')
    assert _evidence(queue, 'impl')['independent_review_correct'] is True
    _task(queue, 'review-older', 'review', parent='impl')
    _task(queue, 'review-newer', 'review', parent='impl')
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE tasks SET created_at=10000000000 WHERE task_id='review-older'")
        conn.execute("UPDATE tasks SET created_at=10000000001 WHERE task_id='review-newer'")
    _result(queue, 'review-newer', verdict='rejected')
    _result(queue, 'review-older', verdict='approved')
    assert _evidence(queue, 'impl')['independent_review_correct'] is False


def test_nonreview_verdict_and_self_link_do_not_write_quality(tmp_path):
    queue = TaskQueue(str(tmp_path / 'tasks.db'))
    _task(queue, 'impl', 'implement')
    _result(queue, 'impl')
    _task(queue, 'other', 'implement', parent='impl')
    _result(queue, 'other', verdict='approve')
    assert _evidence(queue, 'impl')['independent_review_correct'] is None
    _task(queue, 'self', 'review', parent='self')
    _result(queue, 'self', verdict='approve')
    assert _evidence(queue, 'self')['independent_review_correct'] is None
    _task(queue, 'review-unknown', 'review', parent='impl')
    _result(queue, 'review-unknown', verdict='unknown')
    assert _evidence(queue, 'impl')['independent_review_correct'] is None
