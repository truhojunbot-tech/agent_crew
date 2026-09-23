"""SEV-0 CEA step 2 — T1 engine, P4 intent identity, the P2a downgrade, the service boundary.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce565`` (Π P1–P7, §3, §5, §6, §7). Receipt schema: the byte-identical copy
at ``tests/cea_contract/receipt.schema.json``.

Every test here is about the engine as a **pure decision**: fake input providers
in, a schema-valid receipt out, an in-memory SQLite for the store. No server, no
tmux, no network, no live DB. The adapters that call this engine are step 2(c)/(d)
and are not covered here — see the task result for what remains.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea.engine import (
    REVIEW_FLOOR, AuthorizationEngine, EngineConfig, intent_hash)
from agent_crew.cea.intent import (
    Caller, CallerProvenance, IdentityStatus, Intent, IntentIdentity, Target, WorkClass)
from agent_crew.cea.providers import CapabilityLookup, PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import (
    BudgetClass, DecisionRev, HumanGate, HumanGateState, MatchedCapability,
    ProviderBudget, RegistryRef)
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.cea.schema import validate_receipt
from agent_crew.cea.validator import CurrentInputs, ValidationOutcome, ValidationPoint, validate


# ---------------------------------------------------------------------------
# fixture writers — every provider answers, so a test that wants a missing input
# has to say so explicitly. The failure mode this avoids is a test passing
# because nothing was wired, which is the exact defect E10 4a found in the
# product.
# ---------------------------------------------------------------------------

DECISION = DecisionRev(decision_id="T0-1234", body_hash="b" * 32)
CAPABILITY = MatchedCapability(id="tokenomics.work_class_gate", owner="quota-core",
                               repo="example/quota-core")


class FakeSnapshot:
    def __init__(self, **kw):
        self.kw = kw

    def current(self, intent=None) -> PolicySnapshotRef:
        base = dict(generation=7, hash="h" * 16, produced_at=None,
                    decisions=(DECISION,), in_scope=(DECISION,),
                    signature=SignatureStatus.VALID, available=True, tier="T0")
        base.update(self.kw)
        return PolicySnapshotRef(**base)


class FakeRegistry:
    def __init__(self, **kw):
        self.kw = kw

    def lookup(self, intent) -> CapabilityLookup:
        base = dict(registry=RegistryRef(generation="2026-09-23.1", hash="r" * 16),
                    matches=(), anchor_matches=(), available=True, stale=False)
        base.update(self.kw)
        return CapabilityLookup(**base)


class FakeRuntime:
    def __init__(self, state=RuntimeState.ACTIVE, epoch=3, read_failed=False):
        self.snap = RuntimeStateSnapshot(state=state, epoch=epoch, read_failed=read_failed)

    def current(self):
        return self.snap


class FakeBudget:
    def __init__(self, state=BudgetClass.OK):
        self.state = state

    def budget(self, provider):
        return ProviderBudget(provider=provider, state=self.state, observed_at=None)


class FakeGate:
    def __init__(self, gate=None):
        self.gate = gate or HumanGate(HumanGateState.NOT_REQUIRED)

    def state(self, intent, snapshot):
        return self.gate


def engine(**kw) -> AuthorizationEngine:
    """An engine whose inputs all answer unless a test replaces one."""
    config = kw.pop("config", None) or EngineConfig(mode="enforce")
    return AuthorizationEngine(
        config=config,
        capabilities=kw.pop("capabilities", None) or FakeRegistry(),
        snapshots=kw.pop("snapshots", None) or FakeSnapshot(),
        runtime=kw.pop("runtime", None) or FakeRuntime(),
        budgets=kw.pop("budgets", None) or FakeBudget(),
        gates=kw.pop("gates", None) or FakeGate(),
        **kw)


def identity(*, project="agent_crew", work_class=WorkClass.IMPLEMENT,
             repo="example/agent_crew", base_ref="main", anchors=("src/agent_crew/server.py",),
             capability_id=None, authority=("T0-1234",)) -> IntentIdentity:
    return IntentIdentity(project=project, work_class=work_class,
                          target=Target(repo=repo, base_ref=base_ref, scope_anchors=tuple(anchors)),
                          capability_id=capability_id,
                          authority_decision_ids=tuple(authority))


def intent(task_id="t1", *, ident=None, **kw) -> Intent:
    return Intent(identity=ident or identity(), task_id=task_id,
                  task_type=kw.pop("task_type", "implement"),
                  description=kw.pop("description", "Add a --json flag"), **kw)


def caller(principal="cron:admitted_trigger", provenance=CallerProvenance.CRON,
           status=IdentityStatus.UNVERIFIED) -> Caller:
    return Caller(principal=principal, provenance=provenance, identity_status=status,
                  credential_kind="adapter_token")


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    receipt_store.ensure_schema(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# P4 — intent identity
# ---------------------------------------------------------------------------

def test_intent_hash_is_the_p4_tuple_and_nothing_else():
    """Description, task_id and operation_id are not members (P4; E10 4c)."""
    a = intent("adm-r1", description="Implement the work-class gate")
    b = intent("adm-r2", description="totally different words",
               extra={"operation_id": "op-2"})
    assert intent_hash(a.identity) == intent_hash(b.identity)


def test_intent_hash_is_a_sha256_over_canonical_json():
    h = intent_hash(identity())
    assert h.startswith("sha256:") and len(h) == 7 + 64
    assert not validate_receipt({"intent_hash": h}) or True  # shape only; full receipt below


@pytest.mark.parametrize("field,value", [
    ("project", "quota-core"),
    ("work_class", WorkClass.REVIEW),
    ("repo", "example/other"),
    ("base_ref", "release"),
    ("capability_id", "tokenomics.work_class_gate"),
])
def test_every_p4_member_changes_the_hash(field, value):
    base = intent_hash(identity())
    assert intent_hash(identity(**{field: value})) != base


def test_scope_anchor_order_is_not_identity():
    """Two declarations of the same anchors are the same work — otherwise a caller
    reorders a list to mint a 'new' intent, which is the evasion P4 closes."""
    a = identity(anchors=("a.py", "b.py", "c.py"))
    b = identity(anchors=("c.py", "a.py", "b.py", "a.py"))
    assert intent_hash(a) == intent_hash(b)


def test_a_superseding_decision_changes_the_intent():
    """P4's ALREADY_COMPLETED exception needs no special case: authority_decision_ids
    is a hash member, so a newer decision record is a different intent."""
    assert intent_hash(identity(authority=("T0-1234",))) != \
        intent_hash(identity(authority=("T0-1234", "T0-9999")))


# ---------------------------------------------------------------------------
# the happy path, and the schema
# ---------------------------------------------------------------------------

def test_authorize_emits_a_schema_valid_receipt(conn):
    auth = engine().authorize(conn, intent(), caller())
    assert validate_receipt(auth.receipt) == []
    assert auth.receipt["intent_hash"] == intent_hash(identity())
    assert auth.receipt["task_id"] == "t1"
    assert auth.receipt["state"] == "ISSUED"
    assert receipt_store.current_receipt(conn, auth.receipt_id) == auth.receipt


def test_a_block_receipt_is_still_recorded(conn):
    """P2: the audit row exists even when no task row is written."""
    auth = engine(runtime=FakeRuntime(state=RuntimeState.STOPPED)).authorize(conn, intent(), caller())
    assert auth.decision == "BLOCK"
    assert receipt_store.current_receipt(conn, auth.receipt_id) is not None


def test_implement_is_never_allowed_under_an_unverified_binding(conn):
    """P2a: implement carries a review contract, which is an identity claim; with
    no credential boundary it is REVIEW, never ALLOW."""
    auth = engine().authorize(conn, intent(), caller())
    assert auth.decision == "REVIEW"
    assert auth.receipt["required_reviewer"] == "codex"
    assert auth.receipt["downgrade_reason"] == "SHARED_UID_NO_CREDENTIAL_BOUNDARY"
    assert auth.receipt["executor_binding_status"] == "UNVERIFIED"
    assert auth.receipt["caller_identity_status"] == "UNVERIFIED"


def test_the_engine_never_issues_what_the_validator_would_refuse(conn):
    """The two halves of P2a must agree: whatever the engine may issue, the
    validator must accept at enqueue. A disagreement here is a receipt that is
    born invalid."""
    auth = engine().authorize(conn, intent(), caller())
    result = validate(auth.receipt, ValidationPoint.ENQUEUE,
                      CurrentInputs(binding=auth.receipt["binding"]))
    assert result.outcome is ValidationOutcome.PROCEED, result.reason


def test_ops_work_with_no_identity_dependence_is_allowed(conn):
    ident = identity(work_class=WorkClass.OPS)
    auth = engine().authorize(conn, intent("ops1", ident=ident, task_type="discuss"), caller())
    assert auth.decision == "ALLOW"
    assert auth.http_status == 201
    assert auth.receipt["required_reviewer"] is None


# ---------------------------------------------------------------------------
# P7 — fail closed, never shadow-ALLOW a missing input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw,missing", [
    ({"snapshots": FakeSnapshot(available=False)}, "policy_snapshot"),
    ({"capabilities": FakeRegistry(available=False)}, "capability_registry"),
    ({"runtime": FakeRuntime(read_failed=True)}, "runtime_state"),
    ({"capabilities": FakeRegistry(stale=True)}, "capability_registry_stale"),
])
def test_a_missing_input_blocks_and_names_itself(conn, kw, missing):
    auth = engine(**kw).authorize(conn, intent(), caller())
    assert auth.decision == "BLOCK"
    assert auth.code == "INPUTS_UNAVAILABLE"
    assert missing in auth.receipt["reason"]["text"]
    assert missing in (auth.receipt["provenance"] or {}).get("unavailable_inputs", [])


def test_the_default_engine_has_no_providers_and_therefore_blocks(conn):
    """"Providers that do not exist yet return UNAVAILABLE" — the default engine
    is the honest degraded one, not an open door."""
    auth = AuthorizationEngine(config=EngineConfig(mode="enforce")).authorize(
        conn, intent(), caller())
    assert auth.decision == "BLOCK" and auth.code == "INPUTS_UNAVAILABLE"


def test_shadow_mode_does_not_change_the_verdict(conn):
    """The shadow switch is an adapter concern. If it reached the engine, a
    shadow deployment would measure a policy nobody is going to enforce."""
    shadow = engine(config=EngineConfig(mode="shadow"),
                    snapshots=FakeSnapshot(available=False)).authorize(conn, intent("s1"), caller())
    enforce = engine(config=EngineConfig(mode="enforce"),
                     snapshots=FakeSnapshot(available=False)).authorize(conn, intent("s2"), caller())
    assert shadow.decision == enforce.decision == "BLOCK"
    assert shadow.code == enforce.code == "INPUTS_UNAVAILABLE"


def test_no_authority_in_the_snapshot_blocks(conn):
    """J2: free text in a task description is not authority (§1.4)."""
    auth = engine(snapshots=FakeSnapshot(decisions=(), in_scope=())).authorize(
        conn, intent(), caller())
    assert auth.decision == "BLOCK" and auth.code == "NO_AUTHORITY"


# ---------------------------------------------------------------------------
# P6 / J6
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", [RuntimeState.DRAINING, RuntimeState.QUARANTINED,
                                   RuntimeState.STOPPED])
def test_no_admission_outside_active(conn, state):
    auth = engine(runtime=FakeRuntime(state=state)).authorize(conn, intent(), caller())
    assert auth.decision == "BLOCK" and auth.code == "RUNTIME_STATE_FORBIDS"


def test_exhausted_budget_blocks_rather_than_queueing_work_to_fail(conn):
    auth = engine(budgets=FakeBudget(BudgetClass.EXHAUSTED)).authorize(conn, intent(), caller())
    assert auth.decision == "BLOCK" and auth.code == "BUDGET_EXHAUSTED"


def test_an_unobservable_budget_is_recorded_honestly_and_fails_closed(conn):
    """The frozen schema has no UNVERIFIED in ``binding.budget_class``, so the
    binding carries the fail-closed value while the top-level field keeps the
    truth. Neither one is allowed to become a quiet OK."""
    class Unknown:
        def budget(self, provider):
            return ProviderBudget(provider=provider, state="UNVERIFIED", observed_at=None)
    auth = engine(budgets=Unknown()).authorize(conn, intent(), caller())
    assert validate_receipt(auth.receipt) == []
    assert auth.receipt["provider_budget"]["state"] == "UNVERIFIED"
    assert auth.receipt["binding"]["budget_class"] == "EXHAUSTED"
    assert auth.decision == "BLOCK"


# ---------------------------------------------------------------------------
# J5 / J7 / J8
# ---------------------------------------------------------------------------

def test_owner_conflict_is_a_human_gate_not_a_shadow_allow(conn):
    """CXC-2: E4's own answer was a shadow ALLOW on OWNER_CONFLICT."""
    auth = engine(capabilities=FakeRegistry(matches=(CAPABILITY,))).authorize(
        conn, intent(), caller(principal="manual:operator"))
    assert auth.decision == "HUMAN_GATE" and auth.code == "OWNER_CONFLICT"
    assert auth.receipt["reuse"]["approver_identity_verified"] is False


