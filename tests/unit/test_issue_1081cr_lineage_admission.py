"""Admission uses real result/cascade refusal, never result disposition guesses."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

REPO = 'truhojunbot-tech/agent_crew'
BASE = {'repo': REPO, 'issue': 1081, 'pr_number': 1090, 'risk_tier': 1,
        'crew_run_branch': True, 'fix_round_grant': 0}
GRANT = {'grant_kind': 'technical_extra_round', 'authority_version': '405-v1.1',
         'issue': f'{REPO}#1081', 'finding': 'c7207904048',
         'authorizer': 'coordinator', 'source': 'owner:17253'}


@pytest.fixture
def q(tmp_db, monkeypatch):
    monkeypatch.setenv('AGENT_CREW_REVIEW_FIX_MAX_ROUNDS', '1')
    monkeypatch.setenv('AGENT_CREW_CEA_MODE', 'off')
    monkeypatch.setattr(TaskQueue, '_resolve_review_head', lambda *a: '')
    return TaskQueue(tmp_db)


def task(name, kind='implement', **context):
    return TaskRequest(task_id=name, task_type=kind, description='bounded queue fix',
                       project='agent_crew', branch='agent/1081cr-test',
                       context={**BASE, **context})


def finish(q, name, verdict=None):
    q.submit_result(name, TaskResult(task_id=name, status='completed', summary='result',
                                    pr_number=1090, verdict=verdict,
                                    findings=['HIGH queue.py:1 - finding'] if verdict == 'request_changes' else []))


def review(q, parent, name='review', verdict='request_changes', **context):
    finish(q, parent)
    q.enqueue(task(name, 'review', prev_task_id=parent, **context))
    finish(q, name, verdict)
    return name


def refuse(q, name):
    assert auto_enqueue_fix(q, name, repo=REPO, pr_state_fn=lambda _: 'open',
                            comment_fn=lambda *a: None,
                            coordinator_notify_fn=lambda _: True) is None
    assert q.get_task(name).context['lineage_terminal_reason'].startswith('successor_refused: fix_')


def extra(name, parent='review', finding='c7207904048', **context):
    return task(name, prev_task_id=parent,
                technical_extra_round={**GRANT, 'finding': finding}, **context)


def exhausted(q, **context):
    q.enqueue(task('root', **context))
    review(q, 'root')
    refuse(q, 'review')


def denied(q, request, reason=None):
    from agent_crew.lineage_human_gate import LineageHumanGateError
    with pytest.raises(LineageHumanGateError, match=reason or 'LINEAGE_HUMAN_GATE') as e:
        q.enqueue(request)
    assert q.get_task(request.task_id) is None
    return e.value


def test_committed_review_waits_for_cascade_refusal(q):
    q.enqueue(task('root'))
    review(q, 'root')
    denied(q, extra('too-early'), 'durable_refusal_required')
    refuse(q, 'review')
    assert q.enqueue(extra('extra1')) == 'extra1'
    error = denied(q, extra('duplicate', finding='c222'), 'review_already_used')
    assert error.admitted_task_id == 'extra1'


def test_two_rounds_require_their_own_refused_review(q):
    exhausted(q)
    q.enqueue(extra('extra1'))
    review(q, 'extra1', 'review2')
    denied(q, extra('early2', 'review2', 'c222'), 'durable_refusal_required')
    refuse(q, 'review2')
    error = denied(q, extra('same', 'review2', 'issuecomment-7207904048'), 'finding_reused')
    assert error.admitted_task_id == 'extra1'
    q.enqueue(extra('extra2', 'review2', 'c222'))
    review(q, 'extra2', 'review3')
    refuse(q, 'review3')
    denied(q, extra('third', 'review3', 'c333'), 'extra_rounds_exhausted')
    events = [e for e in q.get_exec_state('root')['events'] if e['event'] == 'lineage_extra_round_admitted']
    assert len(events) == 2


def test_approve_result_and_tester_never_authorize_extra(q):
    exhausted(q)
    q.enqueue(extra('extra1'))
    review(q, 'extra1', 'approved', verdict='approve')
    denied(q, extra('race', 'approved', 'c222'), 'durable_refusal_required')
    tester = auto_enqueue_test(q, 'approved', repo=REPO, pr_state_fn=lambda _: 'open')
    assert tester is not None
    denied(q, extra('after-test', 'approved', 'c222'), 'durable_refusal_required')


@pytest.mark.parametrize('context', [
    {'capability_tags': ['orders']}, {'capability_tags': ['risk']},
    {'capability_tags': ['caps']}, {'capability_tags': ['credentials']},
    {'capability_tags': ['payments']}, {'capability_tags': ['external_publishing']},
    {'capability_tags': ['irreversible_removal']}, {'capability_tags': ['unknown']},
    {'risk_tier': None}, {'owner_gate': 'HUMAN_GATE'}, {'owner_gate': 'HOLD'},
    {'owner_gate': 'STOP'}, {'owner_gate': 'PARK'},
])
def test_excluded_unknown_and_owner_gated_lineages(q, context):
    exhausted(q, **context)
    denied(q, extra('blocked'))


@pytest.mark.parametrize('field', list(GRANT))
def test_missing_and_malformed_authority(q, field):
    exhausted(q)
    grant = dict(GRANT)
    del grant[field]
    denied(q, task('missing', prev_task_id='review', technical_extra_round=grant))
    grant[field] = 'invalid'
    denied(q, task('malformed', prev_task_id='review', technical_extra_round=grant))


def test_fresh_id_and_issue_only_cannot_reset_gate(q):
    exhausted(q)
    denied(q, task('fresh'))
    denied(q, task('new-pr', pr_number=2222))
    assert q.enqueue(task('unrelated', issue=2222, pr_number=3333)) == 'unrelated'


def test_caller_marker_is_not_cascade_authority(q):
    q.enqueue(task('root'))
    review(q, 'root', lineage_terminal_reason='successor_refused: fix_grant_exhausted')
    denied(q, extra('forged'), 'durable_refusal_required')


def test_normal_unexhausted_lineage_unchanged(q):
    q.enqueue(task('root', fix_round_grant=1))
    review(q, 'root', fix_round_grant=1)
    assert auto_enqueue_fix(q, 'review', repo=REPO, pr_state_fn=lambda _: 'open') is not None


def test_concurrent_admission_serializes_and_reports_survivor(q):
    exhausted(q)
    def attempt(name):
        try:
            return q.enqueue(extra(name, finding='c111' if name == 'a' else 'c222'))
        except Exception as e:
            return e
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['a', 'b']))
    winner = next(x for x in results if isinstance(x, str))
    error = next(x for x in results if not isinstance(x, str))
    assert error.code == 'LINEAGE_HUMAN_GATE'
    assert error.admitted_task_id == winner


def test_http_grant_schema_and_typed_conflict(q):
    exhausted(q)
    with TestClient(create_app(str(q.db_path), project='agent_crew')) as client:
        request = extra('http-extra')
        response = client.post('/tasks', json=request.__dict__,
                               headers={'X-Agent-Crew-Project': 'agent_crew'})
        assert response.status_code == 201, response.text
        request = extra('http-second')
        response = client.post('/tasks', json=request.__dict__,
                               headers={'X-Agent-Crew-Project': 'agent_crew'})
        assert response.status_code == 409, response.text
        assert response.json()['detail']['error'] == 'LINEAGE_HUMAN_GATE'
        assert response.json()['detail']['admitted_task_id'] == 'http-extra'


def test_port_cap_also_produces_durable_admission_authority(q):
    q.enqueue(task('root', fix_round_grant=10))
    review(q, 'root', fix_round_grant=10, fix_round=1)
    refuse(q, 'review')
    assert q.get_task('review').context['lineage_terminal_reason'] == 'successor_refused: fix_rounds_exhausted'
    q.enqueue(extra('extra'))


def test_sibling_refusal_cannot_start_parallel_second_round(q):
    q.enqueue(task('root'))
    review(q, 'root')
    refuse(q, 'review')
    q.enqueue(task('sibling', 'review', prev_task_id='root'))
    finish(q, 'sibling', 'request_changes')
    refuse(q, 'sibling')
    q.enqueue(extra('extra1'))
    denied(q, extra('parallel', 'sibling', 'c222'), 'review_not_from_previous_round')


def test_preexisting_child_blocks_grant_even_without_admission_event(q):
    q.enqueue(task('root'))
    review(q, 'root')
    q.enqueue(task('existing-child', prev_task_id='review'))
    refuse(q, 'review')
    error = denied(q, extra('duplicate'), 'review_already_used')
    assert error.admitted_task_id == 'existing-child'


def test_admission_event_rolls_back_if_task_insert_fails(q):
    from agent_crew.queue import TaskAlreadyExistsError
    exhausted(q)
    request = extra('root')
    receipt = q.authorize_task(request).receipt
    with pytest.raises(TaskAlreadyExistsError):
        q.enqueue_with_receipt(request, receipt)
    assert not any(e['event'] == 'lineage_extra_round_admitted'
                   for e in q.get_exec_state('root')['events'])
    q.enqueue(extra('valid'))


def test_old_root_is_not_retroactively_authorized(q, monkeypatch):
    from agent_crew.lineage_human_gate import _TECHNICAL_AUTHORITY_START
    with monkeypatch.context() as patch:
        patch.setattr('agent_crew.queue.time.time', lambda: _TECHNICAL_AUTHORITY_START - 1)
        q.enqueue(task('root'))
    review(q, 'root')
    refuse(q, 'review')
    denied(q, extra('retroactive'), 'nonretroactive_lineage')


def test_unsigned_owner_claim_cannot_bypass_lineage_gate(q):
    from agent_crew.lineage_human_gate import LineageHumanGateError
    exhausted(q)
    request = task('unsigned-owner')
    receipt = q.authorize_task(request).receipt
    receipt['decision'] = 'ALLOW'
    receipt['reuse'] = {'approved_by': 'owner', 'approver_identity_verified': True}
    with pytest.raises(LineageHumanGateError):
        q.enqueue_with_receipt(request, receipt)


def test_unexhausted_legacy_reference_does_not_add_new_policy(q):
    assert q.enqueue(task('legacy', prev_task_id='missing-legacy-parent')) == 'legacy'


@pytest.mark.parametrize("mode", ["test", "shadow", "off"])
def test_verified_scoped_owner_receipt_can_admit(q, tmp_path, monkeypatch, mode):
    from agent_crew.cea.engine import EngineConfig
    from agent_crew.cea.receipt import DecisionRev, MatchedCapability
    from tests.unit.test_sev0_cea_engine import (
        DECISION, FakeSnapshot, FakeRegistry, FakeRuntime, FakeBudget, FakeGate,
    )
    exhausted(q)
    key = tmp_path / 'receipt.key'
    key.write_bytes(b'lineage-test-signing-key')
    capability = MatchedCapability(id='technical.queue', owner='owner', repo=REPO)
    approval = DecisionRev('OWNER-1081', 'c' * 32,
                           capabilities=('reuse:technical.queue',), projects=('agent_crew',))
    providers = {
        'snapshots': FakeSnapshot(decisions=(DECISION, approval), in_scope=(DECISION, approval)),
        'capabilities': FakeRegistry(matches=(capability,)),
        'runtime': FakeRuntime(), 'budgets': FakeBudget(), 'gates': FakeGate(),
    }
    owner_queue = TaskQueue(q.db_path, cea_config=EngineConfig(mode='test', key_path=str(key)),
                            cea_providers=providers)
    request = task('owner-approved', capability_id='technical.queue')
    auth = owner_queue.authorize_task(request)
    assert auth.receipt['decision'] in ('ALLOW', 'REVIEW'), auth.receipt['reason']
    assert auth.receipt['reuse']['approver_identity_verified'] is True
    assert owner_queue.cea_engine('agent_crew').verify(auth.receipt)
    from agent_crew.cea import callsites
    from unittest.mock import patch
    monkeypatch.setattr(owner_queue, '_cea_config_override',
                        EngineConfig(mode=mode, key_path=str(key)))
    with patch.object(callsites, 'gate_enqueue', wraps=callsites.gate_enqueue) as gate:
        assert owner_queue.enqueue_with_receipt(request, auth.receipt) == 'owner-approved'
    assert gate.call_count == 1
    if mode == 'off':
        assert owner_queue.get_task('owner-approved').context['cea_enqueue']['reason'].startswith(
            'CEA_MODE_OFF:')


def test_cap_refusal_survives_default_branch_replay(q, monkeypatch):
    exhausted(q)
    monkeypatch.setattr('agent_crew.pipeline._default_branches', lambda *a, **kw: None)
    assert auto_enqueue_fix(q, 'review', repo=REPO, pr_state_fn=lambda _: 'open',
                            coordinator_notify_fn=lambda _: True) is None
    events = q.get_exec_state('review')['events']
    assert any(e['event'] == 'successor_refused'
               and e.get('terminal_reason') == 'successor_refused: default_branch_unverified'
               for e in events)
    denied(q, task('fresh-after-replay'), 'fix_budget_exhausted')
    assert q.get_task('review').context['lineage_terminal_reason'] == (
        'successor_refused: fix_grant_exhausted')
    q.enqueue(extra('authorized-after-replay'))


def test_context_overwrite_cannot_hide_durable_cap_refusal(q):
    exhausted(q)
    q.merge_task_context('review', {'lineage_terminal_reason': 'successor_refused: default_branch_unverified'})
    denied(q, task('fresh-after-overwrite'), 'fix_budget_exhausted')
    # Both matching predecessor facts are still required for an extra round.
    denied(q, extra('extra-after-overwrite'), 'durable_refusal_required')


@pytest.mark.parametrize('legacy_context', [
    {}, {'repo': None}, {'risk_tier': None}, {'capability_tags': ['payments']},
])
def test_fresh_chain_ignores_unrelated_legacy_capabilities(q, monkeypatch, legacy_context):
    from agent_crew.lineage_human_gate import _TECHNICAL_AUTHORITY_START
    with monkeypatch.context() as patch:
        patch.setattr('agent_crew.queue.time.time', lambda: _TECHNICAL_AUTHORITY_START - 1)
        q.enqueue(task('legacy', pr_number=999, **legacy_context))
    exhausted(q)
    assert q.enqueue(extra('fresh-extra')) == 'fresh-extra'


@pytest.mark.parametrize('receipt_update', [
    "decision='HUMAN_GATE'", "runtime_state='HOLD'", "runtime_state='STOP'",
    "runtime_state='PARK'", """human_gate_state='{"state":"PENDING"}'""",
    """human_gate_state='{"state":"DENIED"}'""",
])
@pytest.mark.parametrize('fresh_root', [False, True])
def test_unrelated_issue_receipt_still_blocks_extra_round(q, receipt_update, fresh_root):
    q.enqueue(task('unrelated', pr_number=999))
    exhausted(q)
    with q._connect() as conn:
        # Receipts are append-only. Preserve the stored receipt and append
        # the historical owner-gate state at the next sequence.
        field, value = receipt_update.split('=', 1)
        columns = [row['name'] for row in conn.execute(
            "PRAGMA table_info(authorization_receipts)") if row['name'] != 'row_id']
        expressions = [
            'seq + 1' if column == 'seq' else value if column == field else column
            for column in columns
        ]
        conn.execute(
            f"INSERT INTO authorization_receipts ({', '.join(columns)}) "
            f"SELECT {', '.join(expressions)} FROM authorization_receipts "
            "WHERE receipt_id=(SELECT receipt_id FROM tasks WHERE task_id='unrelated') "
            "ORDER BY seq DESC LIMIT 1"
        )
    request = (task('blocked-by-receipt', pr_number=None,
                    lineage_root_task_id='blocked-by-receipt')
               if fresh_root else extra('blocked-by-receipt'))
    denied(q, request, 'owner_gate:receipt')


@pytest.mark.parametrize('root_context', [
    {}, {'lineage_root_task_id': 'fresh-root'}, {'lineage_root_id': 'fresh-root'},
    {'root_task_id': 'fresh-root'},
])
def test_fresh_root_on_exhausted_issue_is_admitted(q, root_context):
    exhausted(q)
    # Old issue history can also contain unresolved legacy references.
    q.merge_task_context('root', {'lineage_root_task_id': 'missing-legacy-root'})
    assert q.enqueue(task('fresh-root', pr_number=None, **root_context)) == 'fresh-root'


@pytest.mark.parametrize('canary_applied, receipt_id, held', [
    (0, 'held', False),
    (1, 'held', True),
    (1, None, False),
])
@pytest.mark.parametrize('fresh_root', [False, True])
def test_unrelated_issue_canary_hold_still_blocks_extra_round(
        q, fresh_root, canary_applied, receipt_id, held):
    q.enqueue(task('unrelated', pr_number=999))
    exhausted(q)
    with q._connect() as conn:
        conn.execute(
            "UPDATE tokenomics_shadow_receipts SET canary_applied=?, canary_cea_receipt_id=? "
            "WHERE task_id='unrelated'", (canary_applied, receipt_id),
        )
    request = (task('blocked-by-canary', pr_number=None)
               if fresh_root else extra('blocked-by-canary'))
    if held:
        denied(q, request, 'owner_gate:canary_HELD')
    else:
        assert q.enqueue(request) == request.task_id


def test_unrelated_conflicting_issue_still_blocks_extra_round(q):
    q.enqueue(task('unrelated', pr_number=999))
    exhausted(q)
    # Admission normalizes issue aliases; model conflicting legacy storage.
    with q._connect() as conn:
        conn.execute(
            "UPDATE tasks SET context=json_set(context, '$.issue_number', 2222) "
            "WHERE task_id='unrelated'"
        )
    denied(q, extra('blocked-by-issue'), 'conflicting_lineage_issue')


def test_missing_chain_repo_still_blocks_extra_round(q):
    exhausted(q, repo=None)
    denied(q, extra('blocked-by-repo'), 'unknown_capability:repo')


def test_linked_implement_without_pr_still_requires_grant(q):
    exhausted(q)
    denied(q, task('continuation', pr_number=None, prev_task_id='review'),
           'fix_budget_exhausted')


@pytest.mark.parametrize('marker', ['lineage_root_task_id', 'parent_task_id', 'root_task_id'])
def test_no_pr_marker_inherits_exhaustion(q, marker):
    exhausted(q)
    denied(q, task('legacy-continuation', pr_number=None, **{marker: 'root'}),
           'fix_budget_exhausted')


def test_unrelated_missing_root_does_not_poison_second_chain(q):
    exhausted(q)
    q.merge_task_context('root', {'lineage_root_task_id': 'absent'})
    q.enqueue(task('b', pr_number=None, lineage_root_task_id='b', fix_round_grant=1))
    review(q, 'b', 'b-review', fix_round_grant=1, pr_number=2222)
    child = auto_enqueue_fix(q, 'b-review', repo=REPO, pr_state_fn=lambda _: 'open')
    assert child is not None
    review(q, child, 'b-refused', pr_number=2222)
    refuse(q, 'b-refused')
    assert q.enqueue(extra('b-extra', 'b-refused', 'c333', pr_number=2222)) == 'b-extra'
    denied(q, extra('a-extra'), 'unresolved_lineage_reference')


@pytest.mark.parametrize('with_grant', [False, True])
def test_cycle_denied_without_cap_refusal(q, with_grant):
    q.enqueue(task('root'))
    q.merge_task_context('root', {'prev_task_id': 'root'})
    request = extra('cycle', 'root') if with_grant else task('cycle', prev_task_id='root')
    denied(q, request, 'cyclic_lineage')


def test_missing_reference_in_own_exhausted_chain_denied(q):
    exhausted(q)
    q.merge_task_context('root', {'prev_task_id': 'genuinely-absent'})
    denied(q, extra('missing'), 'unresolved_lineage_reference')


def test_persisted_cross_anchor_parent_is_resolved(q):
    q.enqueue(task('ancestor', issue=2222, pr_number=3333))
    q.enqueue(task('root', prev_task_id='ancestor'))
    finish(q, 'root')
    assert q.enqueue(task('ordinary', prev_task_id='root')) == 'ordinary'


@pytest.mark.parametrize('parent', ['absent', None, 42, ' whitespace '])
def test_ordinary_truncated_legacy_chain_preserves_main(q, parent):
    q.enqueue(task('root', prev_task_id=parent))
    finish(q, 'root')
    assert q.enqueue(task('ordinary', prev_task_id='root')) == 'ordinary'


def test_malformed_link_cannot_hide_exhausted_root_marker(q):
    exhausted(q)
    denied(q, task('bad-link', pr_number=None, prev_task_id=42,
                   lineage_root_task_id='root'), 'invalid_lineage_reference')


def test_two_node_cycle_denied(q):
    q.enqueue(task('root'))
    review(q, 'root')
    q.merge_task_context('root', {'parent_task_id': 'review'})
    denied(q, task('cycle', prev_task_id='review'), 'cyclic_lineage')


def test_extra_round_resolves_parent_without_shared_anchors(q):
    q.enqueue(task('ancestor', issue=None, pr_number=3333))
    # Keep this row genuinely persisted and outside the issue/PR anchor set.
    q.enqueue(task('root', prev_task_id='ancestor'))
    review(q, 'root')
    refuse(q, 'review')
    assert q.enqueue(extra('cross-anchor')) == 'cross-anchor'


@pytest.mark.parametrize('marker', ['lineage_root_id', 'lineage_root_task_id', 'root_task_id'])
def test_pr_self_root_marker_cannot_reset_exhaustion(q, marker):
    exhausted(q)
    denied(q, task('fresh', **{marker: 'fresh'}), 'fix_budget_exhausted')


@pytest.mark.parametrize('marker', [None, 'lineage_root_id', 'lineage_root_task_id', 'root_task_id'])
def test_missing_direct_predecessor_cannot_reset_exhausted_pr(q, marker):
    exhausted(q)
    context = {marker: 'fresh'} if marker else {}
    denied(q, task('fresh', prev_task_id='genuinely-absent', **context),
           'unresolved_lineage_reference')


@pytest.mark.parametrize('missing_ancestor', [False, True])
def test_resolved_chain_remains_independent_of_exhausted_pr(q, missing_ancestor):
    exhausted(q)
    q.enqueue(task('b', pr_number=None, lineage_root_task_id='b'))
    # A genuinely persisted B row establishes its own chain, even when its
    # older ancestor is missing. A missing direct incoming parent cannot.
    if missing_ancestor:
        q.merge_task_context('b', {'prev_task_id': 'missing-b-ancestor'})
    q.submit_result('b', TaskResult(task_id='b', status='completed',
                                    summary='result', pr_number=2222))
    assert q.enqueue(task('b-fix', prev_task_id='b', pr_number=2222,
                          lineage_root_task_id='b-fix')) == 'b-fix'


@pytest.mark.parametrize('marker', ['prev_task_id', 'lineage_root_task_id', 'lineage_root_id', 'root_task_id'])
def test_no_pr_unresolved_identity_cannot_reset_exhausted_issue(q, marker):
    exhausted(q)
    denied(q, task('unresolved', pr_number=None, **{marker: 'absent'}),
           'unresolved_lineage_reference')


def independent_review(q):
    q.enqueue(task('b-root', pr_number=None, lineage_root_task_id='b-root',
                   fix_round_grant=1))
    q.submit_result('b-root', TaskResult(task_id='b-root', status='completed',
                                        summary='result', pr_number=2222))
    q.enqueue(task('b-review', 'review', prev_task_id='b-root', pr_number=2222,
                   fix_round_grant=1))
    q.submit_result('b-review', TaskResult(
        task_id='b-review', status='completed', summary='result', pr_number=2222,
        verdict='request_changes', findings=['HIGH queue.py:1 - finding']))


def test_resolved_other_chain_cannot_authorize_exhausted_destination_pr(q):
    exhausted(q)
    independent_review(q)
    denied(q, task('wrong-pr', prev_task_id='b-review', pr_number=1090),
           'conflicting_lineage_pr')
    assert q.enqueue(task('right-pr', prev_task_id='b-review', pr_number=2222)) == 'right-pr'


@pytest.mark.parametrize('hold', [None, 'HOLD', 'HUMAN_GATE', 'canary_HELD'])
def test_ordinary_independent_fix_preserves_shared_authority(q, hold, caplog):
    exhausted(q)
    independent_review(q)
    if hold:
        with q._connect() as conn:
            if hold == 'canary_HELD':
                conn.execute("UPDATE tokenomics_shadow_receipts SET canary_applied=1, canary_cea_receipt_id='held' "
                             "WHERE task_id='root'")
            else:
                field = 'runtime_state' if hold == 'HOLD' else 'decision'
                columns = [row['name'] for row in conn.execute(
                    'PRAGMA table_info(authorization_receipts)') if row['name'] != 'row_id']
                expressions = ['seq + 1' if col == 'seq' else '?' if col == field else col
                               for col in columns]
                conn.execute(
                    f"INSERT INTO authorization_receipts ({', '.join(columns)}) "
                    f"SELECT {', '.join(expressions)} FROM authorization_receipts "
                    "WHERE receipt_id=(SELECT receipt_id FROM tasks WHERE task_id='root') "
                    "ORDER BY seq DESC LIMIT 1", (hold,))
    fix = auto_enqueue_fix(q, 'b-review', repo=REPO, pr_state_fn=lambda _: 'open')
    if hold:
        assert fix is None
        assert ('owner_gate:canary_HELD' if hold == 'canary_HELD'
                else 'owner_gate:receipt') in caplog.text
    else:
        assert fix is not None


@pytest.mark.parametrize('actual_predecessor', [False, True])
def test_legacy_admission_cycle_validation_is_component_local(q, actual_predecessor):
    q.enqueue(task('other-root'))
    finish(q, 'other-root')
    q.merge_task_context('other-root', {'prev_task_id': 'other-root'})
    context = {'prev_task_id': 'other-root'} if actual_predecessor else {}
    request = task('legacy', pr_number=2222, **context)
    if actual_predecessor:
        denied(q, request, 'cyclic_lineage')
    else:
        assert q.enqueue(request) == 'legacy'


@pytest.mark.parametrize('hold', [None, 'HOLD', 'HUMAN_GATE', 'canary_HELD'])
def test_legacy_cycle_fallback_preserves_exhaustion_and_shared_authority(q, hold):
    exhausted(q)
    q.merge_task_context('root', {'prev_task_id': 'root'})
    if hold:
        with q._connect() as conn:
            if hold == 'canary_HELD':
                conn.execute("UPDATE tokenomics_shadow_receipts SET canary_applied=1, canary_cea_receipt_id='held' "
                             "WHERE task_id='root'")
            else:
                field = 'runtime_state' if hold == 'HOLD' else 'decision'
                columns = [row['name'] for row in conn.execute(
                    'PRAGMA table_info(authorization_receipts)') if row['name'] != 'row_id']
                expressions = ['seq + 1' if col == 'seq' else '?' if col == field else col
                               for col in columns]
                conn.execute(
                    f"INSERT INTO authorization_receipts ({', '.join(columns)}) "
                    f"SELECT {', '.join(expressions)} FROM authorization_receipts "
                    "WHERE receipt_id=(SELECT receipt_id FROM tasks WHERE task_id='root') "
                    "ORDER BY seq DESC LIMIT 1", (hold,))
    reason = ('owner_gate:canary_HELD' if hold == 'canary_HELD' else
              'owner_gate:receipt' if hold else 'fix_budget_exhausted')
    denied(q, task('legacy', pr_number=2222), reason)
