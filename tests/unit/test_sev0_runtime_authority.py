"""SEV-0 CEA s1-fix re-review, P1 #1 — a string the caller chose is not authority.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce565`` (Π P6, P7, §5.3).

The s1-fix routed ``set_stop_epoch``/``resume_stop`` through the P6 predicate, but
the predicate itself asked only that ``who`` start with ``owner:`` and that
``decision_id`` be non-empty — both supplied by the requester. This suite is the
verification path: the decision id must name a record in the current **signed**
snapshot, that record must name the principal, and it must name the build the
runtime is running.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent_crew.cea.providers import PolicySnapshotRef, SignatureStatus   # noqa: E402
from agent_crew.cea.receipt import DecisionRev                            # noqa: E402
from agent_crew.protocol import TaskRequest                               # noqa: E402
from agent_crew.queue import (                                            # noqa: E402
    RefuseAllLoosening, RuntimeTransitionRefused, SnapshotLooseningAuthority, TaskQueue)

BUILD = "b" * 40
OTHER_BUILD = "c" * 40


def snapshots(*, records, signature=SignatureStatus.VALID, available=True):
    class _S:
        def current(self, intent=None):
            return PolicySnapshotRef(generation=7, hash="h" * 16, produced_at=None,
                                     decisions=records, in_scope=records,
                                     signature=signature, available=available)
    return _S()


T0 = DecisionRev(decision_id="T0-1234", body_hash="a" * 32,
                 principals=("owner:hojun",), build_commits=(BUILD,))


def authority(**kw):
    return SnapshotLooseningAuthority(
        snapshots(records=kw.pop("records", (T0,)), **{k: v for k, v in kw.items()
                                                       if k in ("signature", "available")}),
        build_commit=kw.get("build_commit", BUILD))


@pytest.fixture()
def q(tmp_path):
    """A queue with the real verifier, pointed at a snapshot that names T0-1234."""
    return TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=authority())


def stop(queue):
    queue.transition_runtime_state("STOPPED", who="fleet_stop", reason="test")
    assert queue.get_runtime_state()["state"] == "STOPPED"


# ── the exact repro ──────────────────────────────────────────────────────────

def test_the_exact_review_repro_is_refused(q):
    """``set_stop_epoch(False, who='owner:attacker', decision_id='not-a-t0-record')``.

    Before the fix this returned ACTIVE: the prefix matched and the id was
    non-empty, and nothing else was asked.
    """
    stop(q)
    with pytest.raises(RuntimeTransitionRefused) as exc:
        q.set_stop_epoch(False, who="owner:attacker", decision_id="not-a-t0-record")
    assert "names no record in the signed snapshot" in str(exc.value)
    assert q.get_runtime_state()["state"] == "STOPPED"


def test_a_real_decision_id_under_the_wrong_principal_is_refused(q):
    """Half the repro: the id is real, the requester is not who it authorises."""
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="does not authorise principal"):
        q.set_stop_epoch(False, who="owner:attacker", decision_id="T0-1234")
    assert q.get_runtime_state()["state"] == "STOPPED"


def test_resume_stop_is_refused_on_the_same_terms(q):
    stop(q)
    epoch = q.get_stop_epoch()["epoch"]
    with pytest.raises(RuntimeTransitionRefused):
        q.resume_stop(generation=epoch + 1, who="owner:attacker", decision_id="not-a-t0-record")
    assert q.get_runtime_state()["state"] == "STOPPED"


def test_the_verified_owner_may_resume(q):
    """The control: the same call, by the principal the signed record names."""
    stop(q)
    q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")
    assert q.get_runtime_state()["state"] == "ACTIVE"
    latest = q.runtime_state_events(limit=1)[0]
    assert latest["to_state"] == "ACTIVE" and latest["decision_id"] == "T0-1234"


# ── each condition, one at a time ────────────────────────────────────────────

def test_no_verifier_refuses_everything(tmp_path):
    """The fail-closed default. A runtime that cannot check a decision record
    cannot tell an owner's resume from an attacker's, so it refuses both."""
    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=RefuseAllLoosening())
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="no authority verifier"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")
    assert q.get_runtime_state()["state"] == "STOPPED"


@pytest.mark.parametrize("signature", [SignatureStatus.UNSIGNED, SignatureStatus.INVALID,
                                       SignatureStatus.UNKEYED])
def test_an_unverified_snapshot_is_an_unavailable_input(tmp_path, signature):
    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=authority(signature=signature))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="not VALID"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_an_unavailable_snapshot_refuses(tmp_path):
    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=authority(available=False))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="unavailable"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_a_snapshot_reader_that_raises_refuses(tmp_path):
    class _Boom:
        def current(self, intent=None):
            raise OSError("snapshot file is gone")

    q = TaskQueue(str(tmp_path / "tasks.db"),
                  runtime_authority=SnapshotLooseningAuthority(_Boom(), build_commit=BUILD))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="unreadable"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_the_containment_build_check(tmp_path):
    """A decision to lift containment is a decision about the build that was
    contained. Replaying it against a later build re-authorises code nobody
    reviewed under it."""
    q = TaskQueue(str(tmp_path / "tasks.db"),
                  runtime_authority=SnapshotLooseningAuthority(snapshots(records=(T0,)),
                                                               build_commit=OTHER_BUILD))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="not the running build"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_a_record_naming_no_build_cannot_loosen(tmp_path):
    naked = DecisionRev(decision_id="T0-1234", body_hash="a" * 32, principals=("owner:hojun",))
    q = TaskQueue(str(tmp_path / "tasks.db"),
                  runtime_authority=SnapshotLooseningAuthority(snapshots(records=(naked,)),
                                                               build_commit=BUILD))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="is about build"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_an_unknown_running_build_refuses(tmp_path):
    class _NoBuild(SnapshotLooseningAuthority):
        def _build(self):
            return None

    q = TaskQueue(str(tmp_path / "tasks.db"),
                  runtime_authority=_NoBuild(snapshots(records=(T0,))))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="running build commit is unknown"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_a_verifier_that_raises_is_a_verifier_that_did_not_grant(tmp_path):
    class _Broken:
        def verify(self, **kw):
            raise RuntimeError("verifier exploded")

    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=_Broken())
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="authority verifier failed"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


def test_a_decision_that_does_not_cover_this_runtime_is_refused(tmp_path):
    rec = DecisionRev(decision_id="T0-1234", body_hash="a" * 32, principals=("owner:hojun",),
                      build_commits=(BUILD,), runtimes=("some-other-runtime",))
    q = TaskQueue(str(tmp_path / "tasks.db"),
                  runtime_authority=SnapshotLooseningAuthority(
                      snapshots(records=(rec,)), build_commit=BUILD, runtime="agent_crew"))
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="does not cover runtime"):
        q.set_stop_epoch(False, who="owner:hojun", decision_id="T0-1234")


# ── what the verifier must never be asked about ──────────────────────────────

def test_tightening_never_consults_the_verifier(tmp_path):
    """P6: "a tightening can never be blocked by an unavailable input"."""
    class _Explode:
        def verify(self, **kw):
            raise AssertionError("tightening must not ask for authority")

    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=_Explode())
    q.transition_runtime_state("DRAINING", who="operator:alfred")
    q.transition_runtime_state("QUARANTINED", who="runtime")
    q.transition_runtime_state("STOPPED", who="fleet_stop")
    assert q.get_runtime_state()["state"] == "STOPPED"


def test_the_principal_that_drained_may_undrain_without_the_owner(tmp_path):
    """P6 keeps this one: it reverses a state that same principal chose, and a
    quarantine entry is refused before it is reached."""
    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=RefuseAllLoosening())
    q.transition_runtime_state("DRAINING", who="operator:alfred", reason="planned drain")
    q.transition_runtime_state("ACTIVE", who="operator:alfred")
    assert q.get_runtime_state()["state"] == "ACTIVE"


def test_a_stopped_runtime_still_refuses_a_non_owner_shape(q):
    stop(q)
    with pytest.raises(RuntimeTransitionRefused, match="owner only"):
        q.transition_runtime_state("ACTIVE", who="operator:alfred", decision_id="T0-1234")


# ── #463 item 4: the lookup is keyed by (project, decision_id) ────────────────
#
# Two projects may reuse one decision_id. The first matching row used to win
# regardless of project, so project A's lookup could resolve project B's record.

A_REC = DecisionRev(decision_id="D-1", body_hash="a" * 32, principals=("owner:a",),
                    build_commits=(BUILD,), runtimes=("alfred",), project="alfred")
B_REC = DecisionRev(decision_id="D-1", body_hash="b" * 32, principals=("owner:b",),
                    build_commits=(OTHER_BUILD,), runtimes=("agent_crew",), project="agent_crew")


def scoped(project, *, records=(B_REC, A_REC), build=BUILD):
    """B first, so a lookup that ignores project picks B's row for project A."""
    return SnapshotLooseningAuthority(snapshots(records=records), build_commit=build,
                                      runtime=project, project=project)