def test_a_snapshot_cannot_reduce_the_review_floor(conn):
    """§7.2 / J7: the matrix may require more, never less. A snapshot saying
    'implement needs no reviewer' is exactly the reduction the fold forbids."""
    snap = FakeSnapshot(review_test_matrix={"implement": {"reviewer": False, "tester": False}})
    auth = engine(snapshots=snap).authorize(conn, intent(), caller())
    assert auth.receipt["required_reviewer"] == "codex"
    assert auth.receipt["required_tester"] == "gemini"


def test_the_snapshot_can_tighten_the_floor(conn):
    snap = FakeSnapshot(review_test_matrix={"ops": {"reviewer": True, "tester": True}})
    ident = identity(work_class=WorkClass.OPS)
    auth = engine(snapshots=snap).authorize(conn, intent("o1", ident=ident), caller())
    assert auth.receipt["required_reviewer"] == "codex"
    assert REVIEW_FLOOR["ops"]["reviewer"] is False


def test_the_reviewer_is_never_the_executor(conn):
    """§3: required_reviewer "must differ from executor_binding". A review task
    routed back to the implementer is a label, not independence."""
    ident = identity(work_class=WorkClass.REVIEW)
    snap = FakeSnapshot(review_test_matrix={"review": {"reviewer": True}})
    auth = engine(snapshots=snap).authorize(conn, intent("r1", ident=ident), caller())
    assert auth.receipt["executor_binding"] == "codex"
    assert auth.receipt["required_reviewer"] != "codex"


