"""SEV-0 CEA interface candidates (``agent_crew.cea``) — shape and no-wiring proof.

These are NOT the §12 fixtures (see ``test_sev0_cea_fixtures.py``). They pin
two things about the pre-freeze scaffolding:

1. the types spell the ADR (alfred ``sev0/e11-adr-draft`` @ ``6cbce56``): §3
   receipt field names, Π lifecycle states, P6 runtime states, §2.3 decisions,
   the five P2 validator call points;
2. the package is inert — nothing in the runtime imports it, and it imports no
   DB, HTTP or queue code — so landing it changes no behaviour (freeze rule,
   owner 11354).

Test 2 is expected to be *deleted* by the engine lineage when wiring begins;
it is a guard for this branch's claim, not a permanent invariant.
"""
from __future__ import annotations

import dataclasses
import importlib
import pathlib
import re

import pytest

import agent_crew
from agent_crew import cea
from agent_crew.cea import receipt as receipt_mod

SRC = pathlib.Path(agent_crew.__file__).resolve().parent
CEA_MODULES = ("intent", "runtime_state", "receipt", "providers", "validator")

#: §3 YAML block, verbatim field names (plus the Π lifecycle/binding block).
ADR_S3_FIELDS = {
    "receipt_id", "issued_at", "issuer",
    "task_id", "intent_hash", "parent_receipt_id", "project",
    "authority_source", "policy_generation", "policy_hash", "source_decision_revs",
    "capability_registry", "matched_capability", "reuse",
    "runtime_state", "provider_budget",
    "required_reviewer", "required_tester", "human_gate_state",
    "caller_identity", "caller_provenance", "executor_binding",
    "executor_binding_status", "caller_identity_status", "downgrade_reason",
    "decision", "reason", "signature",
    # Π additions
    "state", "binding", "idempotency_key", "attempt", "max_attempts",
    "dispatch_nonces", "supersedes",
}


def test_contract_commit_is_pinned():
    assert cea.CONTRACT_COMMIT == "6cbce565e6f562c1727fc3fca45f0dac807f0050"


