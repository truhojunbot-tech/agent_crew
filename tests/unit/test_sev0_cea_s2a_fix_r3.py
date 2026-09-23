"""SEV-0 CEA step 2a-fix r3 — codex's re-review of ``f1aee1d``, P1.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce565`` (Π P1–P7, §3, §5, §6, §7, §11). Receipt schema: the byte-identical
copy at ``tests/cea_contract/receipt.schema.json``.

The finding, verbatim: *"The sealed Caller is not an authentication boundary.
``_mint_caller`` is a module-global importable function... The weak registry can
also be poisoned: create a Caller with ``object.__new__``, set fields using
``object.__setattr__``, retrieve the WeakSet from
``is_authenticated_caller.__closure__``, add the object, and ``authorize()``
returns ALLOW."*

Both reproductions are below and **both still succeed** — they produce a Caller
the engine accepts. That is not the bug being fixed; in-process Python cannot be
made to keep a secret from in-process Python, and a test asserting it could
would be testing a claim rather than the attack. What each test asserts is that
the forgery **buys nothing**: the decision is identical to the honest UNVERIFIED
baseline, it is never ALLOW for OPS, and no receipt records the forged principal
as authenticated.

The other half of the answer is deployment, at the bottom: in ``enforce`` the
engine refuses to decide from inside a caller's own process at all.
"""
from __future__ import annotations

import sqlite3
import weakref

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea._caller_mint import is_authenticated_caller, mint_caller
from agent_crew.cea.engine import (
    EMBEDDED_MODES, ENFORCE, SHADOW, TEST, AuthorizationEngine, EngineConfig)
from agent_crew.cea.intent import (
    IDENTITY_DEPENDENT_WORK_CLASSES, Caller, CallerProvenance, IdentityStatus, WorkClass)

from tests.unit.test_sev0_cea_engine import (  # the step-2a fixture writers, unchanged
    FakeBudget, FakeGate, FakeRegistry, FakeRuntime, FakeSnapshot, caller, engine,
    identity, intent)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    receipt_store.ensure_schema(c)
    yield c
    c.close()


def ops(task_id, **kw):
    """An OPS intent — J9's who-may-act question, and the exact work class the
    review's reproductions returned ALLOW for."""
    return intent(task_id, ident=identity(work_class=WorkClass.OPS, **kw),
                  task_type="discuss")


def decision_of(auth) -> tuple[str, str]:
    return auth.decision, auth.receipt["reason"]["code"]


# ═══════════════════════════════════════════════════════════════════════════
# reproduction 1 — import the mint
# ═══════════════════════════════════════════════════════════════════════════

def test_importing_the_mint_still_produces_an_accepted_caller(conn):
    """The review's first reproduction, run as written. It succeeds, and this
    test says so: relocating the function to ``_caller_mint`` renamed it, it did
    not make Python private. The next test is the one that matters."""
    forged = mint_caller("attacker", CallerProvenance.DIRECT, "adapter_token")
    assert is_authenticated_caller(forged), "an importable mint is importable"
    auth = engine().authorize(conn, ops("mint-1"), forged)
    assert auth.receipt is not None, "the engine accepted it — J9 is hygiene, not a boundary"


def test_an_imported_mint_gets_exactly_what_an_honest_caller_gets(conn):
    """codex P1, reproduction 1: ``_mint_caller('attacker', DIRECT,
    'adapter_token')`` *"creates an engine-accepted caller with no HMAC
    credential and returns ALLOW for OPS"*.

    It is still engine-accepted. It no longer returns ALLOW, and it no longer
    differs in any way from the baseline — which is the property that makes
    minting one pointless."""
    eng = engine()
    baseline = eng.authorize(conn, ops("mint-base", anchors=("ops/a.py",)), caller())
    forged = mint_caller("attacker", CallerProvenance.DIRECT, "adapter_token")
    attack = eng.authorize(conn, ops("mint-atk", anchors=("ops/b.py",)), forged)

    assert decision_of(attack) == decision_of(baseline)
    assert attack.decision != "ALLOW"
    assert decision_of(attack) == ("HUMAN_GATE", "IDENTITY_UNVERIFIED_WHO_MAY_ACT")
    assert attack.http_status == 403


