"""#716 — procedural lifecycle on real ADR-001 rows (no monkeypatched layers)."""
import json
import sqlite3

import pytest

from agent_crew import procedural_lifecycle as pl
from agent_crew import memory_runtime as mr
from agent_crew import procedural_memory as pm
from agent_crew.memory import MemoryRequest
from agent_crew.memory_runtime import (
    HybridMemoryStorage, MemoryRecord, MemoryScope, RuntimeMemoryProvider,
    SQLiteMemoryStorage, reconstruct_context,
)

T0 = 1_800_000_000.0
COORD = pl.Approver("coord-a", pl.ROLE_PROJECT_COORDINATOR, "proj-a", authenticated=True)
OWNER = pl.Approver("owner-a", pl.ROLE_OWNER, "proj-a", authenticated=True)
OTHER = pl.Approver("coord-b", pl.ROLE_PROJECT_COORDINATOR, "proj-b", authenticated=True)
TWO = [{"kind": "task", "ref": "t-1"}, {"kind": "pr", "ref": "pr-2"}]


@pytest.fixture(params=["sqlite", "hybrid"])
def store(tmp_path, request):
    path = str(tmp_path / "memory.db")
    return SQLiteMemoryStorage(path) if request.param == "sqlite" else HybridMemoryStorage(path)


def reopen(store):
    return type(store)(store.path)


def create(store, event="evt-1", *, project="proj-a", evidence=TWO, scope=None,
           trigger=None, **kw):
    return pl.create_at_decision(
        store, project=project, source_event_id=event, decision_ref=f"decision:{event}",
        source_ref=f"task:{event}", title="Check the precondition",
        rule="Verify the precondition before retrying", trigger=trigger or {"outcome_signature": "failed: x"},
        evidence=evidence, scope=scope, required_action="verify the precondition",
        now=T0, **kw)


def served_keys(store, project="proj-a"):
    """Every production active reader, unpatched."""
    scope = MemoryScope(project=project)
    keys = {r.key for r in store.retrieve(scope)}
    keys |= {r.key for r in store.retrieve(scope, query="precondition")}
    shadow, _ = store.retrieve_shadow(scope, {"procedural"}, 50, query="precondition")
    keys |= {r.key for r in shadow}
    keys |= {item.item_id for item in RuntimeMemoryProvider(store).retrieve(
        MemoryRequest(project=project, task_id="t", memory_types=("procedural",))).items}
    if isinstance(store, HybridMemoryStorage):
        ranked = store.retrieve_ranked(scope, "precondition", "implementer", 50, 10**6)
        keys |= {r["key"] for r in ranked["head"] + ranked["middle"]}
        keys |= {r["key"] for r in store.render_head(project)["records"]}
    return keys


def live_keys(store, monkeypatch, project="proj-a"):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    return {r["key"] for r in reconstruct_context(
        store, "implementer", "t", MemoryScope(project=project))["records"]}


def test_create_writes_one_candidate_row_with_contract_payload(store):
    out = create(store)
    assert out.ok and out.action == pl.ACTION_CREATE and out.to_status == pm.CANDIDATE
    assert out.due_at == T0 + pl.DEFAULT_INITIAL_DUE_SECONDS
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT layer,key,value,version FROM adr001_memory").fetchall()
    assert len(rows) == 1
    layer, key, value, version = rows[0]
    life = json.loads(value)["lifecycle"]
    assert (layer, key, version) == ("procedural", out.key, 1)
    assert life["contract"] == pl.CONTRACT_VERSION
    assert life["candidate_id"] == pl.candidate_id_for("proj-a", "evt-1")
    assert life["decision_ref"] == "decision:evt-1" and life["source_ref"] == "task:evt-1"
    assert life["created_at"] == T0 and life["status"] == pm.CANDIDATE


def test_duplicate_delivery_returns_the_same_row(store):
    first = create(store)
    second = create(store)
    changed = create(store, evidence=TWO[:1])
    assert second.ok and second.action == pl.ACTION_DUPLICATE
    assert {first.key, second.key, changed.key} == {first.key}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*), max(version) FROM adr001_memory").fetchone() == (1, 1)


