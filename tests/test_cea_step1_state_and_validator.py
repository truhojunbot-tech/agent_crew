"""SEV-0 CEA fold, step 1 — P6 runtime state, §3 receipt store, T3 validator.

Contract under test: alfred ``sev0/e11-adr-draft`` @ ``6cbce565``
(``evidence/sev0-p0/E11-ADR-DRAFT.md``), Π P1–P7 and §3, with the receipt schema
frozen at alfred ``sev0/cea-alfred-lineage`` @ ``e1063eb``.

The tables in the ADR are executable here: every P3 outcome row, every P6
enforcement cell, the O18 field list and the O20 window each have a test, so a
later change to the validator cannot quietly disagree with the contract.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time

import pytest

from agent_crew.cea import schema as cea_schema
from agent_crew.cea import store as cea_store
from agent_crew.cea.validator import (
    CurrentInputs, O18_IMMEDIATE_INVALIDATION_FIELDS, O18_MAX_RECEIPT_AGE_SECONDS,
    O20_SNAPSHOT_MAX_AGE_SECONDS, ValidationOutcome, ValidationPoint, validate)
from agent_crew.queue import RUNTIME_STATES, RuntimeTransitionRefused, TaskQueue

FIXTURE_DB = os.path.join(os.path.dirname(__file__), "fixtures", "pre_g12_tasks.db")
LIVE_DB = os.path.expanduser("~/.agent_crew/agent_crew/tasks.db")


# ── receipt fixtures ────────────────────────────────────────────────────────

def make_binding(**over) -> dict:
    b = {
        "policy_generation": 7,
        "policy_hash": "sha256:" + "a" * 64,
        "source_decision_revs": [{"decision_id": "D-51-1", "body_hash": "sha256:" + "b" * 64}],
        "capability_registry": {"generation": 3, "hash": "sha256:" + "c" * 64},
        "matched_capability": {"id": "cap.dispatch", "owner": "agent_crew"},
        "runtime_state": "ACTIVE",
        "runtime_state_epoch": 12,
        "human_gate_state": "NOT_REQUIRED",
        "budget_class": "OK",
    }
    b.update(over)
    return b


def make_receipt(**over) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = {
        "receipt_id": "11111111-2222-3333-4444-555555555555",
        "issued_at": now,
        "issuer": "agent_crew:8105@d073a59",
        "task_id": "t-1",
        "intent_hash": "sha256:" + "d" * 64,
        "parent_receipt_id": None,
        "project": "agent_crew",
        "authority_source": {"decision_ids": ["D-51-1"], "tier": "T0"},
        "policy_generation": 7,
        "policy_hash": "sha256:" + "a" * 64,
        "source_decision_revs": [{"decision_id": "D-51-1", "body_hash": "sha256:" + "b" * 64}],
        "capability_registry": {"generation": 3, "hash": "sha256:" + "c" * 64},
        "matched_capability": {"id": "cap.dispatch", "owner": "agent_crew", "repo": "agent_crew"},
        "reuse": None,
        "runtime_state": "ACTIVE",
        "provider_budget": {"provider": "claude", "state": "OK", "observed_at": now},
        "required_reviewer": None,
        "required_tester": None,
        "human_gate_state": "NOT_REQUIRED",
        "caller_identity": "cron-adapter",
        "caller_provenance": "cron",
        "executor_binding": "claude",
        "executor_binding_status": "UNVERIFIED",
        "caller_identity_status": "UNVERIFIED",
        "downgrade_reason": "SHARED_UID_NO_CREDENTIAL_BOUNDARY",
        "decision": "ALLOW",
        "reason": {"code": "ALLOW_SHADOW", "text": "caller-independent shadow work"},
        "signature": {"alg": None, "key_id": None, "value": None, "status": "UNVERIFIED"},
        "state": "ISSUED",
        "binding": make_binding(),
        "idempotency_key": "idem-1",
        "attempt": 1,
        "max_attempts": 3,
        "dispatch_nonces": [],
        "supersedes": [],
    }
    r.update(over)
    return r


# ── the frozen contract ─────────────────────────────────────────────────────

class TestFrozenSchema:
    def test_copy_is_byte_identical_to_alfreds(self):
        """The schema file here must be the same blob as alfred's frozen copy.

        Two repos implementing "the same" contract from two files is exactly the
        divergence the ADR forbids; ``git hash-object`` is the cheap proof.
        """
        out = subprocess.run(["git", "hash-object", str(cea_schema.SCHEMA_PATH)],
                             capture_output=True, text=True, check=True)
        assert out.stdout.strip() == cea_schema.SCHEMA_BLOB

    def test_schema_is_closed_and_requires_every_field(self):
        s = cea_schema.load_schema()
        assert s["additionalProperties"] is False
        # "'provenance' is the only non-§3 key and is optional"
        assert cea_schema.optional_fields() == ("provenance",)

    def test_valid_receipt_passes(self):
        assert cea_schema.validate_receipt(make_receipt()) == []

    @pytest.mark.parametrize("mutation, expect", [
        ({"decision": "MAYBE"}, "decision"),
        ({"caller_provenance": "gremlin"}, "caller_provenance"),
        ({"state": "PENDING"}, "state"),
        ({"receipt_id": "not-a-uuid"}, "receipt_id"),
        ({"issued_at": "2026-09-23 10:00:00"}, "issued_at"),
        ({"intent_hash": "deadbeef"}, "intent_hash"),
    ])
    def test_invalid_values_are_rejected(self, mutation, expect):
        errors = cea_schema.validate_receipt(make_receipt(**mutation))
        assert errors and expect in errors[0]

    def test_missing_key_is_rejected_even_when_unknowable(self):
        """"a value the issuer cannot know is an explicit null or UNVERIFIED,
        never an omitted key"."""
        r = make_receipt()
        del r["parent_receipt_id"]
        assert cea_schema.validate_receipt(r)

    def test_unknown_key_is_rejected(self):
        assert cea_schema.validate_receipt(make_receipt(extra_field=1))

    def test_fallback_checker_agrees_with_jsonschema(self):
        """The dependency-free fallback is not allowed to be a second contract."""
        jsonschema = pytest.importorskip("jsonschema")
        s = cea_schema.load_schema()
        validator = jsonschema.Draft202012Validator(s)
        cases = [make_receipt(), make_receipt(decision="MAYBE"), make_receipt(extra_field=1),
                 make_receipt(human_gate_state={"state": "GRANTED", "decision_id": "D-9"}),
                 make_receipt(human_gate_state={"state": "GRANTED"}),
                 make_receipt(matched_capability=None), make_receipt(attempt=0),
                 make_receipt(dispatch_nonces=[{"nonce": "n", "attempt": 1,
                                                "issued_at": "2026-09-23T10:00:00Z",
                                                "used_at": None}])]
        for case in cases:
            theirs = bool(list(validator.iter_errors(case)))
            ours = bool(cea_schema._validate_subset(case, s, s, "$"))
            assert theirs == ours, f"disagreement on {case.get('decision')}/{case.get('attempt')}"