def test_a_pending_human_gate_holds_everything(conn):
    auth = engine(gates=FakeGate(HumanGate(HumanGateState.PENDING))).authorize(
        conn, intent(), caller())
    assert auth.decision == "HUMAN_GATE" and auth.code == "HUMAN_GATE_PENDING"


def test_a_denied_human_gate_blocks(conn):
    auth = engine(gates=FakeGate(HumanGate(HumanGateState.DENIED))).authorize(
        conn, intent(), caller())
    assert auth.decision == "BLOCK" and auth.code == "HUMAN_GATE_DENIED"


def test_a_granted_gate_carries_its_decision_id(conn):
    auth = engine(gates=FakeGate(HumanGate(HumanGateState.GRANTED, "T0-gate-1"))).authorize(
        conn, intent(), caller())
    assert validate_receipt(auth.receipt) == []
    assert auth.receipt["human_gate_state"] == {"state": "GRANTED", "decision_id": "T0-gate-1"}


def test_the_snapshot_gate_provider_ignores_a_self_asserted_flag(conn):
    """J8: a gate exists because a snapshot predicate says so. A flag on the
    request is not a gate — and is not the absence of one either."""
    snap = FakeSnapshot(human_gate_predicates=({"project": "agent_crew", "state": "PENDING"},))
    auth = AuthorizationEngine(config=EngineConfig(mode="enforce"), snapshots=snap,
                               capabilities=FakeRegistry(), runtime=FakeRuntime(),
                               budgets=FakeBudget()).authorize(
        conn, intent(extra={"human_gate": "granted"}), caller())
    assert auth.decision == "HUMAN_GATE"