# ═══════════════════════════════════════════════════════════════════════════
# reproduction 2 — poison the weak registry through the closure
# ═══════════════════════════════════════════════════════════════════════════

def poison_registry() -> Caller:
    """codex P1, reproduction 2, step for step: *"create a Caller with
    ``object.__new__``, set fields using ``object.__setattr__``, retrieve the
    WeakSet from ``is_authenticated_caller.__closure__``, add the object"*."""
    obj = object.__new__(Caller)
    object.__setattr__(obj, "principal", "attacker")
    object.__setattr__(obj, "provenance", CallerProvenance.DIRECT)
    object.__setattr__(obj, "identity_status", IdentityStatus.VERIFIED)
    object.__setattr__(obj, "credential_kind", "broker_registered")
    registry = next(cell.cell_contents for cell in is_authenticated_caller.__closure__
                    if isinstance(cell.cell_contents, weakref.WeakSet))
    registry.add(obj)
    return obj


def test_poisoning_the_registry_through_the_closure_still_works(conn):
    """*"and ``authorize()`` returns ALLOW"* — the first half is still true: the
    object passes J9. Stated plainly rather than asserted away."""
    obj = poison_registry()
    assert is_authenticated_caller(obj), "closure introspection reaches the registry"
    assert engine().authorize(conn, ops("poison-1"), obj).receipt is not None


def test_a_poisoned_registry_entry_gets_exactly_what_an_honest_caller_gets(conn):
    """The second half is not: a VERIFIED-claiming, broker-claiming forgery in
    the registry gets the same HUMAN_GATE as ``cron:admitted_trigger``."""
    eng = engine()
    baseline = eng.authorize(conn, ops("poison-base", anchors=("ops/c.py",)), caller())
    attack = eng.authorize(conn, ops("poison-atk", anchors=("ops/d.py",)), poison_registry())

    assert decision_of(attack) == decision_of(baseline)
    assert attack.decision != "ALLOW"
    assert decision_of(attack) == ("HUMAN_GATE", "IDENTITY_UNVERIFIED_WHO_MAY_ACT")


def test_no_receipt_records_a_forged_principal_as_authenticated(conn):
    """The forged caller claims ``identity_status=VERIFIED`` and
    ``credential_kind=broker_registered``. The receipt is an audit record and
    still names the principal that asked — it must not *endorse* it."""
    eng = engine()
    for task_id, forged in (("rec-1", mint_caller("attacker", CallerProvenance.DIRECT, "x")),
                            ("rec-2", poison_registry())):
        r = eng.authorize(conn, ops(task_id, anchors=(f"ops/{task_id}.py",)), forged).receipt
        assert r["caller_identity"] == "attacker", "provenance is recorded, not laundered"
        assert r["caller_identity_status"] == "UNVERIFIED"
        assert r["executor_binding_status"] == "UNVERIFIED"
        assert r["downgrade_reason"] == "SHARED_UID_NO_CREDENTIAL_BOUNDARY"
        assert r["decision"] != "ALLOW"

    rows = [r[0] for r in conn.execute(
        "SELECT receipt_json FROM authorization_receipts").fetchall()]
    assert rows, "the audit rows exist"
    assert not any('"caller_identity_status": "VERIFIED"' in body for body in rows)
    assert not any('"credential_kind"' in body for body in rows), \
        "a caller-chosen credential kind is not a receipt field at all"