# ── §3 receipt store ────────────────────────────────────────────────────────

@pytest.fixture()
def store_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    cea_store.ensure_schema(conn)
    yield conn
    conn.close()


class TestReceiptStore:
    def test_columns_match_the_frozen_schema(self):
        cea_store._assert_columns_match_contract()

    def test_record_and_read_back(self, store_conn):
        r = make_receipt()
        assert cea_store.record_receipt(store_conn, r) == 0
        assert cea_store.current_receipt(store_conn, r["receipt_id"]) == r

    def test_invalid_receipt_never_reaches_the_table(self, store_conn):
        with pytest.raises(cea_store.ReceiptStoreError):
            cea_store.record_receipt(store_conn, make_receipt(decision="MAYBE"))
        assert store_conn.execute("SELECT COUNT(*) FROM authorization_receipts").fetchone()[0] == 0

    def test_update_is_refused_by_trigger(self, store_conn):
        cea_store.record_receipt(store_conn, make_receipt())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store_conn.execute("UPDATE authorization_receipts SET state = 'CONSUMED'")

    def test_delete_is_refused_by_trigger(self, store_conn):
        cea_store.record_receipt(store_conn, make_receipt())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store_conn.execute("DELETE FROM authorization_receipts")

    def test_lifecycle_change_is_a_new_row(self, store_conn):
        r = make_receipt()
        cea_store.record_receipt(store_conn, r)
        cea_store.append_lifecycle(store_conn, r["receipt_id"], "QUEUED")
        cea_store.append_lifecycle(store_conn, r["receipt_id"], "CLAIMED")
        history = cea_store.receipt_history(store_conn, r["receipt_id"])
        assert [h["state"] for h in history] == ["ISSUED", "QUEUED", "CLAIMED"]
        assert [h["seq"] for h in history] == [0, 1, 2]
        assert cea_store.current_receipt(store_conn, r["receipt_id"])["state"] == "CLAIMED"

    def test_nonce_is_single_use(self, store_conn):
        cea_store.mint_nonce(store_conn, "rid", 1, "nonce-a", "2026-09-23T10:00:00Z")
        assert cea_store.consume_nonce(store_conn, "nonce-a", used_by="claude") is True
        assert cea_store.consume_nonce(store_conn, "nonce-a", used_by="codex") is False
        assert cea_store.nonce_row(store_conn, "nonce-a")["used_by"] == "claude"

    def test_unknown_nonce_is_not_consumable(self, store_conn):
        assert cea_store.consume_nonce(store_conn, "never-minted") is False

    def test_one_attempt_cannot_have_two_live_nonces(self, store_conn):
        cea_store.mint_nonce(store_conn, "rid", 1, "n1", "2026-09-23T10:00:00Z")
        with pytest.raises(sqlite3.IntegrityError):
            cea_store.mint_nonce(store_conn, "rid", 1, "n2", "2026-09-23T10:00:00Z")


# ── migration: additive and idempotent on an existing DB ────────────────────