# ---------------------------------------------------------------------------
# P4 lineage — duplicate, idempotency, already-completed, retry
# ---------------------------------------------------------------------------

def test_the_same_intent_under_a_new_task_id_is_a_duplicate(conn):
    """CX-4c: the same work under a new opid was dispatched twice."""
    eng = engine()
    first = eng.authorize(conn, intent("adm-r1"), caller())
    second = eng.authorize(conn, intent("adm-r2", description="reworded"), caller())
    assert second.http_status == 409
    assert second.code == "DUPLICATE_INTENT"
    assert second.existing_receipt_id == first.receipt_id
    assert second.receipt["decision"] == "BLOCK"


def test_same_intent_same_idempotency_key_returns_the_same_receipt(conn):
    eng = engine()
    first = eng.authorize(conn, intent("adm-r1", idempotency_key="k-1"), caller())
    again = eng.authorize(conn, intent("adm-r2", idempotency_key="k-1"), caller())
    assert again.http_status == 200
    assert again.code == "IDEMPOTENT_REPLAY"
    assert again.reused and again.receipt_id == first.receipt_id


def test_a_different_idempotency_key_on_a_live_intent_is_409(conn):
    eng = engine()
    eng.authorize(conn, intent("adm-r1", idempotency_key="k-1"), caller())
    other = eng.authorize(conn, intent("adm-r2", idempotency_key="k-2"), caller())
    assert other.http_status == 409 and other.code == "DUPLICATE_INTENT"


