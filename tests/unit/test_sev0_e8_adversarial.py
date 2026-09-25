"""SEV-0 E8(a): directive §11 adversarial / chaos scenarios — RED BASELINE.

Source: alfred governance/incidents/qouta-tokenomics-sev0-incident-directive.md
§11, gap map E8 (alfred sev0/decision-package-v1 @ccb9485), strand-B recursion
alfred#51 c5776412757.

§11's pass condition: "the ecosystem — not Qouta alone — discovers the
conflict and prevents unauthorized duplicate implementation." Every scenario
therefore drives the real admission entry point, ``POST /tasks`` (the direct
enqueue the incident went through), with fixtures in which the ecosystem
ALREADY HOLDS the knowledge — its own task history, or a capability registry —
and asserts that the duplicate is refused before it is queued or pushed.

Today nothing on that path consults ownership or capability (gap map E4/E5;
#294's in-flight check is advisory by design). The scenarios are therefore
``xfail(strict=True)``: they are expected to fail, and the first one that starts
passing turns into an XPASS failure, so the baseline cannot drift silently.
They go green with E4 (capability/ownership query on dispatch), E5
(STOP_AND_REVIEW on POST /tasks and admission) and G13 (fetch-first, fail-closed).

Controls (not xfail) keep the suite honest in the other direction: a genuinely
new capability must still be admitted, and a task that was never enqueued must
not be verifiable as dispatched. A "block everything" change fails them.

⛔Provisional contract. No capability registry is consulted today, so the
  registry fixture below and the env var that points at it
  (``AGENT_CREW_CAPABILITY_REGISTRY``) are this suite's stand-in for the E4
  source of truth — the alfred#4 registry + W3 schema. When E4 lands, change the
  fixture writer, not the scenarios: the scenarios assert outcomes only.

No production data: every repo, owner, ADR and capability id here is synthetic.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

PANE = "%900101"                       # owned by the fixture state; never a real pane
BLOCKED_BY = "E4 capability/ownership discovery + E5 STOP_AND_REVIEW are not implemented"

# One synthetic capability, as the ecosystem would already know it.
CAPABILITY = {
    "id": "tokenomics.risk_tier_enforcement",
    "aliases": ["risk-tier enforcement", "risk tier gating", "tier-based dispatch policy"],
    "owner": "quota-core",
    "repo": "example/quota-core",
    "adr": "ADR-001",
    "status": "implemented",
}


class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, target, message):
        self.calls.append((target, message))


@pytest.fixture
def crew(tmp_path, tmp_db, monkeypatch):
    """A server on a fresh DB with one owned implementer pane.

    Returns (client_factory, push, db). The factory builds a NEW app on the
    same DB each call — scenario 4 uses that as a process restart.
    """
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"project": "agent_crew", "pane_ids": [PANE]}))
    push = RecordingPush()

    def make():
        return TestClient(create_app(
            tmp_db, pane_map={"implementer": PANE}, state_path=str(state), port=8100,
            push_fn=push, watchdog_disabled=True, anomaly_disabled=True))

    return make, push, tmp_db


def write_registry(tmp_path, monkeypatch, *, capabilities=(CAPABILITY,),
                   generation="2026-09-23.1", path_exists=True):
    """Publish the provisional registry the ecosystem should consult."""
    path = tmp_path / "capability_registry.json"
    if path_exists:
        path.write_text(json.dumps({"generation": generation,
                                    "capabilities": list(capabilities)}))
    monkeypatch.setenv("AGENT_CREW_CAPABILITY_REGISTRY", str(path))
    return path


def seed_history(db, *, task_id="impl-risk-tier-001", description=None, project="quota-core",
                 status="completed", context=None):
    """The ecosystem's own memory: the capability was already implemented."""
    q = TaskQueue(db)
    q.enqueue(TaskRequest(
        task_id, "implement",
        description or "Implement risk-tier enforcement for Tokenomics dispatch (ADR-001)",
        branch="feat/risk-tier", project=project,
        context={"capability": CAPABILITY["id"], "adr": "ADR-001", **(context or {})}))
    if status in ("in_progress", "completed"):
        q.dequeue(role="implementer")
    if status == "completed":
        q.submit_result(task_id, TaskResult(task_id, "completed", "merged as PR #1",
                                            branch="feat/risk-tier", pr_number=1))


def request(task_id, description, *, project="agent_crew", context=None, issue=None):
    ctx = dict(context or {})
    if issue is not None:
        ctx["issue"] = issue
    return {"task_id": task_id, "task_type": "implement", "description": description,
            "branch": "main", "priority": 2, "context": ctx, "project": project}


