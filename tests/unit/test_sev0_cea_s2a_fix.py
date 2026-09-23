"""SEV-0 CEA step 2a-fix — the five P1s from codex's adversarial review of 5831d39.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce565`` (Π P1–P7, §3, §5, §6, §7, §11). Receipt schema: the byte-identical
copy at ``tests/cea_contract/receipt.schema.json``.

Each section below is one finding, and each test is the *exact* reproduction the
review reported — not a paraphrase of it. A regression test that tests the fix
rather than the attack is how a fix silently stops covering the attack.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea.auth import (
    AdapterIdentity, AuthenticationError, DenyAllAuthenticator, StaticTokenAuthenticator,
    TokenFileAuthenticator)
from agent_crew.cea.engine import (
    AuthorizationEngine, EngineConfig, EngineError, UnauthenticatedCaller, intent_hash)
from agent_crew.cea.intent import (
    Caller, CallerProvenance, IdentityStatus, Intent, IntentIdentity, Target, WorkClass)
from agent_crew.cea.providers import SignatureStatus
from agent_crew.cea.receipt import DecisionRev
from agent_crew.cea.schema import validate_receipt
from agent_crew.cea.service import EngineService, UnixSocketEngineClient, encode_intent

from tests.unit.test_sev0_cea_engine import (  # the step-2a fixture writers, unchanged
    DECISION, FakeBudget, FakeGate, FakeRegistry, FakeRuntime, FakeSnapshot, caller,
    engine, identity, intent)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    receipt_store.ensure_schema(c)
    yield c
    c.close()


TOKEN = "s3cret-adapter-token"
TOKENS = {TOKEN: AdapterIdentity(principal="cron:admitted_trigger",
                                 provenance=CallerProvenance.CRON)}


def socket_engine(tmp_path, eng=None, *, authenticator=None):
    """A live unix-socket engine, the way ``crew-authz`` deploys one."""
    db = tmp_path / "receipts.db"

    def connect():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        receipt_store.ensure_schema(c)
        return c

    sock = str(tmp_path / "authz.sock")
    service = EngineService(sock, eng or engine(), connect,
                            authenticator or StaticTokenAuthenticator(TOKENS))
    service.serve_in_thread()
    return service, sock


# ═══════════════════════════════════════════════════════════════════════════
# P1 #1 — J9 is not authentication (engine.py:287-323, service.py:71-92,145-162)
# ═══════════════════════════════════════════════════════════════════════════

def test_the_wire_no_longer_carries_a_principal_at_all():
    """The forgery was structural: the request described its own caller.

    Nothing downstream can be trusted to disbelieve a field that is there, so
    the field is gone."""
    payload = encode_intent(intent("w-1"), TOKEN)
    assert "caller" not in payload
    body = json.dumps(payload)
    for forgeable in ("principal", "provenance", "credential_kind", "identity_status"):
        assert forgeable not in body, f"{forgeable} is still peer-controlled"


def test_the_exact_review_forgery_is_refused_over_the_socket(tmp_path):
    """codex P1 #1, verbatim: ``Caller(principal='attacker', provenance='direct',
    credential_kind=None)`` received ALLOW for an OPS intent. Now: 401, no receipt."""
    service, sock = socket_engine(tmp_path)
    try:
        client = UnixSocketEngineClient(EngineConfig(mode="enforce", endpoint=sock),
                                        credential="attacker-made-this-up")
        ops = intent("forge-1", ident=identity(work_class=WorkClass.OPS,
                                               anchors=("ops/rotate.py",)))
        with pytest.raises(EngineError, match="401 UNAUTHENTICATED"):
            client.authorize(None, ops)
    finally:
        service.shutdown_and_close()
    # and no audit row was written in the attacker's name
    c = sqlite3.connect(tmp_path / "receipts.db")
    c.row_factory = sqlite3.Row
    receipt_store.ensure_schema(c)
    rows = c.execute("SELECT caller_identity FROM authorization_receipts").fetchall()
    c.close()
    assert rows == [], "an unauthenticated caller must not appear in the receipt store"


def test_no_credential_at_all_is_also_401(tmp_path):
    service, sock = socket_engine(tmp_path)
    try:
        client = UnixSocketEngineClient(EngineConfig(mode="enforce", endpoint=sock))
        with pytest.raises(EngineError, match="401 UNAUTHENTICATED"):
            client.authorize(None, intent("nocred-1"))
    finally:
        service.shutdown_and_close()


def test_a_registered_token_authenticates_and_the_engine_names_the_file_identity(tmp_path):
    """The principal on the receipt comes from the token table, not the request."""
    service, sock = socket_engine(tmp_path)
    try:
        client = UnixSocketEngineClient(EngineConfig(mode="enforce", endpoint=sock),
                                        credential=TOKEN)
        auth = client.authorize(None, intent("ok-1"))
        assert validate_receipt(auth.receipt) == []
        assert auth.receipt["caller_identity"] == "cron:admitted_trigger"
        assert auth.receipt["caller_provenance"] == "cron"
    finally:
        service.shutdown_and_close()


def test_an_engine_with_no_token_table_authenticates_nobody(tmp_path):
    """A credential boundary that was never configured refuses; it does not open."""
    service, sock = socket_engine(tmp_path, authenticator=DenyAllAuthenticator())
    try:
        client = UnixSocketEngineClient(EngineConfig(mode="enforce", endpoint=sock),
                                        credential=TOKEN)
        with pytest.raises(EngineError, match="401 UNAUTHENTICATED"):
            client.authorize(None, intent("deny-1"))
    finally:
        service.shutdown_and_close()


def test_in_process_authorize_refuses_a_caller_nobody_authenticated(conn):
    """Defence in depth for the embedded deployment: an object with no
    credential_kind was never produced by an authenticator."""
    forged = Caller(principal="attacker", provenance=CallerProvenance.DIRECT,
                    identity_status=IdentityStatus.VERIFIED, credential_kind=None)
    with pytest.raises(UnauthenticatedCaller):
        engine().authorize(conn, intent("inproc-forge"), forged)
    assert conn.execute("SELECT COUNT(*) FROM authorization_receipts").fetchone()[0] == 0


def test_a_group_readable_token_file_is_refused(tmp_path):
    """0640 is not a credential store under a shared uid (P2a)."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"adapters": {TOKEN: {"principal": "cron:admitted_trigger",
                                                     "provenance": "cron"}}}))
    os.chmod(path, 0o640)
    with pytest.raises(AuthenticationError, match="group/world-readable"):
        TokenFileAuthenticator(str(path)).authenticate(TOKEN)
    os.chmod(path, 0o600)
    assert TokenFileAuthenticator(str(path)).authenticate(TOKEN).principal == "cron:admitted_trigger"