# ═══════════════════════════════════════════════════════════════════════════
# the general property: the decision does not depend on the principal
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("work_class", list(WorkClass))
def test_the_decision_is_invariant_across_principals(conn, work_class):
    """Not just OPS, and not just these two forgeries: for every work class the
    verdict is the same for an honest caller, a caller under a different
    principal, an imported mint and a poisoned registry entry."""
    eng = engine()
    callers = [caller(), caller(principal="quota-core", provenance=CallerProvenance.MANUAL),
               mint_caller("attacker", CallerProvenance.DIRECT, "adapter_token"),
               poison_registry()]
    answers = set()
    for n, who in enumerate(callers):
        ident = identity(work_class=work_class, anchors=(f"inv/{work_class.value}/{n}.py",))
        answers.add(decision_of(eng.authorize(conn, intent(f"inv-{work_class.value}-{n}",
                                                           ident=ident), who)))
    assert len(answers) == 1, f"{work_class.value} answered differently by principal: {answers}"


def test_naming_yourself_the_owner_no_longer_downgrades_an_owner_decision(conn):
    """J5 used to read ``owner != caller.principal``, so a caller that arrived
    as ``quota-core`` got EXTEND where everyone else got REUSE/OWNER_CONFLICT —
    a HUMAN_GATE downgraded to a REVIEW by choosing a string. The ownership test
    now reads intent scope only."""
    from agent_crew.cea.receipt import MatchedCapability

    owned = FakeRegistry(matches=(MatchedCapability(id="tokenomics.gate", owner="quota-core",
                                                    repo="example/quota-core"),))
    eng = engine(capabilities=owned)
    honest = eng.authorize(conn, intent("own-1", ident=identity(anchors=("own/a.py",))),
                           caller())
    impostor = eng.authorize(conn, intent("own-2", ident=identity(anchors=("own/b.py",))),
                             mint_caller("quota-core", CallerProvenance.DIRECT, "adapter_token"))

    assert decision_of(honest) == decision_of(impostor) == ("HUMAN_GATE", "OWNER_CONFLICT")
    assert honest.receipt["reuse"]["decision"] == impostor.receipt["reuse"]["decision"] == "REUSE"
    assert impostor.receipt["reuse"]["approver_identity_verified"] is False


def test_ops_is_identity_dependent_on_both_sides_of_the_receipt(conn):
    """What the engine may not issue is exactly what the validator refuses. The
    two share the predicate rather than restating it."""
    from agent_crew.cea.validator import _identity_dependent

    assert IDENTITY_DEPENDENT_WORK_CLASSES == frozenset({"ops"})
    r = engine().authorize(conn, ops("mirror-1"), caller()).receipt
    assert r["required_reviewer"] is None and r["human_gate_state"] == "NOT_REQUIRED"
    assert _identity_dependent(r), "the validator sees the work class, not just the reviewer"


# ═══════════════════════════════════════════════════════════════════════════
# P2 — the boundary is the process plus the credential, not the class
# ═══════════════════════════════════════════════════════════════════════════

def _engine(mode):
    return AuthorizationEngine(
        config=EngineConfig(mode=mode), capabilities=FakeRegistry(), snapshots=FakeSnapshot(),
        runtime=FakeRuntime(), budgets=FakeBudget(), gates=FakeGate())


def test_enforce_refuses_embedded_direct_authorization(conn):
    """The engine will not decide from inside a caller's own process when a
    non-ALLOW verdict would actually withhold work. P7 shape: the credential
    boundary is named as the input that did not answer."""
    auth = _engine(ENFORCE).authorize(conn, intent("emb-1"), caller())
    assert auth.decision == "BLOCK"
    assert auth.code == "CREDENTIAL_BOUNDARY_UNAVAILABLE"
    assert auth.http_status == 403
    assert auth.receipt["provenance"]["unavailable_inputs"] == ["caller_credential_boundary"]
    assert "not this interpreter" in auth.receipt["reason"]["text"]


@pytest.mark.parametrize("mode", list(EMBEDDED_MODES))
def test_shadow_and_test_may_still_run_embedded(conn, mode):
    """`shadow` stops nothing, so embedding it withholds nothing. `test` says
    what it is in its name."""
    auth = _engine(mode).authorize(conn, intent(f"emb-{mode}"), caller())
    assert auth.code != "CREDENTIAL_BOUNDARY_UNAVAILABLE"
    assert (mode in EMBEDDED_MODES) and mode in (TEST, SHADOW)