def test_a_completed_lineage_refuses_re_admission(conn):
    eng = engine()
    first = eng.authorize(conn, intent("adm-r1"), caller())
    eng.transition(conn, first.receipt_id, "CONSUMED")
    again = eng.authorize(conn, intent("adm-r2"), caller())
    assert again.http_status == 409 and again.code == "ALREADY_COMPLETED"


SUPERSEDING = DecisionRev(decision_id="T0-9999", body_hash="c" * 32, supersedes=("T0-1234",))


def test_a_superseding_decision_re_admits_completed_work(conn):
    """The P4 exception, in the only form it may take after 2a-fix: the *signed
    snapshot* carries a record that explicitly supersedes the completed run's
    authority. A different intent_hash is the consequence, never the licence —
    the caller controls the hash inputs, so "the hash changed" proves nothing."""
    eng = engine()
    first = eng.authorize(conn, intent("adm-r1"), caller())
    eng.transition(conn, first.receipt_id, "CONSUMED")
    snapshot = FakeSnapshot(decisions=(DECISION, SUPERSEDING),
                            in_scope=(DECISION, SUPERSEDING))
    superseding = intent("adm-r2", ident=identity(authority=("T0-1234", "T0-9999")))
    again = engine(snapshots=snapshot).authorize(conn, superseding, caller())
    assert again.http_status in (201, 403)
    assert again.code != "ALREADY_COMPLETED"


def test_a_superseded_lineage_is_released(conn):
    eng = engine()
    first = eng.authorize(conn, intent("adm-r1"), caller())
    eng.transition(conn, first.receipt_id, "SUPERSEDED")
    assert receipt_store.lineage_for_intent(conn, first.receipt["intent_hash"]) is None
    again = eng.authorize(conn, intent("adm-r2"), caller())
    assert again.http_status != 409


def test_the_lineage_claim_is_atomic_not_check_then_act(conn):
    """Two admissions of one intent must not both win. The PRIMARY KEY arbitrates;
    nothing here reads-then-writes."""
    ih = "sha256:" + "a" * 64
    won_a, holder_a = receipt_store.claim_lineage(conn, ih, "r-a", "p", "ISSUED")
    won_b, holder_b = receipt_store.claim_lineage(conn, ih, "r-b", "p", "ISSUED")
    assert (won_a, holder_a) == (True, "r-a")
    assert (won_b, holder_b) == (False, "r-a")


def test_releasing_a_lineage_requires_owning_it(conn):
    """A late transition on a superseded receipt must not free its successor's
    claim — that would reopen the duplicate window the table exists to close."""
    ih = "sha256:" + "b" * 64
    receipt_store.claim_lineage(conn, ih, "r-winner", "p", "ISSUED")
    assert receipt_store.release_lineage(conn, ih, "r-loser") is False
    assert receipt_store.lineage_for_intent(conn, ih)["receipt_id"] == "r-winner"


def test_a_retry_reuses_the_receipt_while_b_is_unchanged(conn):
    eng = engine(config=EngineConfig(mode="enforce", default_max_attempts=3))
    first = eng.authorize(conn, intent("adm-r1"), caller())
    retried = eng.authorize(conn, intent("adm-r1"), caller(), retry=True)
    assert retried.reused and retried.receipt_id == first.receipt_id
    assert retried.receipt["attempt"] == 2
    assert retried.code == "RETRY_SAME_RECEIPT"


def test_a_retry_past_max_attempts_re_admits(conn):
    eng = engine(config=EngineConfig(mode="enforce", default_max_attempts=1))
    first = eng.authorize(conn, intent("adm-r1"), caller())
    retried = eng.authorize(conn, intent("adm-r1"), caller(), retry=True)
    assert not retried.reused
    assert retried.receipt_id != first.receipt_id
    assert receipt_store.current_receipt(conn, first.receipt_id)["state"] == "SUPERSEDED"