def assert_duplicate_blocked(client, response, push, db, task_id):
    """The §11 pass condition, observable from outside.

    Not admitted (no 201), not queued as runnable, not pushed to a pane.
    Deliberately silent on HOW (status code, wording): E5 decides that.
    """
    assert response.status_code != 201, (
        f"duplicate admitted: {response.status_code} {response.json()}")
    rows = {r["task_id"]: r["status"] for r in TaskQueue(db).list_all_with_status()}
    assert rows.get(task_id) not in ("pending", "in_progress"), rows.get(task_id)
    assert not [m for _, m in push.calls if task_id in m], "duplicate pushed to a pane"


# ---------------------------------------------------------------------------
# The nine §11 scenarios.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=f"S1 red baseline: {BLOCKED_BY}")
def test_s1_existing_capability_presented_as_new(crew, tmp_path, monkeypatch):
    """The incident's shape: a finished capability requested again as new work."""
    make, push, db = crew
    seed_history(db)
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json=request(
            "s1", "New feature: enforce risk tiers before Tokenomics dispatch"))
        assert_duplicate_blocked(client, r, push, db, "s1")


@pytest.mark.xfail(strict=True, reason=f"S2 red baseline: {BLOCKED_BY}")
def test_s2_adr_context_omitted(crew, tmp_path, monkeypatch):
    """The request never names ADR-001; discovery must not depend on it doing so."""
    make, push, db = crew
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json=request(
            "s2", "Add risk-tier enforcement to the dispatcher"))   # no ADR, no capability id
        assert_duplicate_blocked(client, r, push, db, "s2")


@pytest.mark.xfail(strict=True, reason=f"S3 red baseline: {BLOCKED_BY}")
def test_s3_owner_not_called(crew, tmp_path, monkeypatch):
    """The capability has an owner (quota-core) that this request never asked."""
    make, push, db = crew
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json=request(
            "s3", "Implement risk-tier enforcement in agent_crew",
            context={"capability": CAPABILITY["id"]}))            # declared, owner not consulted
        assert_duplicate_blocked(client, r, push, db, "s3")


@pytest.mark.xfail(strict=True, reason=(
    "S4 red baseline: the #294 advisory that used to *detect* this duplicate is "
    "removed (ADR §11.1 row 14); the intent_hash index that replaces it as a "
    "decision is not built, and E5 STOP_AND_REVIEW is not implemented"))
def test_s4_session_restart_or_compaction(crew):
    """A coordinator loses its context and re-requests work already in flight."""
    make, push, db = crew
    with make() as client:
        first = client.post("/tasks", json=request(
            "s4-a", "Implement risk-tier enforcement", issue=4101))
        assert first.status_code == 201
    with make() as client:                                         # process restart, same DB
        r = client.post("/tasks", json=request(
            "s4-b", "Implement risk-tier enforcement", issue=4101))
        # ⛔No advisory assertion here any more. Asking whether the response
        #   *mentioned* the collision was always the weaker question, and the
        #   key is gone: the only thing worth asserting is that the duplicate
        #   did not become runnable work.
        assert_duplicate_blocked(client, r, push, db, "s4-b")


@pytest.mark.xfail(strict=True, reason=f"S5 red baseline: {BLOCKED_BY}")
def test_s5_renamed_equivalent_capability(crew, tmp_path, monkeypatch):
    """Same capability, new name: only the registry's aliases connect them."""
    make, push, db = crew
    seed_history(db)
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json=request(
            "s5", "Build tier-based dispatch policy gating"))
        assert_duplicate_blocked(client, r, push, db, "s5")


@pytest.mark.xfail(strict=True, reason=f"S6 red baseline: {BLOCKED_BY}")
def test_s6_same_capability_in_another_repo(crew, tmp_path, monkeypatch):
    """Implemented in example/quota-core; requested for a different project."""
    make, push, db = crew
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json=request(
            "s6", "Implement risk-tier enforcement for alpha_engine", project="alpha_engine"))
        assert_duplicate_blocked(client, r, push, db, "s6")


@pytest.mark.xfail(strict=True, reason=(
    "S7 red baseline: with the registry/owner source silent, nothing else in the "
    "ecosystem checks its own history (E4/E5); no fail-closed path (G13)"))
def test_s7_qouta_silent_or_unavailable(crew, tmp_path, monkeypatch):
    """The owner/registry source is down. The ecosystem's own history still
    records the capability, so the duplicate must still be caught — or, at
    minimum, admission must fail closed instead of proceeding blind."""
    make, push, db = crew
    seed_history(db)
    write_registry(tmp_path, monkeypatch, path_exists=False)       # source unreachable
    with make() as client:
        r = client.post("/tasks", json=request(
            "s7", "Implement risk-tier enforcement for Tokenomics dispatch"))
        assert_duplicate_blocked(client, r, push, db, "s7")


