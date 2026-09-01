"""#240 — episode → lesson → procedure → reuse, with the gates that matter.

Retrieval (#239) makes past work visible; it does not stop a crew repeating a
mistake. This closes the loop. Most of these tests exist to keep the *refusals*
working, because a procedural-memory system earns its keep by what it declines
to promote:

  * an LLM-extracted candidate is never autonomously an active rule;
  * one narrow incident never becomes a global rule;
  * a new procedure never starts by enforcing — it starts in shadow so its
    false-positive rate is measured first;
  * a procedure never outranks current code or the acceptance criteria;
  * a provider/runtime incident is not a project procedure.

The seven fixture cases named in #240's Evaluation section are covered in
`TestEvaluationFixtures` at the bottom.
"""

import time

import pytest

from agent_crew.context_pack import (
    TYPE_AC,
    TYPE_CODE,
    TYPE_PROCEDURE,
    IssueProvider,
    ProcedureProvider,
    RetrievalQuery,
    plan_pack,
)
from agent_crew.procedural_memory import (
    ACTIVE,
    ADVISORY,
    CANDIDATE,
    DEPRECATED,
    HARD,
    REJECTED,
    SHADOW,
    VALIDATED,
    CandidateLesson,
    Evidence,
    Procedure,
    Scope,
    ValidationResult,
    append_procedure,
    can_transition,
    deprecate,
    episode_completeness,
    extract_candidates,
    load_procedures,
    mark_stale_by_source_change,
    match_procedures,
    promote,
    ready_for_hard_enforcement,
    reject,
    resolve_precedence,
    resolve_precedence as _rp,  # noqa: F401  (kept for readability below)
    shadow_metrics,
    shadow_record,
    telemetry,
    validate_candidate,
)

NOW = 1_800_000_000.0


def _ep(task_id, outcome="completed", findings=None, repo="org/repo", **kw):
    e = {"task_id": task_id, "outcome": outcome, "repo": repo,
         "summary": "did a thing", "role": "implementer"}
    if findings is not None:
        e["findings"] = findings
    e.update(kw)
    return e


def _candidate(**kw):
    base = dict(
        lesson_id="lesson-abc", title="check the data layer first",
        rule="Confirm the primary data layer before declaring data unrecoverable.",
        pattern="repeated_failure_signature",
        scope=Scope(repos=["org/repo"], paths=["src/"]),
        evidence=[Evidence(kind="task", ref="t-1"), Evidence(kind="task", ref="t-2")],
        trigger={"outcome_signature": "failed:data_loss"},
        required_action="check the primary data layer",
    )
    base.update(kw)
    return CandidateLesson(**base)


def _valid():
    return ValidationResult(ok=True)


# ── 1. episode completeness gates extraction ──────────────────────────


def test_complete_episode_has_no_gaps():
    assert episode_completeness(_ep("t-1")) == []


def test_missing_outcome_is_visibly_incomplete():
    """⛔A rule built on a task whose result we never saw is a guess."""
    gaps = episode_completeness(_ep("t-1", outcome=""))
    assert any("outcome" in g for g in gaps)


def test_failed_episode_without_evidence_is_incomplete():
    gaps = episode_completeness({"task_id": "t", "outcome": "failed:x"})
    assert any("no findings or summary" in g for g in gaps)


def test_unresolved_incident_blocks_extraction():
    gaps = episode_completeness(_ep("t-1", unresolved_incident=True))
    assert any("unresolved incident" in g for g in gaps)


def test_incomplete_episodes_are_excluded_from_extraction():
    eps = [_ep("t-1", outcome=""), _ep("t-2", outcome="")]
    assert extract_candidates(eps) == []


# ── 2. candidate extraction needs repetition ──────────────────────────


def test_single_incident_produces_no_candidate():
    """⛔One event is an anecdote. Two is a pattern worth proposing."""
    assert extract_candidates([_ep("t-1", outcome="failed:boom")]) == []