def test_receipt_carries_every_adr_s3_field_and_is_frozen():
    names = {f.name for f in dataclasses.fields(cea.Receipt)}
    assert ADR_S3_FIELDS <= names, sorted(ADR_S3_FIELDS - names)
    r = cea.Receipt(receipt_id="r", issued_at="t", issuer="i", task_id="t1",
                    intent_hash="h", project="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.decision = cea.Decision.ALLOW          # type: ignore[misc]
    # Fail-closed defaults: a receipt nobody filled in is a BLOCK under STOPPED, UNVERIFIED.
    assert r.decision is cea.Decision.BLOCK
    assert r.runtime_state is cea.RuntimeState.STOPPED
    assert r.executor_binding_status is cea.IdentityStatus.UNVERIFIED
    assert r.caller_identity_status is cea.IdentityStatus.UNVERIFIED
    assert r.downgrade_reason is cea.DowngradeReason.SHARED_UID_NO_CREDENTIAL_BOUNDARY


@pytest.mark.parametrize("enum, values", [
    (cea.Decision, {"ALLOW", "BLOCK", "REVIEW", "HUMAN_GATE"}),
    (cea.ReceiptState, {"ISSUED", "QUEUED", "CLAIMED", "RUNNING", "HELD", "CONSUMED", "SUPERSEDED", "REVOKED"}),
    (cea.RuntimeState, {"ACTIVE", "DRAINING", "QUARANTINED", "STOPPED"}),
    (cea.HumanGateState, {"NOT_REQUIRED", "PENDING", "GRANTED", "DENIED"}),
    (cea.BudgetClass, {"OK", "CONSTRAINED", "EXHAUSTED"}),
    (cea.IdentityStatus, {"VERIFIED", "UNVERIFIED"}),
    (cea.WorkClass, {"implement", "fix", "review", "test", "merge", "ops"}),
    (cea.CallerProvenance, {"cron", "direct", "coordinator", "manual", "cascade", "retry", "watchdog"}),
])
def test_enums_spell_the_adr(enum, values):
    assert {m.value for m in enum} == values


def test_live_lineage_states_match_p4_partial_index():
    assert receipt_mod.LIVE_STATES == {
        cea.ReceiptState.ISSUED, cea.ReceiptState.QUEUED, cea.ReceiptState.CLAIMED,
        cea.ReceiptState.RUNNING, cea.ReceiptState.HELD}


def test_intent_identity_excludes_description_task_id_and_opid():
    names = {f.name for f in dataclasses.fields(cea.IntentIdentity)}
    assert names == {"project", "work_class", "target", "capability_id", "authority_decision_ids"}
    assert {f.name for f in dataclasses.fields(cea.Target)} == {"repo", "base_ref", "scope_anchors"}


def test_runtime_state_tightness_is_total_and_stopped_is_tightest():
    order = sorted(cea.RuntimeState, key=lambda s: s.tightness)
    assert order == [cea.RuntimeState.ACTIVE, cea.RuntimeState.DRAINING,
                     cea.RuntimeState.QUARANTINED, cea.RuntimeState.STOPPED]


def test_binding_is_the_p3_tuple():
    names = {f.name for f in dataclasses.fields(cea.Binding)}
    assert names == {"policy_generation", "policy_hash", "source_decision_revs", "capability_registry",
                     "matched_capability", "runtime_state", "runtime_state_epoch", "human_gate",
                     "budget_class"}


def test_validator_protocol_has_exactly_the_five_p2_call_points():
    methods = sorted(m for m in vars(cea.ReceiptValidator) if m.startswith("validate_"))
    assert methods == ["validate_claim", "validate_dispatch", "validate_enqueue",
                       "validate_execute_start", "validate_result"]
    assert {p.value for p in cea.ValidationPoint} == {
        "enqueue", "claim", "dispatch", "execute_start", "result"}


def test_input_providers_return_data_not_decisions():
    """P1: a provider protocol never returns a Decision."""
    import typing
    for proto in (cea.CapabilityOwnershipProvider, cea.PolicySnapshotProvider,
                  cea.RuntimeStateProvider, cea.BudgetProvider, cea.HumanGateProvider,
                  cea.CallerAuthenticator):
        for name, member in vars(proto).items():
            if name.startswith("_") or not callable(member):
                continue
            hints = typing.get_type_hints(member)
            assert hints.get("return") is not cea.Decision, f"{proto.__name__}.{name} returns a verdict"


# ---------------------------------------------------------------------------
# Which runtime modules may reach into `cea` (the freeze rule, after step 1 wired it)
# ---------------------------------------------------------------------------

# Step 1 (`4b62f32`) wired `queue.py → cea.store`, which made the original
# "nothing in the runtime imports cea" guard red the moment it landed — it was
# still failing at `10153bf`. A guard that asserts a state the tree has already
# left is not a freeze rule, it is noise that trains people to ignore the suite.
#
# The invariant that is actually worth holding is the *direction*: the runtime
# reaches into `cea` only at known, reviewed points, and `cea` never reaches back
# (`test_cea_imports_no_db_http_or_queue_code`, below). This list is the review
# gate — adding a call site means editing it, which is the point.
CEA_IMPORTERS = {
    "queue.py",        # step 1: the receipt store rides on the tasks DB connection
    "server.py",       # step 4c: T5 reads the project's rollout mode at /result
    "pipeline.py",     # step 4m: the cascade READS the review/test contract
                       # admission stored (cea.cascade_contract). It is a
                       # consumer — the direction the freeze rule cares about is
                       # preserved, and this replaces pipeline.py's own
                       # risk-tier decision (§11.2 #14).
}


def test_only_the_declared_runtime_modules_import_cea():
    found = set()
    for path in SRC.rglob("*.py"):
        if path.is_relative_to(SRC / "cea"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+agent_crew\.cea\b|^\s*from\s+\.\s*cea\b|from agent_crew import .*\bcea\b",
                     text, re.MULTILINE):
            found.add(path.relative_to(SRC).as_posix())
    assert found == CEA_IMPORTERS, (
        f"undeclared cea call sites: {sorted(found - CEA_IMPORTERS)}; "
        f"declared but gone: {sorted(CEA_IMPORTERS - found)}")


def test_cea_imports_no_db_http_or_queue_code():
    forbidden = ("sqlite3", "fastapi", "starlette", "agent_crew.queue", "agent_crew.server",
                 "agent_crew.pipeline", "agent_crew.mcp_server", "subprocess", "requests", "httpx")
    for name in CEA_MODULES:
        text = (SRC / "cea" / f"{name}.py").read_text(encoding="utf-8")
        for token in forbidden:
            assert not re.search(rf"^\s*(from|import)\s+{re.escape(token)}\b", text, re.MULTILINE), (
                f"cea/{name}.py imports {token}")
        importlib.import_module(f"agent_crew.cea.{name}")