def test_the_token_file_never_mints_a_verified_identity(tmp_path):
    """P2a: VERIFIED belongs to the O21b broker. A file cannot hand it out —
    not even if the file says so."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"adapters": {TOKEN: {
        "principal": "cron:admitted_trigger", "provenance": "cron",
        "identity_status": "VERIFIED", "credential_kind": "broker_registered"}}}))
    os.chmod(path, 0o600)
    who = TokenFileAuthenticator(str(path)).authenticate(TOKEN)
    assert who.identity_status is IdentityStatus.UNVERIFIED
    assert who.credential_kind == "adapter_token"


# ═══════════════════════════════════════════════════════════════════════════
# P1 #3 — only a VALID snapshot signature is usable (engine.py:314-321)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status", [SignatureStatus.UNSIGNED, SignatureStatus.UNKEYED,
                                    SignatureStatus.INVALID])
def test_an_unverified_snapshot_signature_is_an_unavailable_input(conn, status):
    """codex P1 #3: only INVALID was rejected, so an available, otherwise-valid
    UNSIGNED snapshot produced ALLOW for OPS. §3/P5 require verification."""
    ops = intent(f"sig-{status.value}",
                 ident=identity(work_class=WorkClass.OPS, anchors=(f"ops/{status.value}.py",)))
    auth = engine(snapshots=FakeSnapshot(signature=status)).authorize(conn, ops, caller())
    assert auth.decision == "BLOCK"
    assert auth.receipt["reason"]["code"] == "INPUTS_UNAVAILABLE"
    assert any(u.startswith("policy_snapshot_signature")
               for u in auth.receipt["provenance"]["unavailable_inputs"])
    assert status.value in auth.receipt["reason"]["text"]


def test_a_valid_signature_is_the_only_one_that_admits(conn):
    ops = intent("sig-valid", ident=identity(work_class=WorkClass.OPS,
                                             anchors=("ops/valid.py",)))
    auth = engine(snapshots=FakeSnapshot(signature=SignatureStatus.VALID)).authorize(
        conn, ops, caller())
    assert auth.decision == "ALLOW"


class ExplodingSnapshot:
    def current(self, intent=None):
        raise RuntimeError("snapshot producer is down")


class ExplodingRegistry:
    def lookup(self, intent):
        raise RuntimeError("E4 registry is down")


class ExplodingGate:
    def state(self, intent, snapshot):
        raise RuntimeError("gate predicates are unreadable")


@pytest.mark.parametrize("kw,named", [
    ({"snapshots": ExplodingSnapshot()}, "policy_snapshot"),
    ({"capabilities": ExplodingRegistry()}, "capability_registry"),
    ({"gates": ExplodingGate()}, "human_gate"),
])
def test_a_provider_that_raises_becomes_a_named_unavailable_input(conn, kw, named):
    """Not an escaping error: an exception leaves no receipt, no audit row and a
    500 that twelve ingresses each get to interpret. P7 decides instead."""
    auth = engine(**kw).authorize(conn, intent(f"boom-{named}"), caller())
    assert auth.decision == "BLOCK"
    assert auth.receipt["reason"]["code"] == "INPUTS_UNAVAILABLE"
    assert named in " ".join(auth.receipt["provenance"]["unavailable_inputs"])
    assert validate_receipt(auth.receipt) == []


# ═══════════════════════════════════════════════════════════════════════════
# P1 #4 — a signing key is not an authentication key (engine.py:526-534,572-577)
# ═══════════════════════════════════════════════════════════════════════════

def _keyed_config(tmp_path) -> EngineConfig:
    key = tmp_path / "engine.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    return EngineConfig(mode="enforce", key_path=str(key))


def test_a_keyed_engine_does_not_promote_a_caller_supplied_verified_status(conn, tmp_path):
    """codex P1 #4, verbatim: supplying any engine signing key promoted
    `caller.identity_status` straight from the request, so a keyed engine with a
    caller-created VERIFIED Caller emitted caller_identity_status=VERIFIED."""
    forged = Caller(principal="cron:admitted_trigger", provenance=CallerProvenance.CRON,
                    identity_status=IdentityStatus.VERIFIED, credential_kind="adapter_token")
    eng = engine(config=_keyed_config(tmp_path))
    auth = eng.authorize(conn, intent("keyed-1"), forged)
    assert auth.receipt["caller_identity_status"] == "UNVERIFIED"
    assert auth.receipt["executor_binding_status"] == "UNVERIFIED"
    assert auth.receipt["downgrade_reason"] == "SHARED_UID_NO_CREDENTIAL_BOUNDARY"
    # the key still does the one job it has: the receipt is signed and verifies
    assert auth.receipt["signature"]["status"] == "VERIFIED" and eng.verify(auth.receipt)


def test_the_key_does_not_change_the_verdict_either(conn, tmp_path):
    """An implement intent is REVIEW under an UNVERIFIED binding whether or not a
    signing key exists — otherwise 'configure a key' would be a way to buy ALLOW."""
    unkeyed = engine().authorize(conn, intent("keyed-2a"), caller())
    keyed = engine(config=_keyed_config(tmp_path)).authorize(
        conn, intent("keyed-2b", ident=identity(anchors=("src/keyed.py",))), caller())
    assert unkeyed.decision == keyed.decision == "REVIEW"
    assert keyed.receipt["reason"]["code"] == "IDENTITY_UNVERIFIED_REVIEW_REQUIRED"


# ═══════════════════════════════════════════════════════════════════════════
# P1 #5 — canonical scope anchors before hashing and matching (engine.py:81-91)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spelling", [
    "src/./x.py", "src//x.py", "./src/x.py", "src/a/../x.py", "src/x.py/",
    "src///./x.py", "  src/x.py  ",
])
def test_equivalent_spellings_are_one_intent(spelling):
    """codex P1 #5, verbatim: `src/x.py` and `src/./x.py` hashed differently and
    created two live lineages for the same path. Sorting a set of strings the
    caller chose is not identity."""
    assert intent_hash(identity(anchors=(spelling,))) == \
        intent_hash(identity(anchors=("src/x.py",)))


def test_two_spellings_in_one_declaration_collapse_to_one_anchor():
    """De-duplication has to happen *after* canonicalisation, or both survive."""
    from agent_crew.cea.intent import canonical_anchors
    assert canonical_anchors(("src/x.py", "src/./x.py", "./src//x.py")) == ("src/x.py",)


def test_the_second_spelling_is_refused_as_a_duplicate_intent(conn):
    """The end-to-end consequence: one target, one live lineage."""
    eng = engine()
    first = eng.authorize(conn, intent("anchor-1", ident=identity(anchors=("src/x.py",))),
                          caller())
    assert first.http_status in (201, 403)
    second = eng.authorize(conn, intent("anchor-2", ident=identity(anchors=("src/./x.py",))),
                           caller())
    assert second.http_status == 409 and second.code == "DUPLICATE_INTENT"
    assert second.existing_receipt_id == first.receipt_id


def test_distinct_paths_stay_distinct():
    """Canonicalisation must not merge anchors that are genuinely different."""
    assert intent_hash(identity(anchors=("src/x.py",))) != \
        intent_hash(identity(anchors=("/src/x.py",))), "absolute is not relative"
    assert intent_hash(identity(anchors=("src/X.py",))) != \
        intent_hash(identity(anchors=("src/x.py",))), "POSIX paths are case-sensitive"


@pytest.mark.parametrize("anchor,expected", [
    ("HTTPS://Example.COM/A/./b", "https://example.com/A/b"),
    ("Config:Server.Port", "config:Server.Port"),
    ("URN:acme:Thing", "urn:acme:Thing"),
])
def test_non_path_anchors_lowercase_only_what_is_case_insensitive(anchor, expected):
    """The documented rule: scheme (and authority, when there is one) are
    case-insensitive per RFC 3986; the opaque remainder is the namespace's
    business and guessing would silently merge two distinct config keys."""
    from agent_crew.cea.intent import canonical_scope_anchor
    assert canonical_scope_anchor(anchor) == expected


@pytest.mark.parametrize("anchor", ["../x.py", "a/../../x.py", "..", ".", "", "config:"])
def test_parent_traversal_and_empty_anchors_are_rejected(anchor):
    from agent_crew.cea.intent import InvalidScopeAnchor, canonical_scope_anchor
    with pytest.raises(InvalidScopeAnchor):
        canonical_scope_anchor(anchor)


def test_an_untraversable_anchor_blocks_with_a_receipt_not_an_exception(conn):
    """P2 wants the audit row even for "this intent is malformed"; P7 wants the
    refusal. An escaping ValueError would give neither."""
    auth = engine().authorize(
        conn, intent("trav-1", ident=identity(anchors=("../../etc/passwd",))), caller())
    assert auth.http_status == 400 and auth.code == "INVALID_SCOPE_ANCHOR"
    assert auth.decision == "BLOCK"
    assert validate_receipt(auth.receipt) == []
    assert conn.execute("SELECT COUNT(*) FROM authorization_receipts").fetchone()[0] == 1


def test_the_registry_is_asked_about_the_canonical_anchor(conn):
    """§5.2 matching must see the same spelling the hash did, or the two disagree."""
    seen = []

    class RecordingRegistry(FakeRegistry):
        def lookup(self, intent):
            seen.append(intent.identity.target.scope_anchors)
            return super().lookup(intent)

    engine(capabilities=RecordingRegistry()).authorize(
        conn, intent("reg-1", ident=identity(anchors=("./src//y.py", "src/y.py"))), caller())
    assert seen == [("src/y.py",)]


# ═══════════════════════════════════════════════════════════════════════════
# P1 #2 — authority ids are bound to the signed snapshot (engine.py:617-620,549-551)
# ═══════════════════════════════════════════════════════════════════════════

SUPERSEDING = DecisionRev(decision_id="T0-9999", body_hash="c" * 32, supersedes=("T0-1234",))


def _ops(task_id, *, authority=("T0-1234",), anchors=("ops/rotate.py",)):
    return intent(task_id, ident=identity(work_class=WorkClass.OPS, anchors=anchors,
                                          authority=authority))


def test_the_exact_review_bypass_of_already_completed(conn):
    """codex P1 #2, verbatim: after consuming an OPS lineage authorised by
    T0-1234, the caller-supplied tuple (T0-1234, ATTACKER-ID) produced ALLOW —
    the changed hash bypassed ALREADY_COMPLETED although the snapshot still
    contained only T0-1234. A hash input the caller controls is a nonce, not an
    authorisation."""
    eng = engine()
    first = eng.authorize(conn, _ops("j2-1"), caller())
    assert first.decision == "ALLOW"
    eng.transition(conn, first.receipt_id, "CONSUMED")

    attack = eng.authorize(conn, _ops("j2-2", authority=("T0-1234", "ATTACKER-ID")), caller())
    assert attack.http_status == 409, "the completed work was re-admitted"
    assert attack.code == "ALREADY_COMPLETED"
    assert attack.decision == "BLOCK"
    assert attack.existing_receipt_id == first.receipt_id


def test_a_real_id_that_does_not_supersede_is_also_refused(conn):
    """Not just made-up ids: an id the snapshot *does* carry still does not
    re-admit completed work unless it explicitly supersedes the run's authority."""
    snapshot = FakeSnapshot(decisions=(DECISION, DecisionRev("T0-5555", "d" * 32)),
                            in_scope=(DECISION, DecisionRev("T0-5555", "d" * 32)))
    eng = engine(snapshots=snapshot)
    first = eng.authorize(conn, _ops("j2-3"), caller())
    eng.transition(conn, first.receipt_id, "CONSUMED")
    again = eng.authorize(conn, _ops("j2-4", authority=("T0-1234", "T0-5555")), caller())
    assert again.code == "ALREADY_COMPLETED"