def test_approve_promotes_the_same_row_and_it_is_served(store, monkeypatch):
    out = create(store, scope={"repos": ["r"]})
    assert out.key not in served_keys(store)
    promoted = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                    approver=COORD, now=T0 + 10)
    assert promoted.ok and (promoted.from_status, promoted.to_status) == (pm.CANDIDATE, pm.ACTIVE)
    assert promoted.key == out.key and promoted.version == 2
    assert out.key in served_keys(store)
    assert out.key in live_keys(store, monkeypatch)
    [proc] = pl.active_procedures(store, project="proj-a")
    assert proc.approved_by == "coord-a" and proc.state == pm.ACTIVE
    assert proc.enforcement == pm.SHADOW
    row = pl.candidate(store, project="proj-a", candidate_id=out.candidate_id, reviewer=COORD)
    assert [h["to"] for h in row.lifecycle["history"]] == [pm.CANDIDATE, pm.VALIDATED, pm.ACTIVE]


def test_reject_is_auditable_and_never_served(store, monkeypatch):
    out = create(store)
    assert not pl.reject_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                   approver=COORD, reason="").ok
    res = pl.reject_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                              approver=COORD, reason="one-off", now=T0 + 5)
    assert res.ok and res.to_status == pm.REJECTED
    row = pl.candidate(store, project="proj-a", candidate_id=out.candidate_id, reviewer=COORD)
    assert row.lifecycle["disposition"] == {"action": "reject", "by": "coord-a",
                                            "role": pl.ROLE_PROJECT_COORDINATOR,
                                            "at": T0 + 5, "reason": "one-off"}
    assert out.key not in served_keys(store)
    assert out.key not in live_keys(store, monkeypatch)
    again = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                 approver=COORD)
    assert not again.ok and pl.pending_candidates(store, project="proj-a", reviewer=COORD) == []


def test_hold_then_recheck_with_missing_evidence(store, monkeypatch):
    out = create(store, evidence=TWO[:1])  # global scope, one incident
    first = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                 approver=COORD, now=T0 + 1)
    assert not first.ok and any("scope is global" in r for r in first.reasons)
    bad = pl.hold_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                            approver=COORD, missing_evidence=["second incident"],
                            recheck_at=T0 + 10 * 86400, now=T0 + 2)
    assert not bad.ok  # beyond the recheck bound
    held = pl.hold_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                             approver=COORD, missing_evidence=["second incident"],
                             recheck_at=T0 + 3600, reason="needs repeat", now=T0 + 2)
    assert held.ok and held.to_status == pm.CANDIDATE and held.due_at == T0 + 3600
    assert held.missing_evidence == ["second incident"]
    [pending] = pl.pending_candidates(store, project="proj-a", reviewer=COORD)
    assert pending.held and pending.key == out.key
    assert pl.due_candidates(store, project="proj-a", reviewer=COORD, now=T0 + 100) == []
    assert [r.key for r in pl.due_candidates(store, project="proj-a", reviewer=COORD,
                                             now=T0 + 3600)] == [out.key]
    assert out.key not in served_keys(store) and out.key not in live_keys(store, monkeypatch)
    rechecked = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                     approver=COORD, now=T0 + 3700,
                                     additional_evidence=[{"kind": "task", "ref": "t-9"}])
    assert rechecked.ok and rechecked.version == 3
    row = pl.candidate(store, project="proj-a", candidate_id=out.candidate_id, reviewer=COORD)
    assert row.lifecycle["missing_evidence"] == [] and len(row.lifecycle["evidence"]) == 2


def test_supersede_deprecates_the_old_row_atomically(store, monkeypatch):
    old = create(store, "evt-old", scope={"repos": ["r"]})
    assert pl.promote_candidate(store, project="proj-a", candidate_id=old.candidate_id,
                                approver=COORD, now=T0).ok
    rival = create(store, "evt-rival", scope={"repos": ["r"]})
    conflict = pl.promote_candidate(store, project="proj-a", candidate_id=rival.candidate_id,
                                    approver=COORD, now=T0)
    assert not conflict.ok and any("supersede it explicitly" in r for r in conflict.reasons)
    new = create(store, "evt-new", scope={"repos": ["r"]}, supersedes=old.candidate_id)
    res = pl.promote_candidate(store, project="proj-a", candidate_id=new.candidate_id,
                               approver=COORD, now=T0 + 60)
    assert res.ok and res.superseded == old.candidate_id
    old_row = pl.candidate(store, project="proj-a", candidate_id=old.candidate_id, reviewer=COORD)
    assert old_row.status == pm.DEPRECATED
    assert old_row.lifecycle["superseded_by"] == new.candidate_id
    assert old_row.lifecycle["procedure"]["state"] == pm.DEPRECATED
    served = served_keys(store)
    assert new.key in served and old.key not in served and rival.key not in served
    assert old.key not in live_keys(store, monkeypatch)
    assert [p.procedure_id for p in pl.active_procedures(store, project="proj-a")] == [
        pl.candidate(store, project="proj-a", candidate_id=new.candidate_id,
                     reviewer=COORD).lifecycle["procedure"]["procedure_id"]]