def test_repeated_failure_signature_yields_a_candidate():
    eps = [_ep("t-1", outcome="failed:missing_callsite"),
           _ep("t-2", outcome="failed:missing_callsite")]
    got = extract_candidates(eps)

    assert len(got) == 1
    assert got[0].trigger["outcome_signature"] == "failed:missing_callsite"
    assert {e.ref for e in got[0].evidence} == {"t-1", "t-2"}


def test_repeated_review_finding_yields_a_candidate():
    eps = [_ep("t-1", findings=["the guard is not atomic"]),
           _ep("t-2", findings=["the guard is not atomic here too"])]
    got = extract_candidates(eps)

    assert any(c.pattern == "repeated_review_rejection" for c in got)


def test_candidate_scope_comes_from_where_it_actually_happened():
    """⛔Scope is observed, never generalised."""
    eps = [_ep("t-1", outcome="failed:x", repo="org/a"),
           _ep("t-2", outcome="failed:x", repo="org/a")]
    c = extract_candidates(eps)[0]

    assert c.scope.repos == ["org/a"]


def test_provider_incident_is_flagged_not_promoted():
    """⛔#240 non-goal: a quota/runtime incident is not a project procedure."""
    eps = [_ep("t-1", outcome="failed:agy_quota_exhausted"),
           _ep("t-2", outcome="failed:agy_quota_exhausted")]
    c = extract_candidates(eps)[0]

    assert c.incomplete_reasons
    assert not validate_candidate(c)


# ── 3. validation refuses the dangerous shapes ────────────────────────


def test_a_well_formed_candidate_validates():
    assert validate_candidate(_candidate()).ok is True


def test_global_scope_from_one_incident_is_rejected():
    """★The classic over-reach that makes these systems hated."""
    c = _candidate(scope=Scope(), evidence=[Evidence(kind="task", ref="t-1")])
    res = validate_candidate(c)

    assert not res.ok
    assert any("global" in r and "narrow the scope" in r for r in res.reasons)


def test_global_scope_is_allowed_with_enough_evidence():
    c = _candidate(scope=Scope(), evidence=[Evidence(kind="task", ref=f"t-{i}")
                                            for i in range(3)])
    assert validate_candidate(c).ok is True


def test_unresolvable_evidence_is_rejected():
    c = _candidate(evidence=[Evidence(kind="", ref="")])
    assert any("does not resolve" in r for r in validate_candidate(c).reasons)


def test_no_evidence_is_rejected():
    assert any("no supporting evidence" in r
               for r in validate_candidate(_candidate(evidence=[])).reasons)


def test_untriggerable_candidate_is_rejected():
    c = _candidate(trigger={})
    assert any("machine-detectable trigger" in r
               for r in validate_candidate(c).reasons)


def test_unactionable_candidate_is_rejected():
    c = _candidate(required_action="", prohibited_action="")
    assert any("not actionable" in r for r in validate_candidate(c).reasons)


def test_contradiction_with_authoritative_text_is_rejected():
    """⛔Derived governance may never contradict current code/ADR."""
    c = _candidate(prohibited_action="use BEGIN IMMEDIATE")
    res = validate_candidate(c, authoritative_text="we always use BEGIN IMMEDIATE here")

    assert any("contradicts current authoritative" in r for r in res.reasons)


def test_conflict_with_an_existing_active_procedure_is_surfaced():
    existing = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    res = validate_candidate(_candidate(), existing=[existing], now=NOW)

    assert not res.ok
    assert res.conflicts and "supersede it explicitly" in res.conflicts[0]


# ── 4. promotion is never autonomous ──────────────────────────────────


def test_promotion_requires_an_approver():
    """★The single most important refusal in this module."""
    with pytest.raises(ValueError, match="requires approved_by"):
        promote(_candidate(), approved_by="", validation=_valid())


