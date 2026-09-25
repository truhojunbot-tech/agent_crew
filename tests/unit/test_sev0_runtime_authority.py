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
