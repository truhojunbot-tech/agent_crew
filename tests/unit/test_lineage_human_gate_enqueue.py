"""Regression: HUMAN_GATE survives successor IDs and coordinator-direct enqueue."""
import json
from datetime import datetime, timezone
import sqlite3

import pytest

from agent_crew.lineage_human_gate import blocking_lineage_gate


def _connection():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE tasks (task_id TEXT, project TEXT, pr_number INTEGER, "
        "context TEXT, created_at REAL)"
    )
    return c


def _record(c, *, task_id="review-r1", project="alfred", pr=593, issue=590,
            reason="successor_refused: fix_rounds_exhausted"):
    c.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, 1.0)",
        (task_id, project, pr, json.dumps({
            "issue": issue, "lineage_terminal_reason": reason
        })),
    )


def _block(c, **kwargs):
    return blocking_lineage_gate(
        c, project=kwargs.get("project", "alfred"),
        pr_number=kwargs.get("pr", 593),
        issue_number=kwargs.get("issue", 590),
        receipt=kwargs.get("receipt", {"decision": "ALLOW"}),
    )


def test_manual_successor_task_id_cannot_reset_exhausted_gate():
    c = _connection()
    _record(c)
    assert _block(c) == "review-r1"
    assert _block(c, pr=594) == "review-r1"  # same issue, different PR


def test_unrelated_project_or_issue_is_not_blocked():
    c = _connection()
    _record(c)
    assert _block(c, project="athena", pr=593, issue=590) is None
    assert _block(c, pr=888, issue=999) is None


def test_unverified_claim_or_code_review_does_not_authorize_reexecution():
    c = _connection()
    _record(c)
    claim = {"decision": "ALLOW", "reuse": {
        "approved_by": "coordinator", "approver_identity_verified": False
    }}
    assert _block(c, receipt=claim) == "review-r1"


def test_verified_owner_approval_receipt_can_reopen_scope():
    c = _connection()
    _record(c)
    receipt = {"decision": "ALLOW", "reuse": {
        "approved_by": "owner", "approver_identity_verified": True
    }}
    assert _block(c, receipt=receipt) is None


def test_non_exhaustion_terminal_reason_is_not_global_hold():
    c = _connection()
    _record(c, reason="successor_refused: default_branch")
    assert _block(c) is None


PROSPECTIVE_AT = datetime(2026, 11, 26, 22, tzinfo=timezone.utc).timestamp()

TECHNICAL = {"repo": "org/repo", "risk_tier": 1}
GRANT = {
    "grant_kind": "technical_extra_round", "authority_version": "405-v1",
    "issue": "org/repo#590", "finding": "7204237623",
    "authorizer": "coordinator", "source": "owner:17253",
}


def _technical_connection():
    c = _connection()
    c.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'completed'")
    c.execute("CREATE TABLE task_exec_events (task_id TEXT, event TEXT, at REAL, fields TEXT)")
    c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (
        "review-r1", "alfred", 593, json.dumps({
            **TECHNICAL, "issue": 590,
            "lineage_terminal_reason": "successor_refused: fix_rounds_exhausted",
        }), PROSPECTIVE_AT, "completed"))
    return c


def _extra(c, grant=None, **updates):
    detail = {}
    task_id = "extra-" + str(c.execute("SELECT COUNT(*) FROM task_exec_events").fetchone()[0])
    blocked = blocking_lineage_gate(
        c, project="alfred", pr_number=594, issue_number=590, receipt={},
        task_context={**TECHNICAL, "technical_extra_round": GRANT if grant is None else grant,
                      **updates}, task_id=task_id, refusal=detail)
    if blocked is None:
        c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (
            task_id, "alfred", 594, json.dumps({**TECHNICAL, "issue": 590}),
            PROSPECTIVE_AT, "completed"))
        # Model the queue caller finishing this round with its durable notice.
        c.execute("INSERT INTO task_exec_events VALUES (?, ?, ?, ?)", (
            task_id, "coordinator_notice", PROSPECTIVE_AT,
            json.dumps({"kind": "lineage_end"})))
    return blocked, detail