def test_invalid_candidate_cannot_be_promoted():
    bad = ValidationResult(ok=False, reasons=["scope is global"])
    with pytest.raises(ValueError, match="invalid candidate"):
        promote(_candidate(), approved_by="op", validation=bad)


def test_new_procedure_may_not_start_at_hard_enforcement():
    """★Shadow first. A rule that has never been measured cannot block work."""
    with pytest.raises(ValueError, match="may not start at hard enforcement"):
        promote(_candidate(), approved_by="op", validation=_valid(),
                enforcement=HARD)


def test_promotion_records_who_and_how():
    p = promote(_candidate(), approved_by="operator@host", validation=_valid(),
                now=NOW)

    assert p.state == ACTIVE and p.enforcement == SHADOW
    assert p.approved_by == "operator@host"
    assert p.history[0]["by"] == "operator@host"
    assert p.effective_at == NOW and p.review_at > NOW and p.expires_at > p.review_at


def test_rejection_is_recorded_not_deleted():
    c = reject(_candidate(), "too broad for the evidence")
    assert c.state == REJECTED
    assert "too broad for the evidence" in c.incomplete_reasons


def test_lifecycle_transitions_are_constrained():
    assert can_transition(CANDIDATE, VALIDATED)
    assert can_transition(CANDIDATE, REJECTED)
    assert can_transition(ACTIVE, DEPRECATED)
    # ⛔No shortcut from a raw episode straight to an active rule.
    assert not can_transition("raw_episode", ACTIVE)
    assert not can_transition(REJECTED, ACTIVE)
    assert not can_transition(DEPRECATED, ACTIVE)


# ── 5. versioning, expiry, conflict, decay ────────────────────────────


def test_expired_procedure_is_not_active():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    assert p.is_active(NOW) is True
    assert p.is_active(NOW + 400 * 86400) is False


def test_review_date_is_flagged_before_expiry():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    assert p.needs_review(NOW) is False
    assert p.needs_review(NOW + 120 * 86400) is True


def test_a_superseded_version_loses_to_its_successor():
    v1 = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    v2 = promote(_candidate(), approved_by="op", validation=_valid(),
                 version=2, supersedes=v1.procedure_id, now=NOW)

    effective, conflicts = resolve_precedence([v1, v2], now=NOW)
    assert [p.version for p in effective] == [2]
    assert conflicts == []


def test_repo_specific_procedure_outranks_a_generic_one():
    generic = promote(_candidate(scope=Scope(repos=[], paths=["src/"]),
                                 evidence=[Evidence(kind="task", ref=f"t{i}")
                                           for i in range(3)]),
                      approved_by="op", validation=_valid(), now=NOW)
    specific = promote(_candidate(lesson_id="lesson-xyz",
                                  scope=Scope(repos=["org/repo"], paths=["src/"])),
                       approved_by="op", validation=_valid(), now=NOW)

    effective, conflicts = resolve_precedence([generic, specific],
                                              repo="org/repo", now=NOW)
    assert conflicts == []
    assert [p.scope.repos for p in effective] == [["org/repo"]]


def test_an_unresolved_conflict_is_downgraded_to_advisory():
    """⛔#240 §6: unresolved conflicts block hard enforcement."""
    a = promote(_candidate(lesson_id="lesson-a"), approved_by="op",
                validation=_valid(), now=NOW)
    b = promote(_candidate(lesson_id="lesson-b"), approved_by="op",
                validation=_valid(), now=NOW)

    effective, conflicts = resolve_precedence([a, b], now=NOW)
    assert conflicts and "unresolved conflict" in conflicts[0]
    assert all(p.enforcement == ADVISORY for p in effective)


def test_source_change_marks_a_scoped_procedure_stale():
    p = promote(_candidate(scope=Scope(repos=["org/repo"], paths=["src/agent_crew/"])),
                approved_by="op", validation=_valid(), now=NOW)

    assert mark_stale_by_source_change([p], ["src/agent_crew/server.py"]) == [p]
    assert mark_stale_by_source_change([p], ["docs/readme.md"]) == []