def _columns(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class TestMigration:
    @pytest.mark.parametrize("source", ["fixture", "live"])
    def test_idempotent_on_a_copy_of_a_real_db(self, tmp_path, source):
        """Open the same DB three times: the schema converges and stays converged.

        The live DB is *copied*; nothing in this test opens the server's own file.
        """
        src = FIXTURE_DB if source == "fixture" else LIVE_DB
        if not os.path.exists(src):
            pytest.skip(f"no {source} DB at {src}")
        db = str(tmp_path / f"{source}.db")
        shutil.copy(src, db)
        before = _columns(db, "tasks")
        for _ in range(3):
            TaskQueue(db)
        assert before <= _columns(db, "tasks")           # additive: nothing removed
        assert "receipt_id" in _columns(db, "tasks")
        assert {"state", "reason", "decision_id"} <= _columns(db, "runtime_stop")
        conn = sqlite3.connect(db)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"authorization_receipts", "dispatch_nonces", "runtime_state_events"} <= tables
            # idempotence in the data, not just the DDL: three opens, one row.
            assert conn.execute("SELECT COUNT(*) FROM runtime_stop").fetchone()[0] <= 1
        finally:
            conn.close()

    def test_paused_db_backfills_to_stopped(self, tmp_path):
        """A DB written before P6 carries only ``paused``; STOPPED == paused is
        what keeps every #314 caller correct after the migration."""
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE runtime_stop (id INTEGER PRIMARY KEY CHECK (id = 1), "
                     "epoch INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0, "
                     "incident TEXT, note TEXT, updated_at REAL NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO runtime_stop (id, epoch, paused, incident) "
                     "VALUES (1, 4, 1, 'SEV-0')")
        conn.commit()
        conn.close()
        q = TaskQueue(db)
        state = q.get_runtime_state()
        assert state["state"] == "STOPPED"
        assert state["paused"] is True
        assert state["epoch"] == 4
        assert q.get_stop_epoch()["paused"] is True


# ── P6 runtime state ────────────────────────────────────────────────────────

@pytest.fixture()
def q(tmp_path, monkeypatch):
    """A queue whose pause.json signal is inert, so the row is the only input."""
    monkeypatch.setattr("agent_crew.pause.is_paused", lambda *a, **k: False)
    return TaskQueue(str(tmp_path / "tasks.db"))