def test_same_finding_refused_from_durable_events():
    c = _technical_connection()
    assert _extra(c)[0] is None
    event = c.execute("SELECT * FROM task_exec_events").fetchone()
    assert event["event"] == "lineage_extra_round_admitted"
    assert json.loads(event["fields"])["finding"] == GRANT["finding"]
    blocked, detail = _extra(c, extra_rounds_used=0)
    assert blocked == "extra-0"
    assert detail["reason"] == "extra_round_finding_reused"


@pytest.mark.parametrize("field", list(GRANT))
@pytest.mark.parametrize("invalid", [None, "wrong"])
def test_invalid_v1_record_refused(field, invalid):
    grant = dict(GRANT)
    if invalid is None:
        del grant[field]
    else:
        grant[field] = invalid
    # Findings are opaque numeric GitHub comment IDs.
    assert _extra(_technical_connection(), grant)[0] == "review-r1"


@pytest.mark.parametrize("tag", [
    "orders", "risk", "caps", "money", "credentials", "payments",
    "external_publishing", "irreversible_removal", "unknown",
])
def test_excluded_or_unknown_capability_on_predecessor_refused(tag):
    c = _technical_connection()
    ctx = json.loads(c.execute("SELECT context FROM tasks").fetchone()[0])
    ctx["capability_tags"] = [tag]
    c.execute("UPDATE tasks SET context=?", (json.dumps(ctx),))
    assert _extra(c)[0] == "review-r1"


@pytest.mark.parametrize("change", [
    {"risk_tier": 3}, {"human_gate_required": True},
    {"owner_gate": "HUMAN_GATE"}, {"owner_gate": "HOLD"},
    {"owner_gate": "STOP"}, {"owner_gate": "PARK"},
])
def test_owner_gate_refused(change):
    c = _technical_connection()
    ctx = json.loads(c.execute("SELECT context FROM tasks").fetchone()[0])
    ctx.update(change)
    c.execute("UPDATE tasks SET context=?", (json.dumps(ctx),))
    assert _extra(c)[1]["reason"] == "owner_gate"


def test_needs_human_status_refused():
    c = _technical_connection()
    c.execute("UPDATE tasks SET status='needs_human'")
    assert _extra(c)[1]["reason"] == "owner_gate"


def test_unknown_risk_refused():
    c = _technical_connection()
    c.execute("UPDATE tasks SET context=json_remove(context, '$.risk_tier')")
    assert _extra(c)[1]["reason"] == "unknown_capability"


def test_admission_event_rolls_back_with_transaction():
    c = _technical_connection()
    c.commit()
    c.execute("BEGIN IMMEDIATE")
    assert _extra(c)[0] is None
    c.rollback()
    assert c.execute("SELECT COUNT(*) FROM task_exec_events").fetchone()[0] == 0
    assert _extra(c)[0] is None


def test_canary_receipt_refuses_extra_round():
    c = _technical_connection()
    c.execute("CREATE TABLE tokenomics_shadow_receipts (task_id TEXT, canary_cea_receipt_id TEXT)")
    c.execute("INSERT INTO tokenomics_shadow_receipts VALUES ('review-r1', 'held-receipt')")
    assert _extra(c)[1]["reason"] == "owner_gate:canary_HELD"


def test_different_pr_links_all_prior_risk_rows():
    c = _technical_connection()
    c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (
        "old-root", "alfred", 593, json.dumps({
            **TECHNICAL, "issue": 589, "capability_tags": ["orders"],
        }), 0.0, "completed"))
    assert _extra(c)[0] == "review-r1"