def test_same_decision_id_resolves_to_project_as_own_row():
    auth = scoped("alfred")
    v = auth.verify(frm="STOPPED", to="ACTIVE", who="owner:a", decision_id="D-1")
    assert v.granted, v.reason
    # B's principal is not honoured through A's lookup: the record resolved is A's.
    v = auth.verify(frm="STOPPED", to="ACTIVE", who="owner:b", decision_id="D-1")
    assert not v.granted and "it names ['owner:a']" in v.reason


def test_same_decision_id_resolves_to_project_bs_own_row():
    auth = scoped("agent_crew", records=(A_REC, B_REC), build=OTHER_BUILD)
    v = auth.verify(frm="STOPPED", to="ACTIVE", who="owner:b", decision_id="D-1")
    assert v.granted, v.reason
    v = auth.verify(frm="STOPPED", to="ACTIVE", who="owner:a", decision_id="D-1")
    assert not v.granted and "it names ['owner:b']" in v.reason


def test_another_projects_record_is_never_a_candidate():
    """Only B has D-1: A's lookup finds nothing, even though B's row would pass
    every other check for A's principal."""
    b_for_a = DecisionRev(decision_id="D-1", body_hash="b" * 32, principals=("owner:a",),
                          build_commits=(BUILD,), runtimes=("alfred",), project="agent_crew")
    v = scoped("alfred", records=(b_for_a,)).verify(
        frm="STOPPED", to="ACTIVE", who="owner:a", decision_id="D-1")
    assert not v.granted
    assert "names no record in the signed snapshot for project 'alfred'" in v.reason