def test_an_explicit_superseding_record_in_the_snapshot_does_re_admit(conn):
    """The exception is real — it just has to be a record, not a claim."""
    plain = engine()
    first = plain.authorize(conn, _ops("j2-5"), caller())
    plain.transition(conn, first.receipt_id, "CONSUMED")
    snapshot = FakeSnapshot(decisions=(DECISION, SUPERSEDING),
                            in_scope=(DECISION, SUPERSEDING))
    again = engine(snapshots=snapshot).authorize(
        conn, _ops("j2-6", authority=("T0-1234", "T0-9999")), caller())
    assert again.code != "ALREADY_COMPLETED"
    assert again.decision == "ALLOW"


def test_a_superseding_record_the_caller_did_not_ask_under_is_not_enough(conn):
    """(a) the record must be in the snapshot AND (b) among the ids this request
    acts under. Otherwise any unrelated supersession in the snapshot reopens
    every completed lineage it happens to name."""
    plain = engine()
    first = plain.authorize(conn, _ops("j2-7"), caller())
    plain.transition(conn, first.receipt_id, "CONSUMED")
    snapshot = FakeSnapshot(decisions=(DECISION, SUPERSEDING),
                            in_scope=(DECISION, SUPERSEDING))
    again = engine(snapshots=snapshot).authorize(conn, _ops("j2-8", authority=("T0-1234",)),
                                                 caller())
    assert again.code == "ALREADY_COMPLETED"