def test_queue_admission_and_duplicate_insert_rollback(tmp_path):
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue, TaskAlreadyExistsError

    q = TaskQueue(str(tmp_path / "queue.db"))
    def task(task_id, grant=False):
        return TaskRequest(
            task_id=task_id, task_type="implement", description="bounded technical fix",
            project="alfred", pr_number=593,
            context={**TECHNICAL, "issue": 590,
                     **({"technical_extra_round": GRANT} if grant else {})})
    q.enqueue(task("root"))
    q.patch_context("root", {
        "lineage_terminal_reason": "successor_refused: fix_rounds_exhausted",
    })
    with pytest.raises(TaskAlreadyExistsError):
        q.enqueue(task("root", True))
    q.enqueue(task("extra", True))
    with pytest.raises(PermissionError, match="LINEAGE_HUMAN_GATE.*extra_round_finding_reused"):
        q.enqueue(task("extra2", True))
    c = q._connect()
    try:
        assert c.execute(
            "SELECT COUNT(*) FROM task_exec_events WHERE event='lineage_extra_round_admitted'"
        ).fetchone()[0] == 1
        assert c.execute("SELECT 1 FROM tasks WHERE task_id='extra2'").fetchone() is None
    finally:
        c.close()


def test_predecessor_without_issue_still_checked():
    c = _technical_connection()
    ctx = json.loads(c.execute("SELECT context FROM tasks").fetchone()[0])
    ctx["prev_task_id"] = "root"
    c.execute("UPDATE tasks SET context=?", (json.dumps(ctx),))
    c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (
        "root", "alfred", None, json.dumps({
            **TECHNICAL, "capability_tags": ["caps"],
        }), 0.0, "completed"))
    assert _extra(c)[0] == "review-r1"


def test_concurrent_same_finding_only_admitted_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    path = tmp_path / "race.db"
    c = _technical_connection()
    disk = sqlite3.connect(path)
    c.commit()
    c.backup(disk)
    disk.close()
    c.close()
    barrier = threading.Barrier(2)

    def admit():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=5)
            conn.execute("BEGIN IMMEDIATE")
            blocked, _ = _extra(conn)
            conn.commit()
            return blocked
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: admit(), range(2)))
    assert results.count(None) == 1
    assert results.count("extra-0") == 1


def test_v11_second_distinct_finding_admitted_third_refused():
    c = _technical_connection()
    assert _extra(c)[0] is None
    assert _extra(c, {**GRANT, "finding": "7204988620"})[0] is None
    assert _extra(c, {**GRANT, "finding": "7205022943"})[1]["reason"] == "extra_rounds_exhausted"


@pytest.mark.parametrize("anchors", [(999, 998), (None, None)])
@pytest.mark.parametrize("risk", [{}, {"owner_gate": "HUMAN_GATE"}, {"capability_tags": ["orders"]}])
@pytest.mark.parametrize("with_grant", [False, True])
def test_queue_incoming_predecessor_cannot_reset_lineage(tmp_path, anchors, risk, with_grant):
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue

    q = TaskQueue(str(tmp_path / "linked.db"))
    q.enqueue(TaskRequest(
        task_id="old", task_type="implement", description="technical fix",
        project="alfred", pr_number=593, context={**TECHNICAL, "issue": 590}))
    q.patch_context("old", {
        "lineage_terminal_reason": "successor_refused: fix_rounds_exhausted", **risk,
    })
    pr, issue = anchors
    with pytest.raises(PermissionError, match="LINEAGE_HUMAN_GATE"):
        q.enqueue(TaskRequest(
            task_id="new", task_type="implement", description="technical fix",
            project="alfred", pr_number=pr,
            context={**TECHNICAL, "issue": issue, "prev_task_id": "old",
                     **({"technical_extra_round": {**GRANT, "issue": f"org/repo#{issue}"}}
                        if with_grant else {})}))


def test_earlier_held_technical_lineage_stays_held():
    c = _technical_connection()
    c.execute("UPDATE tasks SET created_at=1.0")
    assert _extra(c)[1].get("reason") == "nonretroactive_lineage"


@pytest.mark.parametrize("first", ["7204988620", "c7204988620", "issuecomment-7204988620"])
@pytest.mark.parametrize("second", ["7204988620", "c7204988620", "issuecomment-7204988620"])
def test_finding_aliases_cannot_spend_another_round(first, second):
    c = _technical_connection()
    assert _extra(c, {**GRANT, "finding": first})[0] is None
    assert _extra(c, {**GRANT, "finding": second})[1]["reason"] == "extra_round_finding_reused"