def test_deprecation_keeps_the_audit_trail():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    deprecate(p, by="operator", reason="superseded by ADR-12", now=NOW + 10)

    assert p.state == DEPRECATED
    assert p.is_active(NOW + 20) is False
    assert p.history[-1]["reason"] == "superseded by ADR-12"


# ── 6. matching + shadow enforcement ──────────────────────────────────


def test_matching_requires_scope_and_trigger():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)

    hit = match_procedures([p], repo="org/repo", paths=["src/x.py"],
                           outcome_signature="failed:data_loss", now=NOW)
    assert hit and "failed:data_loss" in hit[0][1]

    # wrong repo -> no match
    assert match_procedures([p], repo="org/other", paths=["src/x.py"],
                            outcome_signature="failed:data_loss", now=NOW) == []
    # right repo, trigger absent -> no match
    assert match_procedures([p], repo="org/repo", paths=["src/x.py"],
                            outcome_signature="failed:other", now=NOW) == []


def test_deprecated_procedures_never_match():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    deprecate(p, by="op", reason="stale", now=NOW)
    assert match_procedures([p], repo="org/repo", paths=["src/x.py"],
                            outcome_signature="failed:data_loss", now=NOW) == []


def test_every_match_carries_an_inclusion_reason():
    """⛔#240 §4: no silent inclusions."""
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    for _proc, reason in match_procedures([p], repo="org/repo", paths=["src/x.py"],
                                          outcome_signature="failed:data_loss",
                                          now=NOW):
        assert reason


def test_shadow_records_what_would_have_happened_without_doing_it():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    rec = shadow_record(p, task_id="t-9", triggered=True,
                        would="would_require_data_layer_check", now=NOW)

    assert rec["triggered"] is True
    assert rec["would"] == "would_require_data_layer_check"
    assert rec["enforcement"] == SHADOW


def test_hard_enforcement_is_refused_without_shadow_evidence():
    """★No rule blocks work until its false-positive rate is measured."""
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    metrics = shadow_metrics([shadow_record(p, task_id="t", triggered=True,
                                            would="x", now=NOW)])

    res = ready_for_hard_enforcement(p, metrics)
    assert not res.ok
    assert any("shadow trigger" in r for r in res.reasons)


def test_hard_enforcement_is_refused_when_the_rule_over_fires():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    recs = [shadow_record(p, task_id=f"t{i}", triggered=True, would="x", now=NOW)
            for i in range(8)]
    metrics = shadow_metrics(recs, overrides=[{"overridden": True}] * 5)

    res = ready_for_hard_enforcement(p, metrics)
    assert any("override rate" in r for r in res.reasons)


def test_hard_enforcement_allowed_on_clean_shadow_evidence():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    recs = [shadow_record(p, task_id=f"t{i}", triggered=True, would="x", now=NOW)
            for i in range(8)]
    assert ready_for_hard_enforcement(p, shadow_metrics(recs)).ok is True


# ── 7. Context Pack integration (#239) ────────────────────────────────


def _pack_with_procedures(procs_matched):
    q = RetrievalQuery(task_id="t-1", role="implementer", repo="org/repo",
                       issue_number=1, issue_title="do the thing",
                       issue_body="## Acceptance criteria\n- [ ] it works\n")
    return plan_pack(q, [IssueProvider(), ProcedureProvider(procs_matched)])


def test_procedures_never_outrank_the_acceptance_criteria():
    """★#240 non-goal, enforced structurally by _TYPE_RANK."""
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    pack = _pack_with_procedures([(p, "prior tasks failed this way")])

    kinds = [a.artifact_type for a in pack.items]
    assert kinds.index(TYPE_AC) < kinds.index(TYPE_PROCEDURE), kinds


def test_procedure_tokens_are_separately_attributable():
    """#240 §4: procedure cost must be visible on its own."""
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    pack = _pack_with_procedures([(p, "matched")])

    cats = pack.tokens_by_category()
    assert cats.get("procedural", 0) > 0
    assert cats.get("mandatory", 0) > 0