def test_an_ambiguous_decision_id_is_refused_not_resolved_by_order():
    """With no project to key by, two rows for one id are ambiguous."""
    auth = SnapshotLooseningAuthority(snapshots(records=(B_REC, A_REC)), build_commit=BUILD,
                                      runtime="alfred")
    v = auth.verify(frm="STOPPED", to="ACTIVE", who="owner:a", decision_id="D-1")
    assert not v.granted and "names 2 records" in v.reason


def test_single_project_lookup_is_unchanged(tmp_path):
    """A scoped record for this project, and an unscoped one, still loosen."""
    for rec in (A_REC, DecisionRev(decision_id="D-1", body_hash="a" * 32,
                                   principals=("owner:a",), build_commits=(BUILD,),
                                   runtimes=("alfred",))):
        q = TaskQueue(str(tmp_path / f"{rec.body_hash[:1]}{rec.project}.db"),
                      runtime_authority=scoped("alfred", records=(rec,)))
        stop(q)
        q.set_stop_epoch(False, who="owner:a", decision_id="D-1")
        assert q.get_runtime_state()["state"] == "ACTIVE"


def test_production_reader_and_wiring_key_the_lookup_by_project(tmp_path):
    """End to end: the snapshot's ``scope.project`` reaches the record, and the
    wired authority for each project resolves only that project's row."""
    import json
    import time

    from agent_crew.cea.input_providers.snapshot import CanonicalPolicySnapshotReader
    from agent_crew.cea.wiring import _authority

    now = time.time()
    p = tmp_path / "snap.json"
    rec = lambda project, who: {                                         # noqa: E731
        "decision_id": "D-1", "body_hash": who[-1] * 32, "principals": [who],
        "build_commits": [BUILD], "runtimes": [project], "scope": {"project": project}}
    p.write_text(json.dumps({"generation": 9, "produced_at": now,
                             "decisions": [rec("agent_crew", "owner:b"), rec("alfred", "owner:a")],
                             "signature": {"alg": "x", "key_id": "k", "value": "v"}}))
    reader = CanonicalPolicySnapshotReader(str(p), clock=lambda: now,
                                           verifier=lambda body, sig: SignatureStatus.VALID)
    assert [(r.decision_id, r.project) for r in reader.current().decisions] == [
        ("D-1", "agent_crew"), ("D-1", "alfred")]
    for project, own, other in (("alfred", "owner:a", "owner:b"),
                                ("agent_crew", "owner:b", "owner:a")):
        auth, _ = _authority(reader, True, project)
        auth._build_commit = BUILD
        assert auth.verify(frm="STOPPED", to="ACTIVE", who=own, decision_id="D-1").granted
        assert not auth.verify(frm="STOPPED", to="ACTIVE", who=other, decision_id="D-1").granted