class TestRuntimeStateTransitions:
    def test_default_is_active(self, q):
        assert q.get_runtime_state()["state"] == "ACTIVE"
        assert q.get_runtime_state()["paused"] is False

    @pytest.mark.parametrize("frm, to, who, decision_id, allowed", [
        # P6 transition table, row by row.
        ("ACTIVE", "DRAINING", "operator:alfred", None, True),       # tighten
        ("ACTIVE", "QUARANTINED", "operator:alfred", None, True),    # tighten
        ("ACTIVE", "STOPPED", "fleet_stop", None, True),             # tighten
        ("DRAINING", "QUARANTINED", "runtime", None, True),          # tighten (automatic)
        ("DRAINING", "STOPPED", "runtime", None, True),              # tighten (in-flight = 0)
        ("DRAINING", "ACTIVE", "operator:alfred", None, True),       # the principal that set it
        ("DRAINING", "ACTIVE", "operator:someone-else", None, False),
        ("DRAINING", "ACTIVE", "owner", None, True),
        ("QUARANTINED", "ACTIVE", "operator:alfred", "D-1", False),  # owner only
        ("QUARANTINED", "ACTIVE", "owner", None, False),             # owner, but no T0 record
        ("QUARANTINED", "ACTIVE", "owner", "D-51-9", True),
        ("QUARANTINED", "DRAINING", "owner", "D-51-9", True),        # still a loosening
        ("STOPPED", "ACTIVE", "operator:alfred", "D-1", False),
        ("STOPPED", "ACTIVE", "owner", "D-51-9", True),
        ("STOPPED", "QUARANTINED", "operator:alfred", None, False),  # loosening from STOPPED
    ])
    def test_transition_authority(self, q, frm, to, who, decision_id, allowed):
        if frm != "ACTIVE":
            q.transition_runtime_state(frm, who="operator:alfred", reason="setup")
        if allowed:
            out = q.transition_runtime_state(to, who=who, decision_id=decision_id, reason="test")
            assert out["state"] == to
            assert q.get_runtime_state()["state"] == to
        else:
            with pytest.raises(RuntimeTransitionRefused):
                q.transition_runtime_state(to, who=who, decision_id=decision_id, reason="test")
            assert q.get_runtime_state()["state"] == frm

    def test_tightening_is_never_blocked(self, q):
        """"A tightening can never be blocked by an unavailable input" — and never
        by authority either: any principal may raise the state."""
        q.transition_runtime_state("DRAINING", who="nobody-in-particular")
        q.transition_runtime_state("QUARANTINED", who="nobody-in-particular")
        q.transition_runtime_state("STOPPED", who="nobody-in-particular")
        assert q.get_runtime_state()["state"] == "STOPPED"

    def test_quarantine_entered_draining_cannot_be_loosened_by_its_setter(self, q):
        q.transition_runtime_state("DRAINING", who="runtime",
                                   reason="quarantine trigger: checkout_moved_since_start")
        with pytest.raises(RuntimeTransitionRefused):
            q.transition_runtime_state("ACTIVE", who="runtime")

    def test_epoch_increments_on_every_transition(self, q):
        e0 = q.get_runtime_state()["epoch"]
        e1 = q.transition_runtime_state("DRAINING", who="operator")["epoch"]
        e2 = q.transition_runtime_state("STOPPED", who="operator")["epoch"]
        assert e1 == e0 + 1 and e2 == e1 + 1

    def test_events_are_appended_and_immutable(self, q):
        q.transition_runtime_state("QUARANTINED", who="operator", reason="build lacks baseline",
                                   evidence="build.commit=d073a59")
        events = q.runtime_state_events()
        assert events[0]["from_state"] == "ACTIVE"
        assert events[0]["to_state"] == "QUARANTINED"
        assert events[0]["direction"] == "tighten"
        conn = sqlite3.connect(q._db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE runtime_state_events SET to_state='ACTIVE'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM runtime_state_events")
        finally:
            conn.close()

    def test_unknown_state_is_rejected(self, q):
        with pytest.raises(ValueError):
            q.transition_runtime_state("PARTY_MODE", who="owner", decision_id="D-1")

    def test_stopped_equals_paused_for_314_callers(self, q):
        q.transition_runtime_state("STOPPED", who="operator", reason="SEV-0")
        assert q.get_stop_epoch()["paused"] is True
        assert q._stop_active_precheck() is True
        q.transition_runtime_state("ACTIVE", who="owner", decision_id="D-51-9")
        assert q.get_stop_epoch()["paused"] is False
        assert q._stop_active_precheck() is False

    def test_legacy_set_stop_epoch_still_moves_the_state(self, q):
        q.set_stop_epoch(True, incident="SEV-0")
        assert q.get_runtime_state()["state"] == "STOPPED"
        q.set_stop_epoch(False)
        assert q.get_runtime_state()["state"] == "ACTIVE"
        assert [e["who"] for e in q.runtime_state_events()][:2] == \
               ["legacy:set_stop_epoch", "legacy:set_stop_epoch"]

    def test_draining_and_quarantine_gate_enqueue_and_claim(self, q):
        """P6: DRAINING and QUARANTINED BLOCK enqueue and refuse claim, which the
        #314 boolean could not express."""
        for state in ("DRAINING", "QUARANTINED"):
            fresh = TaskQueue(q._db_path)
            fresh.transition_runtime_state(state, who="operator")
            assert fresh._stop_active_precheck() is True
            fresh.transition_runtime_state("STOPPED", who="operator")
            fresh.transition_runtime_state("ACTIVE", who="owner", decision_id="D-51-9")

    def test_pause_json_is_tighten_only(self, tmp_path, monkeypatch):
        """pause.json may raise the effective state, never lower it (P6)."""
        monkeypatch.setattr("agent_crew.pause.is_paused", lambda *a, **k: False)
        qq = TaskQueue(str(tmp_path / "t.db"))
        assert qq.get_runtime_state()["effective_state"] == "ACTIVE"
        monkeypatch.setattr("agent_crew.pause.is_paused", lambda *a, **k: True)
        state = qq.get_runtime_state()
        assert state["state"] == "ACTIVE"            # the stored row is untouched
        assert state["effective_state"] == "STOPPED"  # the gates see the tightening
        # and it cannot loosen a stored QUARANTINED
        qq.transition_runtime_state("QUARANTINED", who="operator")
        monkeypatch.setattr("agent_crew.pause.is_paused", lambda *a, **k: False)
        assert qq.get_runtime_state()["effective_state"] == "QUARANTINED"

    def test_unreadable_state_is_treated_as_stopped(self, q, monkeypatch):
        """P7: "Runtime state unreadable ⇒ treated as STOPPED"."""
        monkeypatch.setattr(TaskQueue, "_read_stop_row",
                            lambda self, conn: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
        assert q.get_runtime_state()["state"] == "STOPPED"
        assert q.get_runtime_state()["read_failed"] is True


# ── T3 validator: the P3 / P6 / P7 tables ───────────────────────────────────

def outcome(receipt, point, **kw) -> ValidationOutcome:
    return validate(receipt, point, CurrentInputs(**kw)).outcome


class TestValidatorHappyPath:
    def test_enqueue_allows_a_fresh_allow_receipt(self):
        r = make_receipt()
        assert outcome(r, ValidationPoint.ENQUEUE, binding=make_binding()) is ValidationOutcome.OK

    def test_ok_is_proceed(self):
        assert ValidationOutcome.OK is ValidationOutcome.PROCEED
        assert ValidationOutcome.READMIT is ValidationOutcome.RE_ADMIT

    def test_claim_dispatch_execute_result_chain(self):
        nonce = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}
        assert outcome(make_receipt(state="QUEUED"), ValidationPoint.CLAIM,
                       binding=make_binding(), claimant="claude") is ValidationOutcome.OK
        assert outcome(make_receipt(state="CLAIMED"), ValidationPoint.DISPATCH,
                       binding=make_binding()) is ValidationOutcome.OK
        assert outcome(make_receipt(state="CLAIMED", dispatch_nonces=[nonce]),
                       ValidationPoint.EXECUTE_START, binding=make_binding(),
                       presented_nonce="n-1", nonce_unused=True,
                       presenter="claude") is ValidationOutcome.OK
        assert outcome(make_receipt(state="RUNNING", dispatch_nonces=[nonce]),
                       ValidationPoint.RESULT, binding=make_binding(),
                       presented_nonce="n-1", presenter="claude") is ValidationOutcome.OK


class TestValidatorSchemaAndLifecycle:
    def test_a_receipt_that_is_not_the_contract_is_not_a_receipt(self):
        res = validate(make_receipt(decision="MAYBE"), ValidationPoint.ENQUEUE,
                       CurrentInputs(binding=make_binding()))
        assert res.outcome is ValidationOutcome.BLOCK
        assert res.reason.startswith("RECEIPT_SCHEMA_INVALID")

    @pytest.mark.parametrize("state, code", [
        ("CONSUMED", "RECEIPT_CONSUMED"), ("SUPERSEDED", "RECEIPT_SUPERSEDED"),
        ("REVOKED", "RECEIPT_REVOKED")])
    def test_terminal_receipts_never_re_enter(self, state, code):
        res = validate(make_receipt(state=state), ValidationPoint.CLAIM,
                       CurrentInputs(binding=make_binding(), claimant="claude"))
        assert res.outcome is ValidationOutcome.BLOCK and res.reason.startswith(code)

    def test_wrong_state_for_the_point_is_blocked(self):
        res = validate(make_receipt(state="ISSUED"), ValidationPoint.DISPATCH,
                       CurrentInputs(binding=make_binding()))
        assert res.outcome is ValidationOutcome.BLOCK
        assert res.reason.startswith("RECEIPT_STATE_INVALID")

    def test_held_receipt_returns_to_the_engine(self):
        res = validate(make_receipt(state="HELD"), ValidationPoint.DISPATCH,
                       CurrentInputs(binding=make_binding()))
        assert res.outcome is ValidationOutcome.HELD


class TestValidatorP2aIdentity:
    def test_unsigned_receipt_must_say_why(self):
        r = make_receipt(downgrade_reason=None)
        res = validate(r, ValidationPoint.ENQUEUE, CurrentInputs(binding=make_binding()))
        assert res.outcome is ValidationOutcome.BLOCK and res.reason.startswith("RECEIPT_UNSIGNED")

    def test_identity_dependent_allow_under_unverified_binding_is_blocked(self):
        """P2a: role separation is an identity claim; it may be REVIEW or
        HUMAN_GATE while the binding is UNVERIFIED, never ALLOW."""
        r = make_receipt(required_reviewer="codex", decision="ALLOW")
        res = validate(r, ValidationPoint.ENQUEUE, CurrentInputs(binding=make_binding()))
        assert res.outcome is ValidationOutcome.BLOCK
        assert res.reason.startswith("IDENTITY_DEPENDENT_ALLOW_UNVERIFIED")

    def test_same_receipt_as_review_is_admissible(self):
        r = make_receipt(required_reviewer="codex", decision="REVIEW")
        assert outcome(r, ValidationPoint.ENQUEUE, binding=make_binding()) is ValidationOutcome.OK

    def test_verified_binding_allows_it(self):
        r = make_receipt(required_reviewer="codex", decision="ALLOW",
                         executor_binding_status="VERIFIED", caller_identity_status="VERIFIED",
                         signature={"alg": "ed25519", "key_id": "engine-1", "value": "sig",
                                    "status": "VERIFIED"})
        assert outcome(r, ValidationPoint.ENQUEUE, binding=make_binding()) is ValidationOutcome.OK

    def test_review_without_a_reviewer_is_not_admissible(self):
        res = validate(make_receipt(decision="REVIEW"), ValidationPoint.ENQUEUE,
                       CurrentInputs(binding=make_binding()))
        assert res.reason.startswith("REVIEW_WITHOUT_REVIEWER")

    def test_block_and_human_gate_receipts_authorise_nothing(self):
        assert outcome(make_receipt(decision="BLOCK"), ValidationPoint.ENQUEUE,
                       binding=make_binding()) is ValidationOutcome.BLOCK
        assert outcome(make_receipt(decision="HUMAN_GATE"), ValidationPoint.ENQUEUE,
                       binding=make_binding()) is ValidationOutcome.HUMAN_GATE


class TestValidatorNonce:
    NONCE = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}

    def test_execute_start_without_a_nonce_is_blocked(self):
        res = validate(make_receipt(state="CLAIMED"), ValidationPoint.EXECUTE_START,
                       CurrentInputs(binding=make_binding()))
        assert res.reason.startswith("NONCE_MISSING")

    def test_unknown_nonce_is_blocked(self):
        res = validate(make_receipt(state="CLAIMED", dispatch_nonces=[self.NONCE]),
                       ValidationPoint.EXECUTE_START,
                       CurrentInputs(binding=make_binding(), presented_nonce="n-2"))
        assert res.reason.startswith("NONCE_UNKNOWN")

    def test_reused_nonce_is_blocked(self):
        res = validate(make_receipt(state="CLAIMED", dispatch_nonces=[self.NONCE]),
                       ValidationPoint.EXECUTE_START,
                       CurrentInputs(binding=make_binding(), presented_nonce="n-1",
                                     nonce_unused=False))
        assert res.reason.startswith("NONCE_REUSED")

    def test_nonce_from_another_attempt_is_blocked(self):
        nonce = dict(self.NONCE, attempt=1)
        res = validate(make_receipt(state="CLAIMED", attempt=2, dispatch_nonces=[nonce]),
                       ValidationPoint.EXECUTE_START,
                       CurrentInputs(binding=make_binding(), presented_nonce="n-1",
                                     nonce_unused=True))
        assert res.reason.startswith("NONCE_WRONG_ATTEMPT")

    def test_result_from_the_wrong_executor_is_blocked(self):
        res = validate(make_receipt(state="RUNNING", dispatch_nonces=[self.NONCE]),
                       ValidationPoint.RESULT,
                       CurrentInputs(binding=make_binding(), presented_nonce="n-1",
                                     presenter="gemini"))
        assert res.reason.startswith("EXECUTOR_BINDING_MISMATCH")


