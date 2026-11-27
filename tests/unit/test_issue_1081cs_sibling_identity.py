"""Real admission and cascade regressions for asymmetric sibling identity."""
import pytest

from agent_crew.pipeline import auto_enqueue_fix
from agent_crew.lineage_human_gate import durable_cap_refusal
from tests.unit.test_issue_1081cr_lineage_admission import (
    REPO, q, task, review, finish,
)


@pytest.fixture
def ordinary_review(q):
    q.enqueue(task('root', fix_round_grant=1))
    review(q, 'root', fix_round_grant=1)
    return 'review'


def auto(q, parent='review'):
    return auto_enqueue_fix(q, parent, repo=REPO, pr_state_fn=lambda _: 'open',
                            coordinator_notify_fn=lambda _: True)


def manual(name='manual', **context):
    return task(name, prev_task_id='review', fix_round_grant=1,
                **{'pr_number': None, **context})


def refused(q, request, survivor):
    from agent_crew.lineage_human_gate import LineageSiblingActiveError
    with pytest.raises(LineageSiblingActiveError) as exc:
        q.enqueue(request)
    assert exc.value.code == 'LINEAGE_SIBLING_ACTIVE'
    assert exc.value.admitted_task_id == survivor
    assert q.get_task(request.task_id) is None
    with q._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM task_exec_events WHERE task_id=? "
            "AND event='lineage_sibling_refused' "
            "AND json_extract(fields, '$.existing_task_id')=?",
            (request.task_id, survivor),
        ).fetchone()


@pytest.mark.parametrize('pr', [None, 1090])
def test_manual_then_auto(q, ordinary_review, pr):
    q.enqueue(manual(pr_number=pr))
    assert auto(q) is None
    assert q.get_task('review').context['lineage_terminal_reason'] == (
        'successor_refused: sibling_active:manual')
    with q._connect() as conn:
        assert durable_cap_refusal(conn, 'review') is None
        assert conn.execute("SELECT count(*) FROM tasks WHERE task_type='implement'").fetchone()[0] == 2
    finish(q, 'manual')
    assert auto(q) is not None


@pytest.mark.parametrize('pr', [None, 1090])
def test_auto_then_manual(q, ordinary_review, pr):
    survivor = auto(q)
    assert survivor
    refused(q, manual(pr_number=pr), survivor)
    finish(q, survivor)
    assert q.enqueue(manual(pr_number=pr)) == 'manual'


@pytest.mark.parametrize('status', ['pending', 'queued', 'waiting_pool', 'in_progress', 'claimed'])
def test_every_active_status(q, ordinary_review, status):
    q.enqueue(manual())
    with q._connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE task_id='manual'", (status,))
    refused(q, manual('second', pr_number=1090), 'manual')


@pytest.mark.parametrize('difference', ['pr', 'lineage', 'project', 'finding'])
def test_issue_alone_is_not_identity(q, ordinary_review, difference):
    q.enqueue(manual(pr_number=1090))
    request = task('unrelated', fix_round_grant=1, pr_number=2222,
                   lineage_root_task_id='other-root')
    if difference == 'lineage':
        request.context['pr_number'] = None
    elif difference == 'project':
        request.project = 'other'
        request.context.update(prev_task_id='review', pr_number=1090)
    elif difference == 'finding':
        q.enqueue(task('other-review', 'review', prev_task_id='root', pr_number=None))
        request.context.update(prev_task_id='other-review', lineage_root_task_id='root')
    assert q.enqueue(request) == 'unrelated'


def test_same_known_pr_without_links(q, ordinary_review):
    q.enqueue(manual(pr_number=1090))
    refused(q, task('same-pr', fix_round_grant=1), 'manual')


def test_explicit_root_and_chain_are_equivalent(q, ordinary_review):
    q.enqueue(manual(lineage_root_task_id='root'))
    refused(q, manual('chain-only', pr_number=1090), 'manual')


def test_concurrent_manual_and_auto_have_one_survivor(q, ordinary_review):
    from concurrent.futures import ThreadPoolExecutor
    from agent_crew.lineage_human_gate import LineageSiblingActiveError

    def enqueue_manual():
        try:
            return q.enqueue(manual())
        except LineageSiblingActiveError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        manual_result = pool.submit(enqueue_manual)
        auto_result = pool.submit(auto, q)
        manual_result, auto_result = manual_result.result(), auto_result.result()
    with q._connect() as conn:
        survivors = conn.execute(
            "SELECT task_id FROM tasks WHERE task_type='implement' AND status='pending'"
        ).fetchall()
    assert len(survivors) == 1
    survivor = survivors[0]["task_id"]
    if isinstance(manual_result, str):
        assert survivor == manual_result
        assert auto_result is None
    else:
        assert manual_result.admitted_task_id == auto_result == survivor