def test_restart_round_trip_keeps_identity_and_status(store):
    a, b, c = (create(store, e, scope={"repos": [e]}) for e in ("e1", "e2", "e3"))
    pl.promote_candidate(store, project="proj-a", candidate_id=a.candidate_id, approver=COORD, now=T0)
    pl.reject_candidate(store, project="proj-a", candidate_id=b.candidate_id, approver=COORD,
                        reason="no", now=T0)
    pl.hold_candidate(store, project="proj-a", candidate_id=c.candidate_id, approver=COORD,
                      missing_evidence=["x"], recheck_at=T0 + 60, now=T0)
    again = reopen(store)
    status = {k: pl.candidate(again, project="proj-a", candidate_id=o.candidate_id,
                              reviewer=COORD).status for k, o in (("a", a), ("b", b), ("c", c))}
    assert status == {"a": pm.ACTIVE, "b": pm.REJECTED, "c": pm.CANDIDATE}
    assert create(again, "e1", scope={"repos": ["e1"]}).action == pl.ACTION_DUPLICATE
    assert served_keys(again) & {a.key, b.key, c.key} == {a.key}


def test_cross_project_reads_and_dispositions_are_zero(store):
    out = create(store)
    create(store, "evt-b", project="proj-b")
    assert pl.pending_candidates(store, project="proj-a", reviewer=OTHER) == []
    assert pl.due_candidates(store, project="proj-a", reviewer=OTHER, now=T0 + 10**6) == []
    assert pl.candidate(store, project="proj-a", candidate_id=out.candidate_id, reviewer=OTHER) is None
    assert [r.project for r in pl.pending_candidates(store, project="proj-a", reviewer=COORD)] == ["proj-a"]
    denied = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id, approver=OTHER)
    assert not denied.ok
    wrong_project = pl.promote_candidate(store, project="proj-b", candidate_id=out.candidate_id,
                                         approver=OTHER)
    assert not wrong_project.ok and wrong_project.reasons == ["candidate not found in project"]
    unauth = pl.Approver("coord-a", pl.ROLE_PROJECT_COORDINATOR, "proj-a")
    assert pl.pending_candidates(store, project="proj-a", reviewer=unauth) == []


def test_broad_scope_single_evidence_promotion_is_rejected(store):
    out = create(store, evidence=TWO[:1])
    res = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                               approver=COORD)
    assert not res.ok and res.to_status == pm.CANDIDATE
    assert any("scope is global" in r for r in res.reasons)
    assert pl.active_procedures(store, project="proj-a") == []


@pytest.mark.parametrize("approver,why", [
    (pl.Approver("coord-a", pl.ROLE_PROJECT_COORDINATOR, "proj-a"), "not authenticated"),
    (pl.Approver("", pl.ROLE_PROJECT_COORDINATOR, "proj-a", True), "identity is empty"),
    (pl.Approver("x", "reviewer", "proj-a", True), "requires role"),
    (OWNER, "requires role project_coordinator"),
    ("coord-a", "explicit Approver"),
])
def test_unqualified_approver_is_rejected(store, approver, why):
    out = create(store, scope={"repos": ["r"]})
    res = pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                               approver=approver)
    assert not res.ok and any(why in r for r in res.reasons)
    assert pl.active_procedures(store, project="proj-a") == []


def test_owner_principle_requires_authenticated_owner(store):
    out = create(store, scope={"repos": ["r"]}, origin=pl.ORIGIN_OWNER)
    assert not pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                    approver=COORD).ok
    assert pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                approver=OWNER).ok


@pytest.mark.parametrize("field", ["source_event_id", "decision_ref", "source_ref"])
def test_missing_source_is_rejected_without_a_write(store, field):
    kw = dict(project="proj-a", source_event_id="e", decision_ref="d", source_ref="s",
              title="t", rule="r", trigger={"k": "v"}, evidence=TWO)
    kw[field] = ""
    res = pl.create_at_decision(store, **kw)
    assert not res.ok and f"missing {field}" in res.reasons
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 0