class TestP6EnforcementMatrix:
    """Every cell of the P6 matrix, as a test."""

    CELLS = [
        ("ACTIVE", "enqueue", ValidationOutcome.OK), ("ACTIVE", "claim", ValidationOutcome.OK),
        ("ACTIVE", "dispatch", ValidationOutcome.OK), ("ACTIVE", "execute_start", ValidationOutcome.OK),
        ("ACTIVE", "result", ValidationOutcome.OK),
        ("DRAINING", "enqueue", ValidationOutcome.BLOCK), ("DRAINING", "claim", ValidationOutcome.HELD),
        ("DRAINING", "dispatch", ValidationOutcome.HELD),          # not already claimed
        ("DRAINING", "execute_start", ValidationOutcome.HELD),     # not already dispatched
        ("DRAINING", "result", ValidationOutcome.OK),
        ("QUARANTINED", "enqueue", ValidationOutcome.BLOCK),
        ("QUARANTINED", "claim", ValidationOutcome.BLOCK),
        ("QUARANTINED", "dispatch", ValidationOutcome.BLOCK),
        ("QUARANTINED", "execute_start", ValidationOutcome.BLOCK),
        ("QUARANTINED", "result", ValidationOutcome.OK),           # accepted, flagged
        ("STOPPED", "enqueue", ValidationOutcome.BLOCK), ("STOPPED", "claim", ValidationOutcome.BLOCK),
        ("STOPPED", "dispatch", ValidationOutcome.BLOCK),
        ("STOPPED", "execute_start", ValidationOutcome.BLOCK),
        ("STOPPED", "result", ValidationOutcome.OK),
    ]

    @pytest.mark.parametrize("state, point, expected", CELLS)
    def test_cell(self, state, point, expected):
        receipt_state = {"enqueue": "ISSUED", "claim": "QUEUED", "dispatch": "CLAIMED",
                         "execute_start": "CLAIMED", "result": "RUNNING"}[point]
        nonce = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}
        r = make_receipt(state=receipt_state, dispatch_nonces=[nonce],
                         binding=make_binding(runtime_state=state))
        res = validate(r, point, CurrentInputs(binding=make_binding(runtime_state=state),
                                               presented_nonce="n-1", nonce_unused=True,
                                               presenter="claude", claimant="claude"))
        assert res.outcome is expected, res.reason

    def test_draining_dispatches_what_was_already_claimed(self):
        r = make_receipt(state="CLAIMED", binding=make_binding(runtime_state="DRAINING"))
        res = validate(r, ValidationPoint.DISPATCH,
                       CurrentInputs(binding=make_binding(runtime_state="DRAINING"),
                                     already_claimed=True))
        assert res.outcome is ValidationOutcome.OK

    def test_draining_starts_what_was_already_dispatched(self):
        nonce = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}
        r = make_receipt(state="CLAIMED", dispatch_nonces=[nonce],
                         binding=make_binding(runtime_state="DRAINING"))
        res = validate(r, ValidationPoint.EXECUTE_START,
                       CurrentInputs(binding=make_binding(runtime_state="DRAINING"),
                                     already_dispatched=True, presented_nonce="n-1",
                                     nonce_unused=True))
        assert res.outcome is ValidationOutcome.OK

    @pytest.mark.parametrize("state", ["QUARANTINED", "STOPPED"])
    def test_result_in_a_stopped_runtime_suppresses_successors(self, state):
        nonce = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}
        r = make_receipt(state="RUNNING", dispatch_nonces=[nonce],
                         binding=make_binding(runtime_state=state))
        res = validate(r, ValidationPoint.RESULT,
                       CurrentInputs(binding=make_binding(runtime_state=state),
                                     presented_nonce="n-1", presenter="claude"))
        assert res.outcome is ValidationOutcome.OK
        assert "RESULT_ACCEPTED_NO_SUCCESSORS" in res.reason


