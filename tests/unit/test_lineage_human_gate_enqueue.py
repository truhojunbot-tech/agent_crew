"""Regression: HUMAN_GATE survives successor IDs and coordinator-direct enqueue."""
import json
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
        }), 1.0, "completed"))
    return c


def _extra(c, grant=None, **updates):
    detail = {}
    blocked = blocking_lineage_gate(
        c, project="alfred", pr_number=594, issue_number=590, receipt={},
        task_context={**TECHNICAL, "technical_extra_round": GRANT if grant is None else grant,
                      **updates}, task_id="extra", refusal=detail)
    return blocked, detail


def test_extra_round_admitted_once_from_durable_events():
    c = _technical_connection()
    assert _extra(c)[0] is None
    event = c.execute("SELECT * FROM task_exec_events").fetchone()
    assert event["event"] == "lineage_extra_round_admitted"
    assert json.loads(event["fields"])["finding"] == GRANT["finding"]
    blocked, detail = _extra(c, extra_rounds_used=0)
    assert blocked == "review-r1"
    assert detail["reason"] == "extra_round_already_used"


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
    with pytest.raises(PermissionError, match="LINEAGE_HUMAN_GATE.*extra_round_already_used"):
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


def test_concurrent_records_only_admit_once(tmp_path):
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
    assert results.count("review-r1") == 1