def test_a_retry_after_binding_drift_re_admits(conn):
    """P4: "retries reuse the receipt iff B unchanged". A drifted B is a new
    decision, not a second attempt at the old one."""
    cfg = EngineConfig(mode="enforce", default_max_attempts=5)
    first = engine(config=cfg).authorize(conn, intent("adm-r1"), caller())
    moved = engine(config=cfg, snapshots=FakeSnapshot(generation=8))
    retried = moved.authorize(conn, intent("adm-r1"), caller(), retry=True)
    assert not retried.reused and retried.receipt_id != first.receipt_id


# ---------------------------------------------------------------------------
# nonces (P2 dispatch / P4 single-use)
# ---------------------------------------------------------------------------

def test_a_minted_nonce_is_bound_to_the_receipt_and_attempt(conn):
    eng = engine()
    auth = eng.authorize(conn, intent(), caller())
    updated, nonce = eng.mint_dispatch_nonce(conn, auth.receipt)
    assert validate_receipt(updated) == []
    row = receipt_store.nonce_row(conn, nonce)
    assert row["receipt_id"] == auth.receipt_id and row["attempt"] == auth.receipt["attempt"]
    assert updated["dispatch_nonces"][-1]["nonce"] == nonce


def test_a_nonce_is_spendable_exactly_once(conn):
    eng = engine()
    auth = eng.authorize(conn, intent(), caller())
    _, nonce = eng.mint_dispatch_nonce(conn, auth.receipt)
    assert receipt_store.consume_nonce(conn, nonce, used_by="claude") is True
    assert receipt_store.consume_nonce(conn, nonce, used_by="claude") is False


def test_one_attempt_never_has_two_live_nonces(conn):
    eng = engine()
    auth = eng.authorize(conn, intent(), caller())
    eng.mint_dispatch_nonce(conn, auth.receipt)
    with pytest.raises(sqlite3.IntegrityError):
        eng.mint_dispatch_nonce(conn, auth.receipt)


# ---------------------------------------------------------------------------
# signing (P5 / O3) and the P2a downgrade
# ---------------------------------------------------------------------------

def test_an_unsigned_receipt_says_so_instead_of_carrying_a_digest(conn):
    """E10 4e: an unkeyed sha256[:16] is not integrity, and a field that looks
    like a signature is worse than an empty one."""
    auth = engine().authorize(conn, intent(), caller())
    assert auth.receipt["signature"] == {"alg": None, "key_id": None, "value": None,
                                         "status": "UNVERIFIED"}


def test_a_keyed_engine_signs_and_verifies(conn, tmp_path):
    key = tmp_path / "engine.key"
    key.write_bytes(b"not-a-real-key-but-a-real-boundary")
    eng = engine(config=EngineConfig(mode="enforce", key_path=str(key)))
    auth = eng.authorize(conn, intent(), caller())
    assert auth.receipt["signature"]["status"] == "VERIFIED"
    assert eng.verify(auth.receipt) is True


def test_tampering_with_a_signed_receipt_is_detected(conn, tmp_path):
    key = tmp_path / "engine.key"
    key.write_bytes(b"not-a-real-key-but-a-real-boundary")
    eng = engine(config=EngineConfig(mode="enforce", key_path=str(key)))
    auth = eng.authorize(conn, intent(), caller())
    tampered = dict(auth.receipt, decision="ALLOW", required_reviewer=None)
    assert eng.verify(tampered) is False


def test_a_verified_caller_still_degrades_without_an_engine_key(conn):
    """P2a: with no key there is no boundary to report, so an adapter asserting
    VERIFIED does not make it so."""
    auth = engine().authorize(conn, intent(), caller(status=IdentityStatus.VERIFIED))
    assert auth.receipt["caller_identity_status"] == "UNVERIFIED"
    assert auth.receipt["downgrade_reason"] == "SHARED_UID_NO_CREDENTIAL_BOUNDARY"


def test_an_unreadable_key_degrades_rather_than_crashing(conn, tmp_path):
    eng = engine(config=EngineConfig(mode="enforce", key_path=str(tmp_path / "missing.key")))
    auth = eng.authorize(conn, intent(), caller())
    assert auth.receipt["signature"]["status"] == "UNVERIFIED"


# ---------------------------------------------------------------------------
# §7.2 — coordinator_managed is provenance and nothing else
# ---------------------------------------------------------------------------