def test_pack_states_whether_a_procedure_binds_or_advises():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    block = _pack_with_procedures([(p, "matched")]).to_prompt_block()

    assert "ADVISORY (shadow" in block
    assert "being measured, not enforced" in block


def test_inclusion_reason_reaches_the_prompt():
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    block = _pack_with_procedures([(p, "prior tasks failed with data_loss")]).to_prompt_block()
    assert "prior tasks failed with data_loss" in block


def test_no_procedures_changes_nothing():
    pack = _pack_with_procedures([])
    assert not [a for a in pack.items if a.artifact_type == TYPE_PROCEDURE]


# ── 8. persistence + telemetry ────────────────────────────────────────


def test_procedures_roundtrip_and_latest_state_wins(tmp_path):
    path = str(tmp_path / "procedures.jsonl")
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    append_procedure(path, p)
    deprecate(p, by="op", reason="stale", now=NOW + 5)
    append_procedure(path, p)   # append-only: both lines remain on disk

    loaded = load_procedures(path)
    assert len(loaded) == 1, "same id@version collapses to its latest state"
    assert loaded[0].state == DEPRECATED
    assert loaded[0].scope.repos == ["org/repo"]
    assert loaded[0].evidence[0].ref == "t-1"


def test_load_is_fail_soft():
    assert load_procedures("/definitely/not/here.jsonl") == []


def test_telemetry_is_counts_and_ids_only():
    """⛔No rule prose, and no external economics system named."""
    import json as _json

    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    t = telemetry([(p, "matched")], task_id="t-1", context_id="ctx-1")

    assert t["procedures_matched"] == 1
    assert t["procedure_tokens"] > 0
    assert t["shadow_count"] == 1 and t["hard_count"] == 0
    blob = _json.dumps(t)
    assert "Confirm the primary data layer" not in blob, "rule prose leaked"
    assert "quota" not in blob.lower()


def test_no_quota_core_import():
    import agent_crew.procedural_memory as m

    src = open(m.__file__).read()
    assert "quota_core" not in src and "quota_ops" not in src


# ── 9. the seven fixture cases named in #240 ──────────────────────────


class TestEvaluationFixtures:
    """The concrete cases #240's Evaluation section requires."""

    def test_1_source_of_truth_check_omitted_before_destructive_op(self):
        eps = [_ep("t-1", outcome="failed:data_loss",
                   findings=["deleted before checking the primary layer"]),
               _ep("t-2", outcome="failed:data_loss",
                   findings=["deleted before checking the primary layer"])]
        cands = extract_candidates(eps)
        assert cands and validate_candidate(cands[0]).ok

    def test_2_module_implemented_but_callsite_not_wired(self):
        eps = [_ep("t-1", findings=["helper added but never called"]),
               _ep("t-2", findings=["helper added but never called anywhere"])]
        assert any(c.pattern == "repeated_review_rejection"
                   for c in extract_candidates(eps))

    def test_3_review_without_original_ac_comparison(self):
        eps = [_ep("t-1", findings=["review did not check the acceptance criteria"]),
               _ep("t-2", findings=["review did not check the acceptance criteria"])]
        c = [x for x in extract_candidates(eps)
             if x.pattern == "repeated_review_rejection"][0]
        assert validate_candidate(c).ok

    def test_4_provider_quota_failure_incorrectly_retried(self):
        """⛔Must NOT become a project procedure."""
        eps = [_ep("t-1", outcome="failed:agy_quota_exhausted"),
               _ep("t-2", outcome="failed:agy_quota_exhausted")]
        c = extract_candidates(eps)[0]
        assert not validate_candidate(c).ok

    def test_5_timeout_chosen_without_measurement(self):
        eps = [_ep("t-1", findings=["timeout value has no measurement behind it"]),
               _ep("t-2", findings=["timeout value has no measurement behind it"])]
        assert extract_candidates(eps)

    def test_6_old_procedure_contradicted_by_a_newer_adr(self):
        c = _candidate(prohibited_action="cap the session at 50MB")
        res = validate_candidate(
            c, authoritative_text="ADR-14: we cap the session at 50MB deliberately")
        assert any("contradicts" in r for r in res.reasons)

    def test_7_narrow_incident_must_not_become_a_global_rule(self):
        """★The refusal that keeps this system trustworthy."""
        c = _candidate(scope=Scope(), evidence=[Evidence(kind="task", ref="t-1")])
        res = validate_candidate(c)
        assert not res.ok
        assert any("narrow the scope" in r for r in res.reasons)