def test_one_decision_expanded_across_capabilities_can_loosen(tmp_path):
    records = tuple(DecisionRev(decision_id="D-MULTI", body_hash="a" * 32,
                                principals=("owner:a",), build_commits=(BUILD,),
                                runtimes=("agent_crew",), project="agent_crew",
                                expires_at=1234567890.0) for _ in range(3))
    q = TaskQueue(str(tmp_path / "tasks.db"), runtime_authority=scoped(
        "agent_crew", records=records))
    stop(q)
    q.set_stop_epoch(False, who="owner:a", decision_id="D-MULTI")
    assert q.get_runtime_state()["state"] == "ACTIVE"


@pytest.mark.parametrize("changed", ["body_hash", "principals", "expires_at"])
def test_same_project_records_with_different_authority_refuse(changed):
    base = dict(decision_id="D-MULTI", body_hash="a" * 32,
                principals=("owner:a",), build_commits=(BUILD,),
                runtimes=("agent_crew",), project="agent_crew", expires_at=1234567890.0)
    other = {**base, changed: {"body_hash": "b" * 32,
                              "principals": ("owner:b",),
                              "expires_at": 1234567891.0}[changed]}
    v = scoped("agent_crew", records=(DecisionRev(**base), DecisionRev(**other))).verify(
        frm="STOPPED", to="ACTIVE", who="owner:a", decision_id="D-MULTI")
    assert not v.granted and "ambiguous id" in v.reason


def test_reader_and_wiring_accept_one_decision_expanded_by_capability(tmp_path):
    import json
    import time

    from agent_crew.cea.input_providers.snapshot import CanonicalPolicySnapshotReader
    from agent_crew.cea.wiring import _authority

    now = time.time()
    expiry = now + 300
    path = tmp_path / "snapshot.json"
    common = {"decision_id": "D-MULTI", "body_hash": "a" * 32,
              "principals": ["owner:a"], "build_commits": [BUILD],
              "runtimes": ["agent_crew"], "expires_at": expiry}
    path.write_text(json.dumps({"generation": 9, "produced_at": now,
                                "decisions": [{**common, "scope": {"project": "agent_crew",
                                                               "capability_id": cap}}
                                              for cap in ("one", "two", "three")],
                                "signature": {"alg": "x", "key_id": "k", "value": "v"}}))
    reader = CanonicalPolicySnapshotReader(str(path), clock=lambda: now,
                                           verifier=lambda body, sig: SignatureStatus.VALID)
    assert len(reader.current().decisions) == 3
    assert {r.expires_at for r in reader.current().decisions} == {expiry}
    auth, _ = _authority(reader, True, "agent_crew")
    auth._build_commit = BUILD
    assert auth.verify(frm="STOPPED", to="ACTIVE", who="owner:a",
                       decision_id="D-MULTI").granted