@pytest.mark.parametrize("key", [
    "prev_task_id", "parent_task_id", "lineage_root_id", "lineage_root_task_id", "root_task_id",
])
def test_incoming_reference_without_numeric_anchors(key):
    c = _technical_connection()
    detail = {}
    assert blocking_lineage_gate(
        c, project="alfred", pr_number=None, issue_number=None, receipt={},
        task_context={**TECHNICAL, key: "review-r1"}, task_id="new", refusal=detail,
    ) == "review-r1"
    assert detail["reason"] == "fix_budget_exhausted"


@pytest.mark.parametrize("reference, reason", [
    ("missing", "unresolved_lineage_reference"),
    (" review-r1", "invalid_lineage_reference"),
    (None, "invalid_lineage_reference"),
    (123, "invalid_lineage_reference"),
])
def test_bad_incoming_reference_fails_closed(reference, reason):
    c = _technical_connection()
    assert _extra(c, prev_task_id=reference)[1]["reason"] == reason


def test_cross_project_reference_is_unresolved():
    c = _technical_connection()
    c.execute("UPDATE tasks SET project='other'")
    assert _extra(c, prev_task_id="review-r1")[1]["reason"] == "unresolved_lineage_reference"


def test_conflicting_repo_fails_closed():
    c = _technical_connection()
    assert _extra(c, repo="other/repo", prev_task_id="review-r1")[1]["reason"] == "conflicting_lineage_repo"


def test_legacy_root_is_not_released_by_new_exhausted_successor():
    c = _technical_connection()
    c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (
        "old-root", "alfred", 593, json.dumps({**TECHNICAL, "issue": 590}),
        1.0, "completed"))
    assert _extra(c, extra_rounds_used=0, lineage_created_at=PROSPECTIVE_AT)[1]["reason"] == "nonretroactive_lineage"


@pytest.mark.parametrize("created_at", [None, "unknown", float("inf")])
def test_undated_lineage_stays_held(created_at):
    c = _technical_connection()
    c.execute("UPDATE tasks SET created_at=?", (created_at,))
    assert _extra(c)[1]["reason"] == "nonretroactive_lineage"


def test_events_on_linked_predecessors_share_allowance():
    c = _technical_connection()
    c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (
        "root", "alfred", None, json.dumps({**TECHNICAL, "issue": 590}),
        PROSPECTIVE_AT, "completed"))
    for task_id, finding in [("root", "c111"), ("review-r1", "issuecomment-222")]:
        c.execute("INSERT INTO task_exec_events VALUES (?, ?, ?, ?)", (
            task_id, "lineage_extra_round_admitted", PROSPECTIVE_AT,
            json.dumps({"finding": finding})))
    assert _extra(c)[1]["reason"] == "extra_rounds_exhausted"


def test_concurrent_distinct_findings_cannot_admit_third_round(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    path = tmp_path / "distinct.db"
    c = _technical_connection()
    assert _extra(c)[0] is None  # One slot remains.
    c.commit()
    disk = sqlite3.connect(path)
    c.backup(disk)
    disk.close()
    c.close()
    barrier = threading.Barrier(2)

    def admit(finding):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=5)
            conn.execute("BEGIN IMMEDIATE")
            result = _extra(conn, {**GRANT, "finding": finding})
            conn.commit()
            return result
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, ["111", "222"]))
    assert sum(blocked is None for blocked, _ in results) == 1
    assert [detail["reason"] for blocked, detail in results if blocked] == ["extra_rounds_exhausted"]