def test_procedures_rank_below_non_mandatory_authoritative_types():
    """★What `_TYPE_RANK` actually controls.

    The AC is protected by the *mandatory* flag, not by type rank — mandatory
    items sort first whatever rank they carry. Type rank is what keeps a
    procedure below ordinary authoritative material like current code, and
    that needs its own test or the ordering can regress silently.
    """
    from agent_crew.context_pack import Artifact, RetrievalQuery, plan_pack

    code = Artifact(artifact_id="c1", uri="src/x.py", artifact_type=TYPE_CODE,
                    score=0.1, provenance="lexical", excerpt="code")
    p = promote(_candidate(), approved_by="op", validation=_valid(), now=NOW)
    q = RetrievalQuery(task_id="t", role="implementer", repo="org/repo",
                       issue_number=1, issue_title="t", issue_body="")

    class _S:
        name = "s"

        def retrieve(self, _q):
            return [code]

    pack = plan_pack(q, [_S(), ProcedureProvider([(p, "matched")])])
    kinds = [a.artifact_type for a in pack.items]

    assert kinds.index(TYPE_CODE) < kinds.index(TYPE_PROCEDURE), kinds


# ── 10. the production path (review-2016dcf3) ─────────────────────────
#
# Every test above instantiates ProcedureProvider by hand. The production
# builder never loaded procedures.jsonl, never called match_procedures and
# never registered the provider — so no persisted procedure could reach a
# real dispatch and no shadow telemetry accrued, while the tests all passed.
# Same shape as the #239 defect: coverage of a path production does not take.


def _persist(path, **kw):
    from agent_crew.procedural_memory import append_procedure

    p = promote(_candidate(**kw), approved_by="operator",
                validation=_valid(), now=NOW)
    append_procedure(str(path), p)
    return p


def test_persisted_procedure_reaches_a_production_shaped_pack(tmp_path):
    """★The regression: an active procedure on disk must reach the pack."""
    from agent_crew.context_pack import build_pack_for_task

    procs = tmp_path / "procedures.jsonl"
    _persist(procs, trigger={}, scope=Scope(repos=["org/repo"]))

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs))

    assert any(a.artifact_type == TYPE_PROCEDURE for a in pack.items), \
        "a persisted active procedure never reached the pack"
    assert "check the data layer first" in pack.to_prompt_block()


def test_out_of_scope_procedure_does_not_reach_the_pack(tmp_path):
    """⛔Scope still governs — wiring must not mean 'include everything'."""
    from agent_crew.context_pack import build_pack_for_task

    procs = tmp_path / "procedures.jsonl"
    _persist(procs, trigger={}, scope=Scope(repos=["org/other"]))

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs))

    assert not [a for a in pack.items if a.artifact_type == TYPE_PROCEDURE]


def test_deprecated_procedure_does_not_reach_the_pack(tmp_path):
    from agent_crew.context_pack import build_pack_for_task
    from agent_crew.procedural_memory import append_procedure

    procs = tmp_path / "procedures.jsonl"
    p = _persist(procs, trigger={}, scope=Scope(repos=["org/repo"]))
    deprecate(p, by="op", reason="superseded", now=NOW)
    append_procedure(str(procs), p)

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs))

    assert not [a for a in pack.items if a.artifact_type == TYPE_PROCEDURE]