class TestP3OutcomeTable:
    """Each row of the P3 "field changed between issue and now" table."""

    def _claim(self, **b_now):
        r = make_receipt(state="QUEUED")
        return validate(r, ValidationPoint.CLAIM,
                        CurrentInputs(binding=make_binding(**b_now), claimant="claude"))

    def test_policy_generation_change_re_admits(self):
        res = self._claim(policy_generation=8)
        assert res.outcome is ValidationOutcome.READMIT and "policy_generation" in res.changed_fields

    def test_policy_hash_change_re_admits(self):
        assert self._claim(policy_hash="sha256:" + "f" * 64).outcome is ValidationOutcome.READMIT

    def test_edited_decision_body_re_admits(self):
        res = self._claim(source_decision_revs=[{"decision_id": "D-51-1",
                                                 "body_hash": "sha256:" + "9" * 64}])
        assert res.outcome is ValidationOutcome.READMIT
        assert "source_decision_revs" in res.changed_fields

    def test_registry_generation_change_re_admits(self):
        res = self._claim(capability_registry={"generation": 4, "hash": "sha256:" + "c" * 64})
        assert res.outcome is ValidationOutcome.READMIT

    def test_new_capability_owner_re_admits(self):
        res = self._claim(matched_capability={"id": "cap.dispatch", "owner": "quota-core"})
        assert res.outcome is ValidationOutcome.READMIT
        assert "matched_capability" in res.changed_fields

    def test_runtime_epoch_advanced_blocks_and_never_resumes(self):
        res = self._claim(runtime_state_epoch=13)
        assert res.outcome is ValidationOutcome.BLOCK
        assert res.reason.startswith("RUNTIME_EPOCH_ADVANCED")

    def test_epoch_that_passed_through_a_non_active_state_blocks(self):
        r = make_receipt(state="QUEUED")
        res = validate(r, ValidationPoint.CLAIM,
                       CurrentInputs(binding=make_binding(), epoch_passed_non_active=True))
        assert res.outcome is ValidationOutcome.BLOCK

    def test_granted_gate_revoked_blocks(self):
        r = make_receipt(state="QUEUED", required_reviewer=None,
                         human_gate_state={"state": "GRANTED", "decision_id": "D-9"},
                         decision="REVIEW",
                         binding=make_binding(human_gate_state={"state": "GRANTED",
                                                                "decision_id": "D-9"}))
        res = validate(r, ValidationPoint.CLAIM,
                       CurrentInputs(binding=make_binding(human_gate_state="DENIED")))
        assert res.outcome is ValidationOutcome.BLOCK and res.reason.startswith("HUMAN_GATE_REVOKED")

    def test_pending_gate_holds_as_human_gate(self):
        res = self._claim(human_gate_state="PENDING")
        assert res.outcome is ValidationOutcome.HUMAN_GATE

    def test_exhausted_budget_defers(self):
        res = self._claim(budget_class="EXHAUSTED")
        assert res.outcome is ValidationOutcome.HELD and res.reason.startswith("BUDGET")

    def test_receipt_older_than_max_age_re_admits(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 25 * 3600))
        r = make_receipt(state="QUEUED", issued_at=old)
        res = validate(r, ValidationPoint.CLAIM, CurrentInputs(binding=make_binding()))
        assert res.outcome is ValidationOutcome.READMIT and res.reason.startswith("RECEIPT_TOO_OLD")

    def test_a_23h_receipt_with_an_unchanged_binding_is_still_current(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 23 * 3600))
        r = make_receipt(state="QUEUED", issued_at=old)
        assert validate(r, ValidationPoint.CLAIM,
                        CurrentInputs(binding=make_binding())).outcome is ValidationOutcome.OK

    def test_o18_immediate_invalidation_fields_are_the_binding_fields(self):
        """Age is the backstop; these fields invalidate the moment they move."""
        assert set(O18_IMMEDIATE_INVALIDATION_FIELDS) == set(make_binding().keys())
        assert O18_MAX_RECEIPT_AGE_SECONDS == 24 * 60 * 60

    @pytest.mark.parametrize("field, value", [
        ("policy_generation", 8), ("policy_hash", "sha256:" + "f" * 64),
        ("source_decision_revs", [{"decision_id": "D-51-1", "body_hash": "sha256:" + "9" * 64}]),
        ("capability_registry", {"generation": 9, "hash": "sha256:" + "c" * 64}),
        ("matched_capability", {"id": "cap.other", "owner": "agent_crew"}),
        ("runtime_state", "QUARANTINED"), ("runtime_state_epoch", 99),
        ("human_gate_state", "PENDING"), ("budget_class", "EXHAUSTED"),
    ])
    def test_every_immediate_field_invalidates_a_one_minute_old_receipt(self, field, value):
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        r = make_receipt(state="QUEUED", issued_at=fresh)
        res = validate(r, ValidationPoint.CLAIM,
                       CurrentInputs(binding=make_binding(**{field: value}), claimant="claude"))
        assert res.outcome is not ValidationOutcome.OK, f"{field} did not invalidate"

    def test_in_flight_result_is_accepted_and_flagged_stale(self):
        """P3: "no automatic kill ... its /result is accepted but marked stale"."""
        nonce = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}
        r = make_receipt(state="RUNNING", dispatch_nonces=[nonce])
        res = validate(r, ValidationPoint.RESULT,
                       CurrentInputs(binding=make_binding(policy_generation=99),
                                     presented_nonce="n-1", presenter="claude"))
        assert res.outcome is ValidationOutcome.OK
        assert res.reason.startswith("STALE_RECEIPT")