def test_no_public_declaration_can_turn_enforce_authorization_on(tmp_path, conn):
    """codex review-sev0-cea-lineage-s2a-fix-r3-x P1, the exact reproduction.

    Before: ``eng.attach_credential_boundary('/not-a-socket')`` then a forged
    mint reached **ALLOW / OK, caller_identity=attacker,
    caller_identity_status=UNVERIFIED** — no socket created, no credential
    checked. The declaration was a public mutable string, so the attacker set
    it. Now there is no such attribute at all, and the public entry point in
    ``enforce`` decides nothing regardless of what anybody declares.
    """
    eng = _engine(ENFORCE)

    # 1. the attribute the attack used is gone, and assigning one changes nothing.
    assert not hasattr(eng, "attach_credential_boundary")
    assert not hasattr(eng, "_credential_boundary")
    eng._credential_boundary = str(tmp_path / "not-a-socket")   # the declaration, verbatim
    eng.attach_credential_boundary = lambda name: None          # and the method, re-added
    eng.attach_credential_boundary(str(tmp_path / "not-a-socket"))
    assert not (tmp_path / "not-a-socket").exists()

    # 2. the forged caller of the finding, minted the way the finding mints it.
    forged = mint_caller("attacker", CallerProvenance.DIRECT, "x")
    assert is_authenticated_caller(forged) and forged.principal == "attacker"

    # 3. a REVIEW intent — the finding's work class — reaches no ALLOW path.
    auth = eng.authorize(conn, intent("fold2-p1", ident=identity(work_class=WorkClass.REVIEW)),
                         forged)
    assert (auth.decision, auth.code) == ("BLOCK", "CREDENTIAL_BOUNDARY_UNAVAILABLE")
    assert auth.http_status == 403
    assert auth.receipt["provenance"]["unavailable_inputs"] == ["caller_credential_boundary"]
    # the audit row still exists, and still does not call the forgery verified.
    assert auth.receipt["caller_identity"] == "attacker"
    assert auth.receipt["caller_identity_status"] == "UNVERIFIED"


def test_enforce_has_no_public_path_that_is_not_fail_closed(conn):
    """Every public spelling of "authorize this" is the fail-closed one. The
    enforcing path is a different, non-public method the socket handler calls."""
    eng = _engine(ENFORCE)
    public = [n for n in dir(eng) if not n.startswith("_") and "author" in n.lower()]
    assert public == ["authorize"], public
    assert eng.authorize(conn, intent("pub-1"), caller()).code == "CREDENTIAL_BOUNDARY_UNAVAILABLE"
    assert hasattr(eng, "_authorize_authenticated")