def test_unresolved_evidence_is_rejected(store):
    res = create(store, evidence=[{"kind": "task", "ref": ""}])
    assert not res.ok and "evidence does not resolve" in res.reasons[0]


def test_new_row_without_status_is_not_implicitly_active(store, monkeypatch):
    scope = MemoryScope(project="proj-a")
    store.put(MemoryRecord("procedural", "procedure:cand-nostatus",
                           {"kind": "procedure_candidate", "text": "precondition"}, scope))
    store.put(MemoryRecord("procedural", "procedure:cand-weird",
                           {"kind": "procedure_candidate", "text": "precondition",
                            "lifecycle": {"status": "validated_lesson"}}, scope))
    served = served_keys(store)
    assert "procedure:cand-nostatus" not in served and "procedure:cand-weird" not in served
    assert not {"procedure:cand-nostatus", "procedure:cand-weird"} & live_keys(store, monkeypatch)


def test_legacy_owner_principles_are_still_served(store, monkeypatch):
    scope = MemoryScope(project="proj-a")
    store.put(MemoryRecord("procedural", "owner_principle:legacy",
                           {"text": "precondition principle"}, scope))
    create(store)  # a pending candidate next to it changes nothing
    assert "owner_principle:legacy" in served_keys(store)
    assert "owner_principle:legacy" in live_keys(store, monkeypatch)


def test_expired_active_procedure_is_not_served(store):
    out = create(store, scope={"repos": ["r"]})
    pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                         approver=COORD, now=1.0)  # 180-day TTL ended long ago
    assert out.key not in served_keys(store)
    assert pl.active_procedures(store, project="proj-a") == []


def _malformed_active_rows(template: dict) -> dict:
    """Variants of a really promoted row that claim to be active but are not servable."""
    def variant(mutate):
        value = json.loads(json.dumps(template))
        mutate(value["lifecycle"])
        return value

    def set_expiry(expiry):
        return lambda lc: lc["procedure"].__setitem__("expires_at", expiry)

    return {
        "procedure:bad-no-procedure": variant(lambda lc: lc.pop("procedure")),
        "procedure:bad-null-procedure": variant(lambda lc: lc.__setitem__("procedure", None)),
        "procedure:bad-string-procedure": variant(lambda lc: lc.__setitem__("procedure", "x")),
        "procedure:bad-no-id": variant(lambda lc: lc["procedure"].pop("procedure_id")),
        "procedure:bad-pending-state": variant(
            lambda lc: lc["procedure"].__setitem__("state", pm.CANDIDATE)),
        "procedure:bad-expiry-text": variant(set_expiry("not-a-time")),
        "procedure:bad-expiry-missing": variant(lambda lc: lc["procedure"].pop("expires_at")),
        "procedure:bad-expiry-zero": variant(set_expiry(0)),
        "procedure:bad-expiry-null": variant(set_expiry(None)),
        "procedure:bad-expiry-bool": variant(set_expiry(True)),
        # Storable on every SQLite build (a non-finite float is not valid JSON).
        "procedure:bad-expiry-list": variant(set_expiry([4102444800])),
        "procedure:bad-expiry-object": variant(set_expiry({"at": 4102444800})),
        # Required Procedure fields: active_procedures must not raise on them.
        "procedure:bad-no-version": variant(lambda lc: lc["procedure"].pop("version")),
        "procedure:bad-text-version": variant(
            lambda lc: lc["procedure"].__setitem__("version", "1")),
        "procedure:bad-bool-version": variant(
            lambda lc: lc["procedure"].__setitem__("version", True)),
        "procedure:bad-no-title": variant(lambda lc: lc["procedure"].pop("title")),
        "procedure:bad-null-title": variant(
            lambda lc: lc["procedure"].__setitem__("title", None)),
        "procedure:bad-no-rule": variant(lambda lc: lc["procedure"].pop("rule")),
        "procedure:bad-list-rule": variant(
            lambda lc: lc["procedure"].__setitem__("rule", ["r"])),
        "procedure:bad-lifecycle-list": {**template, "lifecycle": []},
    }


