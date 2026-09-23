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