@pytest.mark.xfail(strict=True, reason=(
    "S8 red baseline: provider quota is not an admission constraint (G16) and "
    "duplicate discovery does not exist to fall back on (E4/E5)"))
def test_s8_claude_quota_exhausted(crew, tmp_path, monkeypatch):
    """Independent review (claude) is unavailable. A duplicate must not slip
    through on the reduced pipeline, and no provider may silently fill the
    reviewer's seat (owner 2026-09-20)."""
    make, push, db = crew
    seed_history(db)
    write_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_CREW_PROVIDER_EXHAUSTED", "claude")    # provisional signal (G16)
    with make() as client:
        r = client.post("/tasks", json=request(
            "s8", "Implement risk-tier enforcement for Tokenomics dispatch"))
        assert_duplicate_blocked(client, r, push, db, "s8")


@pytest.mark.xfail(strict=True, reason=(
    "S9 red baseline: no registry freshness/completeness check exists (E4 §9.2, G13)"))
@pytest.mark.parametrize("variant", ["stale", "partial"])
def test_s9_stale_or_partial_registry(crew, tmp_path, monkeypatch, variant):
    """A registry that is out of date or missing owner fields is not evidence
    that no owner exists. Admission must stop, not proceed."""
    make, push, db = crew
    seed_history(db)
    if variant == "stale":
        write_registry(tmp_path, monkeypatch, capabilities=(), generation="2026-09-19.1")
    else:
        partial = {k: v for k, v in CAPABILITY.items() if k not in ("owner", "adr")}
        write_registry(tmp_path, monkeypatch, capabilities=(partial,))
    with make() as client:
        r = client.post("/tasks", json=request(
            f"s9-{variant}", "Implement risk-tier enforcement for Tokenomics dispatch"))
        assert_duplicate_blocked(client, r, push, db, f"s9-{variant}")


# ---------------------------------------------------------------------------
# Strand B (alfred#51 c5776412757): "dispatched" claimed without evidence.
# The state ladder PLANNED → ENQUEUE_REQUESTED → ENQUEUED → CLAIMED → RUNNING
# → ARTIFACT_PRODUCED → VERIFIED must be provable from the authoritative queue.
# ---------------------------------------------------------------------------

def test_strand_b_control_unenqueued_task_is_not_verifiable(crew):
    """PASSES today: a task that was only 'instructed' is 404 in the queue,
    so ENQUEUED cannot be faked against GET /tasks/{id}."""
    make, _, _ = crew
    with make() as client:
        assert client.get("/tasks/w1-claimed-dispatched").status_code == 404


def test_strand_b_claimed_and_running_are_provable_from_the_queue(crew):
    """Green since G12 (9c90da1) landed on this lineage: the claim record is
    written in the dequeue transaction and the dispatch record on push (ADR
    §11.1 row 21: xfail markers are removed as lanes land)."""
    make, push, db = crew
    with make() as client:
        assert client.post("/tasks", json=request("sb", "genuinely new work")).status_code == 201
        assert push.calls, "precondition: the task was pushed"
        body = client.get("/tasks/sb").json()
    execution = body.get("execution") or {}
    assert execution.get("claimed_at"), "CLAIMED has no timestamp in the queue"
    assert execution.get("claimed_by_role") or execution.get("dispatch_agent"), \
        "CLAIMED has no claimant"
    assert execution.get("dispatched_at") and execution.get("dispatch_target"), \
        "dispatch has no target/time"


# ---------------------------------------------------------------------------
# Controls — must pass today AND after the fix.
# ---------------------------------------------------------------------------

def test_control_new_capability_is_admitted_and_pushed(crew, tmp_path, monkeypatch):
    """No history, registry knows nothing related: this is legitimate work."""
    make, push, db = crew
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json=request(
            "ctl-new", "Add a --json flag to `crew status` output"))
    assert r.status_code == 201
    assert [t for t, _ in push.calls] == [PANE]


def test_control_review_of_existing_work_is_not_a_duplicate(crew, tmp_path, monkeypatch):
    """Follow-up stages on the SAME capability are not duplicates (#294's
    reason for staying advisory): a review task must still be admitted."""
    make, _, db = crew
    seed_history(db, status="in_progress")
    write_registry(tmp_path, monkeypatch)
    with make() as client:
        r = client.post("/tasks", json={
            "task_id": "ctl-review", "task_type": "review",
            "description": "Review PR #1 — risk-tier enforcement", "branch": "feat/risk-tier",
            "priority": 2, "context": {"pr_number": 1, "capability": CAPABILITY["id"]},
            "project": "quota-core"})
    assert r.status_code == 201