class TestP7AndO20:
    def test_uncomputable_binding_fails_closed_at_admission(self):
        res = validate(make_receipt(), ValidationPoint.ENQUEUE, CurrentInputs(binding=None))
        assert res.outcome is ValidationOutcome.BLOCK and res.reason.startswith("INPUTS_UNAVAILABLE")

    def test_uncomputable_binding_holds_authorised_work(self):
        res = validate(make_receipt(state="QUEUED"), ValidationPoint.CLAIM,
                       CurrentInputs(binding=None, unavailable_inputs=("e4_registry",)))
        assert res.outcome is ValidationOutcome.HELD

    def test_in_flight_result_is_never_lost_to_an_unavailable_input(self):
        nonce = {"nonce": "n-1", "attempt": 1, "issued_at": "2026-09-23T10:00:00Z", "used_at": None}
        res = validate(make_receipt(state="RUNNING", dispatch_nonces=[nonce]),
                       ValidationPoint.RESULT,
                       CurrentInputs(binding=None, presented_nonce="n-1", presenter="claude"))
        assert res.outcome is ValidationOutcome.OK

    def test_snapshot_inside_the_window_is_bounded_stale_not_blocked(self):
        """O20: "the last signed snapshot remains the input until snapshot_max_age"."""
        res = validate(make_receipt(), ValidationPoint.ENQUEUE,
                       CurrentInputs(binding=make_binding(), snapshot_available=False,
                                     snapshot_age_seconds=O20_SNAPSHOT_MAX_AGE_SECONDS - 1))
        assert res.outcome is ValidationOutcome.OK

    def test_snapshot_past_the_window_fails_closed_at_admission(self):
        res = validate(make_receipt(), ValidationPoint.ENQUEUE,
                       CurrentInputs(binding=make_binding(), snapshot_available=False,
                                     snapshot_age_seconds=O20_SNAPSHOT_MAX_AGE_SECONDS + 1))
        assert res.outcome is ValidationOutcome.BLOCK and res.reason.startswith("SNAPSHOT_STALE")

    def test_snapshot_past_the_window_holds_authorised_work(self):
        res = validate(make_receipt(state="QUEUED"), ValidationPoint.CLAIM,
                       CurrentInputs(binding=make_binding(), snapshot_available=False,
                                     snapshot_age_seconds=O20_SNAPSHOT_MAX_AGE_SECONDS + 1))
        assert res.outcome is ValidationOutcome.HELD

    def test_unknown_snapshot_age_is_treated_as_past_the_window(self):
        res = validate(make_receipt(), ValidationPoint.ENQUEUE,
                       CurrentInputs(binding=make_binding(), snapshot_available=False,
                                     snapshot_age_seconds=None))
        assert res.outcome is ValidationOutcome.BLOCK

    def test_the_window_is_fifteen_minutes(self):
        assert O20_SNAPSHOT_MAX_AGE_SECONDS == 15 * 60