def test_episode_evidence_drives_the_trigger(tmp_path):
    """★A triggered procedure fires on evidence, not on a guess."""
    from agent_crew.context_pack import append_episode, build_pack_for_task

    procs = tmp_path / "procedures.jsonl"
    eps = tmp_path / "episodes.jsonl"
    _persist(procs, trigger={"outcome_signature": "failed:data_loss"},
             scope=Scope(repos=["org/repo"]))
    append_episode(str(eps), {"task_id": "t-prev", "issue": 42,
                              "outcome": "failed:data_loss", "findings": []})

    with_ev = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs),
        episodes_path=str(eps))
    assert [a for a in with_ev.items if a.artifact_type == TYPE_PROCEDURE]

    # Same procedure, no prior failure on record -> trigger does not fire.
    without = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-2", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs))
    assert not [a for a in without.items if a.artifact_type == TYPE_PROCEDURE]


def test_shadow_telemetry_actually_accrues(tmp_path):
    """★#240 §5: without accrual there is never evidence for hard mode."""
    import json as _json

    from agent_crew.context_pack import build_pack_for_task

    procs = tmp_path / "procedures.jsonl"
    shadow = tmp_path / "procedure_shadow.jsonl"
    _persist(procs, trigger={}, scope=Scope(repos=["org/repo"]))

    build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs),
        shadow_path=str(shadow))

    recs = [_json.loads(l) for l in open(shadow)]
    assert len(recs) == 1
    assert recs[0]["task_id"] == "t-1"
    assert recs[0]["triggered"] is True
    assert recs[0]["enforcement"] == SHADOW
    assert recs[0]["reason"]


def test_a_corrupt_procedures_file_does_not_break_dispatch(tmp_path):
    """⛔Derived governance may never take a dispatch down."""
    from agent_crew.context_pack import build_pack_for_task

    procs = tmp_path / "procedures.jsonl"
    procs.write_text("{not json at all\n")

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "t", "issue_body": "b"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), procedures_path=str(procs))

    assert isinstance(pack.pack_id, str)
    assert not [a for a in pack.items if a.artifact_type == TYPE_PROCEDURE]


def test_enabled_dispatcher_puts_a_persisted_procedure_into_the_prompt(
    tmp_path, monkeypatch,
):
    """★★The dispatch-level regression the review asked for.

    Drives the real `_dispatch_task` with the pack ENABLED and a procedure on
    disk, then inspects the command that would be spawned. Asserts inside the
    CONTEXT PACK block — a looser check over the whole command would match
    text arriving by other routes, which is how the #239 version of this test
    initially passed against a broken build.
    """
    import asyncio
    import json as _json

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")
    _persist(tmp_path / "procedures.jsonl", trigger={},
             scope=Scope(repos=["org/repo"]))

    spawned = {}

    async def _fake_exec(*cmd, **kwargs):
        spawned["cmd"] = cmd

        class _P:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return _P()

    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec",
                        _fake_exec)

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state_file),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(
            task_id="disp-p1", task_type="implement", description="do it",
            branch="main",
            context={"issue": 42, "repo": "org/repo", "issue_title": "t",
                     "issue_body": "## Acceptance criteria\n- [ ] works\n"},
        ))
        task = q.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    blob = " ".join(str(c) for c in spawned.get("cmd", ()))
    start, end = blob.index("=== CONTEXT PACK"), blob.index("=== END CONTEXT PACK")
    pack_block = blob[start:end]

    assert "procedure" in pack_block, \
        f"no procedure artifact in the dispatched pack:\n{pack_block[:600]}"
    assert "check the data layer first" in pack_block
    # ...and shadow telemetry accrued from the real dispatch.
    shadow = tmp_path / "procedure_shadow.jsonl"
    assert shadow.exists() and shadow.read_text().strip()
