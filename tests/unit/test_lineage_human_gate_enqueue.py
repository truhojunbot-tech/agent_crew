"""Regression: HUMAN_GATE survives successor IDs and coordinator-direct enqueue."""
import json
import sqlite3

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