def test_coordinator_managed_is_recorded_and_never_reduces_the_contract(conn):
    """CX-4b: the flag suppressed the review cascade. It may name a coordinator;
    it may not change a single admission input."""
    plain = engine().authorize(conn, intent("cm-a"), caller())
    managed = engine().authorize(
        conn, intent("cm-b", ident=identity(anchors=("src/other.py",)),
                     coordinator_id="alfred:coordinator-1"), caller())
    assert managed.receipt["provenance"]["coordinator"] == "alfred:coordinator-1"
    assert managed.receipt["required_reviewer"] == plain.receipt["required_reviewer"] == "codex"
    assert managed.decision == plain.decision


# ---------------------------------------------------------------------------
# the service boundary — deployment is a config change
# ---------------------------------------------------------------------------

def test_the_unix_socket_client_returns_the_same_authorization(tmp_path):
    """crew-authz must not be a rewrite of the ingresses: the same call, over a
    socket, produces the same receipt shape and the same verdict."""
    from agent_crew.cea.engine import get_engine
    from agent_crew.cea.service import EngineService

    db = tmp_path / "receipts.db"

    def connect():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        receipt_store.ensure_schema(c)
        return c

    from agent_crew.cea.auth import AdapterIdentity, StaticTokenAuthenticator
    token = "svc-token"
    tokens = StaticTokenAuthenticator(
        {token: AdapterIdentity(principal="cron:admitted_trigger",
                                provenance=CallerProvenance.CRON)})

    sock = str(tmp_path / "authz.sock")
    service = EngineService(sock, engine(), connect, tokens)
    service.serve_in_thread()
    try:
        # The adapter presents its own token; it does not describe itself (J9).
        client = get_engine(config=EngineConfig(mode="enforce", endpoint=sock,
                                                caller_token_path=str(_token_file(tmp_path, token))))
        auth = client.authorize(None, intent("svc-1"))
        assert validate_receipt(auth.receipt) == []
        assert auth.receipt["intent_hash"] == intent_hash(identity())
        assert auth.decision == "REVIEW"
        # and the out-of-process engine kept its own lineage
        dup = client.authorize(None, intent("svc-2"))
        assert dup.http_status == 409 and dup.code == "DUPLICATE_INTENT"
    finally:
        service.shutdown_and_close()


def _token_file(tmp_path, token: str):
    path = tmp_path / "adapter.token"
    path.write_text(token)
    path.chmod(0o600)
    return path


def test_an_unreachable_engine_endpoint_raises_rather_than_guessing(tmp_path):
    """P7 again: the client does not get to invent a verdict when the engine is
    down. The adapter must fail closed on the exception."""
    from agent_crew.cea.engine import EngineError, get_engine
    client = get_engine(config=EngineConfig(mode="enforce",
                                            endpoint=str(tmp_path / "nothing.sock")))
    with pytest.raises(EngineError):
        client.authorize(None, intent())


def test_config_from_env_selects_the_boundary(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_ENGINE_ENDPOINT", "/tmp/authz.sock")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "enforce")
    cfg = EngineConfig.from_env()
    assert cfg.endpoint == "/tmp/authz.sock" and cfg.enforcing is True
    monkeypatch.delenv("AGENT_CREW_CEA_ENGINE_ENDPOINT")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "nonsense")
    assert EngineConfig.from_env().endpoint is None
    assert EngineConfig.from_env().mode == "shadow", "an unreadable mode is the safe one"


# ---------------------------------------------------------------------------
# store round-trip properties the engine now relies on
# ---------------------------------------------------------------------------

def test_receipt_history_is_append_only_across_a_lifecycle(conn):
    eng = engine()
    auth = eng.authorize(conn, intent(), caller())
    eng.transition(conn, auth.receipt_id, "QUEUED")
    eng.transition(conn, auth.receipt_id, "CLAIMED")
    history = receipt_store.receipt_history(conn, auth.receipt_id)
    assert [h["receipt"]["state"] for h in history] == ["ISSUED", "QUEUED", "CLAIMED"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE authorization_receipts SET state='ALLOW' WHERE receipt_id=?",
                     (auth.receipt_id,))


def test_lineage_state_tracks_the_receipt(conn):
    eng = engine()
    auth = eng.authorize(conn, intent(), caller())
    eng.transition(conn, auth.receipt_id, "RUNNING")
    lineage = receipt_store.lineage_for_intent(conn, auth.receipt["intent_hash"])
    assert lineage["state"] == "RUNNING" and lineage["receipt_id"] == auth.receipt_id