class TestRuntimeStatesConstant:
    def test_states_are_the_four_the_adr_names(self):
        assert RUNTIME_STATES == ("ACTIVE", "DRAINING", "QUARANTINED", "STOPPED")


def test_receipt_round_trips_through_the_store_and_the_validator(tmp_path):
    """End to end for step 1: a receipt the store accepted is a receipt the
    validator accepts, and the store's lifecycle rows drive the points."""
    conn = sqlite3.connect(tmp_path / "e2e.db")
    conn.row_factory = sqlite3.Row
    cea_store.ensure_schema(conn)
    r = make_receipt()
    cea_store.record_receipt(conn, r)
    assert validate(cea_store.current_receipt(conn, r["receipt_id"]), ValidationPoint.ENQUEUE,
                    CurrentInputs(binding=make_binding())).outcome is ValidationOutcome.OK
    cea_store.append_lifecycle(conn, r["receipt_id"], "QUEUED")
    assert validate(cea_store.current_receipt(conn, r["receipt_id"]), ValidationPoint.CLAIM,
                    CurrentInputs(binding=make_binding(), claimant="claude")).outcome \
        is ValidationOutcome.OK
    nonce = {"nonce": "n-1", "attempt": 1, "issued_at": r["issued_at"], "used_at": None}
    cea_store.mint_nonce(conn, r["receipt_id"], 1, "n-1", r["issued_at"])
    cea_store.append_lifecycle(conn, r["receipt_id"], "CLAIMED", mutate={"dispatch_nonces": [nonce]})
    current = cea_store.current_receipt(conn, r["receipt_id"])
    assert validate(current, ValidationPoint.EXECUTE_START,
                    CurrentInputs(binding=make_binding(), presented_nonce="n-1",
                                  nonce_unused=True, presenter="claude")).outcome \
        is ValidationOutcome.OK
    assert cea_store.consume_nonce(conn, "n-1", used_by="claude") is True
    cea_store.append_lifecycle(conn, r["receipt_id"], "CONSUMED")
    assert validate(cea_store.current_receipt(conn, r["receipt_id"]), ValidationPoint.CLAIM,
                    CurrentInputs(binding=make_binding())).outcome is ValidationOutcome.BLOCK
    assert json.loads(conn.execute(
        "SELECT receipt_json FROM authorization_receipts ORDER BY seq LIMIT 1"
    ).fetchone()[0])["state"] == "ISSUED"   # the first row is never rewritten
    conn.close()