@pytest.mark.parametrize("first_state", ["pending", "in_progress", "review_pending", "outbox_pending", "outbox_replaying"])
def test_queue_two_distinct_rounds_then_third_refused(tmp_path, first_state):
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue

    q = TaskQueue(str(tmp_path / "two-rounds.db"))
    def task(task_id, finding=None):
        return TaskRequest(
            task_id=task_id, task_type="implement", description="technical fix",
            project="alfred", pr_number=593,
            context={**TECHNICAL, "issue": 590, **({
                "prev_task_id": "root",
                "technical_extra_round": {**GRANT, "finding": finding},
            } if finding else {})})
    q.enqueue(task("root"))
    q.patch_context("root", {
        "lineage_terminal_reason": "successor_refused: fix_rounds_exhausted",
    })
    q.enqueue(task("first", "c111"))
    if first_state != "pending":
        c = q._connect()
        c.execute("UPDATE tasks SET status=? WHERE task_id='first'", (
            "in_progress" if first_state == "in_progress" else "completed",))
        c.commit()
        c.close()
    if first_state.startswith("outbox_"):
        c = q._connect()
        c.execute(
            "INSERT INTO cascade_outbox (parent_task_id, result_json, state) VALUES (?, ?, ?)",
            ("first", "{}", first_state.removeprefix("outbox_")))
        c.commit()
        c.close()
    if first_state == "review_pending":
        q.enqueue(TaskRequest(
            task_id="review-first", task_type="review", description="review technical fix",
            project="alfred", pr_number=593, context={
                **TECHNICAL, "issue": 590, "prev_task_id": "first",
                "expected_head_sha": "a" * 40,
            }))
    with pytest.raises(PermissionError, match="LINEAGE_HUMAN_GATE.*extra_round_active"):
        q.enqueue(task("second", "issuecomment-222"))
    c = q._connect()
    c.execute("UPDATE tasks SET status='completed' WHERE task_id='first'")
    c.execute("UPDATE cascade_outbox SET state='applied' WHERE parent_task_id='first'")
    c.commit()
    c.close()
    if first_state == "review_pending":
        from agent_crew.protocol import TaskResult
        q.submit_result("review-first", TaskResult(
            task_id="review-first", status="completed", summary="needs changes",
            verdict="request_changes", findings=["next finding"]))
    else:
        from agent_crew.server import notify_lineage_end
        notify_lineage_end(q, "first", notify_fn=lambda *args, **kwargs: True)
    q.enqueue(task("second", "issuecomment-222"))
    with pytest.raises(PermissionError, match="LINEAGE_HUMAN_GATE.*extra_rounds_exhausted"):
        q.enqueue(task("third", "333"))
    c = q._connect()
    try:
        assert c.execute(
            "SELECT COUNT(*) FROM task_exec_events WHERE event='lineage_extra_round_admitted'"
        ).fetchone()[0] == 2
        assert c.execute("SELECT 1 FROM tasks WHERE task_id='third'").fetchone() is None
    finally:
        c.close()



def test_cascade_then_hand_same_finding_names_survivor(tmp_path):
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue

    q = TaskQueue(str(tmp_path / "survivor.db"))
    def task(task_id, finding=None):
        return TaskRequest(
            task_id=task_id, task_type="implement", description="technical fix",
            project="alfred", pr_number=593,
            context={**TECHNICAL, "issue": 590, **({
                "prev_task_id": "root",
                "technical_extra_round": {**GRANT, "finding": finding},
            } if finding else {})})
    q.enqueue(task("root"))
    q.patch_context("root", {
        "lineage_terminal_reason": "successor_refused: fix_rounds_exhausted",
    })
    survivor = "fix-review-root-r1"
    q.enqueue(task(survivor, "c111"))
    with pytest.raises(PermissionError, match=f"LINEAGE_HUMAN_GATE.*{survivor}.*extra_round_finding_reused"):
        q.enqueue(task("hand-successor", "issuecomment-111"))
    c = q._connect()
    try:
        events = c.execute(
            "SELECT event, fields FROM task_exec_events WHERE event IN "
            "('lineage_extra_round_admitted', 'lineage_human_gate_refused')"
        ).fetchall()
        assert len(events) == 2
        assert events[0]["event"] == "lineage_extra_round_admitted"
        assert events[1]["event"] == "lineage_human_gate_refused"
        fields = json.loads(events[1]["fields"])
        assert fields["blocking_task_id"] == survivor
        assert fields["admitted_task_id"] == survivor
        assert c.execute("SELECT 1 FROM tasks WHERE task_id='hand-successor'").fetchone() is None
    finally:
        c.close()