@pytest.mark.parametrize("status", [SignatureStatus.UNSIGNED, SignatureStatus.INVALID])
def test_a_supersession_we_cannot_verify_is_not_one(conn, status):
    """P5 + P7 together: an unsigned snapshot cannot grant the exception either."""
    plain = engine()
    first = plain.authorize(conn, _ops(f"j2-sig-{status.value}"), caller())
    plain.transition(conn, first.receipt_id, "CONSUMED")
    snapshot = FakeSnapshot(decisions=(DECISION, SUPERSEDING),
                            in_scope=(DECISION, SUPERSEDING), signature=status)
    again = engine(snapshots=snapshot).authorize(
        conn, _ops(f"j2-sig2-{status.value}", authority=("T0-1234", "T0-9999")), caller())
    assert again.code == "ALREADY_COMPLETED"


def test_an_authority_id_the_snapshot_does_not_carry_blocks_on_its_own(conn):
    """J2 proper, with no completed lineage in play: an invented decision id is
    free text with a ticket-shaped name (§1.4)."""
    auth = engine().authorize(conn, _ops("j2-9", authority=("T0-1234", "ATTACKER-ID"),
                                         anchors=("ops/fresh.py",)), caller())
    assert auth.decision == "BLOCK"
    assert auth.receipt["reason"]["code"] == "AUTHORITY_NOT_IN_SNAPSHOT"
    assert "ATTACKER-ID" in auth.receipt["reason"]["text"]


def test_the_work_hash_is_the_identity_without_the_authority_ids(conn):
    """The mechanism, stated once: authority ids move intent_hash and must not
    move the question 'did this work already complete?'."""
    from agent_crew.cea.engine import work_hash
    a = identity(work_class=WorkClass.OPS, authority=("T0-1234",))
    b = identity(work_class=WorkClass.OPS, authority=("T0-1234", "T0-9999"))
    assert intent_hash(a) != intent_hash(b)
    assert work_hash(a) == work_hash(b)
    c = identity(work_class=WorkClass.OPS, authority=("T0-1234",), anchors=("ops/other.py",))
    assert work_hash(a) != work_hash(c), "different work is still different work"