def test_active_status_without_a_valid_procedure_or_expiry_fails_closed(store, monkeypatch):
    """Review finding on PR #771: Python, SQL and hybrid readers agree and never raise."""
    out = create(store, scope={"repos": ["r"]})
    assert pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                approver=COORD).ok
    template = pl._load(store, "proj-a", "", out.candidate_id).value
    scope = MemoryScope(project="proj-a")
    bad = _malformed_active_rows(template)
    for key, value in bad.items():
        store.put(MemoryRecord("procedural", key, value, scope))
    store.put(MemoryRecord("procedural", "owner_principle:null-lifecycle",
                           {"text": "precondition principle", "lifecycle": None}, scope))

    for s in (store, reopen(store)):
        served = served_keys(s)  # retrieve, retrieve_shadow, provider, hybrid head/render
        assert out.key in served, "the well-formed active row stays served"
        assert "owner_principle:null-lifecycle" in served
        assert not set(bad) & served
        assert not set(bad) & live_keys(s, monkeypatch)
        assert [p.procedure_id for p in pl.active_procedures(s, project="proj-a")] == \
            [template["lifecycle"]["procedure"]["procedure_id"]]

    # The Python predicate and its SQL form decide every row identically.
    with sqlite3.connect(store.path) as db:
        sql_ok = {k for (k,) in db.execute(
            "SELECT key FROM adr001_memory WHERE " + mr.procedural_servable_sql())}
        python_ok = {k for k, layer, v in db.execute(
            "SELECT key, layer, value FROM adr001_memory")
            if mr.procedural_row_servable(layer, json.loads(v))}
    assert sql_ok == python_ok
    assert out.key in sql_ok and not set(bad) & sql_ok


def _set(field, value):
    return lambda lc: lc["procedure"].__setitem__(field, value)


def _drop(field):
    return lambda lc: lc["procedure"].pop(field)


def _scope(mutate):
    return lambda lc: mutate(lc["procedure"]["scope"])


#: Round 3 of PR #771: fix the class, not the field. Every variant claims to be
#: an active, unexpired procedure; each breaks one thing the shared validator
#: (`procedural_memory.procedure_from_stored`) checks.
MALFORMED = {
    "id-missing": _drop("procedure_id"),
    "id-int": _set("procedure_id", 7),
    "id-empty": _set("procedure_id", ""),
    "version-missing": _drop("version"),
    "version-text": _set("version", "1"),
    "version-float": _set("version", 1.0),
    "version-bool": _set("version", True),
    "title-missing": _drop("title"),
    "title-null": _set("title", None),
    "title-int": _set("title", 3),
    "rule-missing": _drop("rule"),
    "rule-list": _set("rule", ["r"]),
    "scope-missing": _drop("scope"),
    "scope-string": _set("scope", "broken"),
    "scope-null": _set("scope", None),
    "scope-list": _set("scope", [["repos", ["r"]]]),
    "scope-unknown-key": _scope(lambda sc: sc.__setitem__("colour", ["red"])),
    "scope-missing-key": _scope(lambda sc: sc.pop("repos")),
    "scope-empty": _set("scope", {}),
    "scope-value-string": _scope(lambda sc: sc.__setitem__("repos", "r")),
    "scope-value-ints": _scope(lambda sc: sc.__setitem__("paths", [1, 2])),
    "evidence-string": _set("evidence", "pr-2"),
    "evidence-bad-item": _set("evidence", [{"kind": "pr", "ref": 2}]),
    "trigger-list": _set("trigger", ["failed: x"]),
    "confidence-text": _set("confidence", "high"),
    "unknown-field": _set("surprise", 1),
    "state-missing": _drop("state"),
    "state-int": _set("state", 1),
    "expires-missing": _drop("expires_at"),
    "expires-text": _set("expires_at", "4102444800"),
    "expires-invalid-text": _set("expires_at", "not-a-time"),
    "expires-null": _set("expires_at", None),
    "expires-bool": _set("expires_at", True),
    "expires-list": _set("expires_at", [4102444800]),
    "expires-past": _set("expires_at", 1.0),
    "status-missing": lambda lc: lc.pop("status"),
    "status-int": lambda lc: lc.__setitem__("status", 1),
    "status-pending": lambda lc: lc.__setitem__("status", pm.CANDIDATE),
    "status-deprecated": lambda lc: lc.__setitem__("status", pm.DEPRECATED),
}