def test_missing_admitted_round_disposition_fails_closed():
    c = _technical_connection()
    c.execute("INSERT INTO task_exec_events VALUES (?, ?, ?, ?)", (
        "review-r1", "lineage_extra_round_admitted", PROSPECTIVE_AT,
        json.dumps({"finding": "111", "admitted_task_id": "missing"})))
    assert _extra(c)[1]["reason"] == "extra_round_disposition_unknown"


def test_caller_cannot_claim_active_round_finished():
    c = _technical_connection()
    assert _extra(c)[0] is None
    c.execute("UPDATE tasks SET status='in_progress' WHERE task_id='extra-0'")
    blocked, detail = _extra(
        c, {**GRANT, "finding": "222"}, extra_round_status="completed")
    assert blocked == "extra-0"
    assert detail["reason"] == "extra_round_active"


@pytest.mark.parametrize("ending", [
    "review", "failed_cascade", "no_event", "successor_refused", "review_without_verdict",
])
def test_real_result_before_live_cascade_keeps_round_active(tmp_path, ending):
    from agent_crew.protocol import TaskRequest, TaskResult
    from agent_crew.queue import TaskQueue
    from agent_crew.pipeline import record_cascade_enqueue_failure

    q = TaskQueue(str(tmp_path / "result-cascade.db"))
    def task(task_id, finding=None):
        return TaskRequest(
            task_id=task_id, task_type="implement", description="technical fix",
            project="alfred", pr_number=593,
            context={**TECHNICAL, "issue": 590, **({
                "prev_task_id": "root",
                "technical_extra_round": {**GRANT, "finding": finding},
            } if finding else {})})
    q.enqueue(task("root"))
    q.patch_context("root", {
        "lineage_terminal_reason": "successor_refused: fix_rounds_exhausted",
    })
    q.enqueue(task("first", "111"))
    # The server calls this before its live cascade. Stop at that boundary,
    # using the real result transaction, not hand-written status/outbox rows.
    q.submit_result("first", TaskResult(
        task_id="first", status="completed", summary="implemented", pr_number=593))
    assert q.outbox_get("first")["state"] == "applied"
    assert not q.has_successor("first")
    with pytest.raises(PermissionError, match="extra_round_active"):
        q.enqueue(task("second", "222"))
    if ending in ("review", "review_without_verdict"):
        q.enqueue(TaskRequest(
            task_id="review-first", task_type="review", description="review fix",
            project="alfred", pr_number=593, context={
                **TECHNICAL, "issue": 590, "prev_task_id": "first",
                "expected_head_sha": "a" * 40,
            }))
        with pytest.raises(PermissionError, match="extra_round_active"):
            q.enqueue(task("second", "222"))
        if ending == "review_without_verdict":
            q.submit_result("review-first", TaskResult(
                task_id="review-first", status="failed", summary="review failed"))
            with pytest.raises(PermissionError, match="extra_round_active"):
                q.enqueue(task("second", "222"))
            return
        q.submit_result("review-first", TaskResult(
            task_id="review-first", status="completed", summary="changes required",
            verdict="request_changes", findings=["second distinct finding"]))
    elif ending == "failed_cascade":
        record_cascade_enqueue_failure(
            q, "first", "review", RuntimeError("review enqueue failed"),
            coordinator_notify_fn=lambda *args, **kwargs: True)
        c = q._connect()
        try:
            assert c.execute(
                "SELECT 1 FROM task_exec_events WHERE task_id='first' "
                "AND event='coordinator_notice' "
                "AND json_extract(fields, '$.kind')='lineage_end'"
            ).fetchone() is not None
        finally:
            c.close()
    elif ending == "successor_refused":
        from agent_crew.pipeline import _record_successor_refused
        _record_successor_refused(
            q, "first", "fix_rounds_exhausted", "no successor",
            coordinator_notify_fn=lambda *args, **kwargs: True)
    else:
        # A failure annotation without the durable terminal notice is not
        # proof that a delayed cascade cannot still create a review.
        q.annotate_error_info("first", {"reason": "cascade_enqueue_failed"})
        with pytest.raises(PermissionError, match="extra_round_active"):
            q.enqueue(task("second", "222"))
        return
    q.enqueue(task("second", "222"))