def test_the_service_is_the_credential_boundary_enforce_requires(tmp_path):
    """An engine served by :class:`EngineService` authorizes under enforce — but
    only for a caller that presented a credential *this* service matched, over
    the socket, in a process the caller does not run in.

    Asserted end to end through :class:`UnixSocketEngineClient` rather than by
    calling ``eng.authorize()`` after construction. The old version of this test
    did exactly that and so encoded the bypass: it asserted only that
    ``CREDENTIAL_BOUNDARY_UNAVAILABLE`` disappeared from an in-process call once
    a service object existed somewhere (codex P1).
    """
    import sqlite3 as _sqlite3

    from agent_crew.cea.auth import AdapterIdentity, StaticTokenAuthenticator
    from agent_crew.cea.service import EngineError, EngineService, UnixSocketEngineClient

    db = tmp_path / "receipts.db"

    def connect():
        c = _sqlite3.connect(db)
        c.row_factory = _sqlite3.Row
        receipt_store.ensure_schema(c)
        return c

    eng = _engine(ENFORCE)
    sock = str(tmp_path / "authz.sock")
    auth_table = StaticTokenAuthenticator(
        {"tok-good": AdapterIdentity("adapter:server", CallerProvenance.DIRECT)})
    service = EngineService(sock, eng, connect, authenticator=auth_table)
    thread = service.serve_in_thread()
    try:
        config = EngineConfig(mode=ENFORCE, endpoint=sock)

        # in-process, same engine object, same moment: still refused.
        local = connect()
        try:
            refused = eng.authorize(local, intent("svc-0"), caller())
            assert refused.code == "CREDENTIAL_BOUNDARY_UNAVAILABLE"
        finally:
            local.close()

        # across the socket with a credential the service matched: decided.
        admitted = UnixSocketEngineClient(config, credential="tok-good").authorize(
            None, intent("svc-1", ident=identity(anchors=("svc/a.py",))))
        assert admitted.code != "CREDENTIAL_BOUNDARY_UNAVAILABLE"
        assert admitted.receipt["caller_identity"] == "adapter:server"

        # across the socket with a credential it did not: 401, and no receipt.
        with pytest.raises(EngineError) as exc:
            UnixSocketEngineClient(config, credential="tok-forged").authorize(
                None, intent("svc-2", ident=identity(anchors=("svc/b.py",))))
        assert "401 UNAUTHENTICATED" in str(exc.value)
        seen = connect()
        try:
            assert seen.execute("SELECT COUNT(*) FROM authorization_receipts "
                                "WHERE task_id = 'svc-2'").fetchone()[0] == 0
        finally:
            seen.close()
    finally:
        service.shutdown_and_close()
        thread.join(timeout=5)


def test_enforce_still_records_the_audit_row_for_the_refusal(conn):
    """P2: the receipt exists even when no task row does. A refusal nobody can
    see is indistinguishable from a request nobody made."""
    _engine(ENFORCE).authorize(conn, intent("emb-audit"), caller())
    n = conn.execute("SELECT COUNT(*) FROM authorization_receipts "
                     "WHERE task_id = 'emb-audit'").fetchone()[0]
    assert n == 1


def test_enforce_mode_still_enforces_at_the_call_sites(conn):
    """`test` and `enforce` are the same gate; they differ only in where the
    engine may run."""
    from agent_crew.cea.callsites import enforcing

    assert enforcing(EngineConfig(mode=ENFORCE)) is True
    assert enforcing(EngineConfig(mode=TEST)) is True
    assert enforcing(EngineConfig(mode=SHADOW)) is False


def test_an_unknown_mode_falls_back_to_shadow_not_to_enforcement():
    """A typo must not silently turn enforcement on, and must not silently turn
    a credential boundary off either."""
    assert EngineConfig.from_env({"AGENT_CREW_CEA_MODE": "enfroce"}).mode == SHADOW
    assert EngineConfig.from_env({"AGENT_CREW_CEA_MODE": "test"}).mode == TEST
    assert EngineConfig.from_env({"AGENT_CREW_CEA_MODE": "enforce"}).mode == ENFORCE
    assert EngineConfig.from_env({}).embedded_authorization_permitted is True
    assert EngineConfig(mode=ENFORCE).embedded_authorization_permitted is False


# ═══════════════════════════════════════════════════════════════════════════
# receipt signing stays independent of caller authentication
# ═══════════════════════════════════════════════════════════════════════════

def test_receipt_signing_is_still_independent_of_caller_authentication(conn, tmp_path):
    """The key proves the engine wrote the receipt. It says nothing about who
    asked — including when who asked is a forgery."""
    key = tmp_path / "engine.key"
    key.write_bytes(b"k" * 32)
    eng = engine(config=EngineConfig(mode=TEST, key_path=str(key)))
    auth = eng.authorize(conn, ops("sign-r3"), poison_registry())
    assert auth.receipt["signature"]["status"] == "VERIFIED" and eng.verify(auth.receipt)
    assert auth.receipt["caller_identity_status"] == "UNVERIFIED"
    assert auth.decision != "ALLOW"