@pytest.mark.parametrize("variant", sorted(MALFORMED))
def test_malformed_active_row_is_never_served_and_never_raises(store, monkeypatch, caplog,
                                                               variant):
    """Python validator is the authority on SQLite, hybrid and the public API."""
    out = create(store, scope={"repos": ["r"]})
    assert pl.promote_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                                approver=COORD).ok
    template = pl._load(store, "proj-a", "", out.candidate_id).value
    good_id = template["lifecycle"]["procedure"]["procedure_id"]
    bad = json.loads(json.dumps(template))
    bad["lifecycle"]["procedure"]["procedure_id"] = "proc-bad"
    MALFORMED[variant](bad["lifecycle"])
    bad_key = f"procedure:bad-{variant}"
    scope = MemoryScope(project="proj-a")
    store.put(MemoryRecord("procedural", bad_key, bad, scope))

    for s in (store, reopen(store)):
        served = served_keys(s)  # retrieve, retrieve_shadow, provider, hybrid head/render
        assert out.key in served, "the well-formed active row stays served"
        assert bad_key not in served
        assert bad_key not in live_keys(s, monkeypatch)
        assert [r.key for r in pl.active_rows(s, project="proj-a")] == [out.key]
        assert [p.procedure_id for p in pl.active_procedures(s, project="proj-a")] == [good_id]
        assert not mr.procedural_row_servable("procedural", bad, key=bad_key)

    # SQL is only a coarse prefilter: it may admit the row, Python never does.
    with sqlite3.connect(store.path) as db:
        sql_ok = {k for (k,) in db.execute(
            "SELECT key FROM adr001_memory WHERE " + mr.procedural_servable_sql())}
    assert out.key in sql_ok

    # A row that claims active is logged once, not on every read.
    status = bad["lifecycle"].get("status")
    logged = [r for r in caplog.records if bad_key in r.getMessage()]
    assert len(logged) == (1 if status == pm.ACTIVE else 0)


def test_shared_validator_accepts_what_promotion_writes():
    procedure = pm.Procedure("p-1", 1, "t", "r", scope=pm.Scope(repos=["r"]),
                             evidence=[pm.Evidence("pr", "2")], state=pm.ACTIVE,
                             expires_at=T0)
    assert pm.procedure_from_stored(procedure.to_dict()) == procedure
    with pytest.raises((TypeError, ValueError)):
        pm.procedure_from_stored({**procedure.to_dict(), "scope": "broken"})


def test_concurrent_transition_loses_cleanly(store):
    out = create(store, scope={"repos": ["r"]})
    stale = pl._load(store, "proj-a", "", out.candidate_id)
    assert pl.reject_candidate(store, project="proj-a", candidate_id=out.candidate_id,
                               approver=COORD, reason="first").ok
    record = MemoryRecord("procedural", stale.key, stale.value, MemoryScope(project="proj-a"),
                          stale.version + 1)
    assert store.compare_and_set([(record, stale.version)]) is False
    assert pl.candidate(store, project="proj-a", candidate_id=out.candidate_id,
                        reviewer=COORD).status == pm.REJECTED


def test_consumer_adapter_uses_only_the_public_interface(store):
    """A decision-post adapter and due scanner built on the exported outcome."""
    posted = []

    def on_decision(event):
        out = pl.create_at_decision(
            store, project=event["project"], source_event_id=event["id"],
            decision_ref=event["decision"], source_ref=event["source"], title=event["title"],
            rule=event["rule"], trigger=event["trigger"], evidence=event["evidence"],
            scope=event.get("scope"), required_action=event["action"], now=event["at"])
        posted.append(out.to_dict())
        return out

    def scan(now):
        for row in pl.due_candidates(store, project="proj-a", reviewer=COORD, now=now):
            posted.append({"action": "due", "candidate_id": row.candidate_id,
                           "due_at": row.due_at, "status": row.status})

    event = {"project": "proj-a", "id": "msg-1", "decision": "issue:716#c1",
             "source": "task:abc", "title": "t", "rule": "r", "trigger": {"k": "v"},
             "evidence": TWO, "action": "do it", "at": T0}
    on_decision(event)
    on_decision(event)
    scan(T0 + pl.DEFAULT_INITIAL_DUE_SECONDS)
    assert [p["action"] for p in posted] == ["create", "duplicate", "due"]
    assert all(p.get("contract", pl.CONTRACT_VERSION) == pl.CONTRACT_VERSION for p in posted)
    json.dumps(posted)  # outcome is serialisable for an external adapter
