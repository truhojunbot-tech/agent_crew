"""#239 — task-scoped Context Pack retrieval.

The pack exists to replace "keep the whole conversation" with "assemble the
relevant durable artifacts". That only works if a few properties hold hard:

  * the acceptance criteria can never be squeezed out by budget pressure —
    a pack that looks complete while omitting the bar is worse than none;
  * authoritative material outranks episodic recollection, always;
  * conflicting artifacts are surfaced rather than silently resolved;
  * a retrieval failure is visible degradation, never an empty pack that
    passes for a successful one;
  * the same inputs always produce the same pack, so a later semantic mode
    can be *measured* against this baseline instead of assumed better.
"""

import json

import pytest

from agent_crew.context_pack import (
    CONTEXT_PACK_SCHEMA_VERSION,
    MODE_LEXICAL,
    STALE,
    TYPE_AC,
    TYPE_ADR,
    TYPE_CODE,
    TYPE_EPISODE,
    TYPE_ISSUE,
    TYPE_TEST,
    Artifact,
    ContextPack,
    EpisodicProvider,
    IssueProvider,
    LexicalRepoProvider,
    RetrievalProvider,
    RetrievalQuery,
    append_episode,
    budget_for,
    build_episode,
    detect_conflicts,
    estimate_tokens,
    keywords_from,
    load_episodes,
    plan_pack,
)

ISSUE_BODY = """## Why
The dispatcher drops the widget.

## Acceptance criteria
- [ ] widget survives a restart
- [ ] no duplicate widgets
"""


def _query(**kw):
    base = dict(task_id="t-1", task_type="implement", role="implementer",
                repo="org/repo", issue_number=42, issue_title="fix widget drop",
                issue_body=ISSUE_BODY)
    base.update(kw)
    return RetrievalQuery(**base)


class _Static(RetrievalProvider):
    name = "static"

    def __init__(self, artifacts):
        self._a = artifacts

    def retrieve(self, query):
        return list(self._a)


class _Broken(RetrievalProvider):
    name = "broken"

    def retrieve(self, query):
        raise RuntimeError("index unavailable")


def _code(i, tokens=50, atype=TYPE_CODE, rev="abc123", score=1.0):
    return Artifact(artifact_id=f"c{i}", uri=f"src/mod{i}.py", artifact_type=atype,
                    revision=rev, score=score, provenance="lexical",
                    excerpt="x" * (tokens * 4))


# ── 1. mandatory issue + AC ───────────────────────────────────────────


def test_issue_and_ac_are_separate_artifacts():
    """★The AC must be independently protectable from a long issue body."""
    got = IssueProvider().retrieve(_query())
    types = [a.artifact_type for a in got]

    assert TYPE_ISSUE in types and TYPE_AC in types
    ac = next(a for a in got if a.artifact_type == TYPE_AC)
    assert "widget survives a restart" in ac.excerpt
    assert "no duplicate widgets" in ac.excerpt


def test_issue_excerpt_does_not_duplicate_the_ac():
    """⛔Duplicated tokens are exactly what the pack exists to remove."""
    got = IssueProvider().retrieve(_query())
    issue = next(a for a in got if a.artifact_type == TYPE_ISSUE)

    assert "widget survives a restart" not in issue.excerpt
    assert "dispatcher drops the widget" in issue.excerpt


def test_issue_artifacts_carry_a_resolvable_uri():
    """Every item must resolve back to a durable artifact."""
    for a in IssueProvider().retrieve(_query()):
        assert a.uri.startswith("https://github.com/org/repo/issues/42")
        assert a.provenance


def test_ac_survives_a_budget_far_too_small():
    """★The load-bearing rule: budget pressure never drops the AC."""
    bulky = [_code(i, tokens=500) for i in range(20)]
    pack = plan_pack(_query(), [IssueProvider(), _Static(bulky)],
                     budget={"max_tokens": 10, "max_items": 1, "type_caps": {}})

    kinds = [a.artifact_type for a in pack.items]
    assert TYPE_AC in kinds, kinds
    assert TYPE_ISSUE in kinds, kinds
    assert pack.total_tokens > 10, "mandatory items are allowed to exceed budget"


def test_a_body_without_ac_still_yields_the_issue():
    got = IssueProvider().retrieve(_query(issue_body="just prose, no criteria"))
    assert [a.artifact_type for a in got] == [TYPE_ISSUE]


def test_no_issue_content_yields_nothing():
    assert IssueProvider().retrieve(_query(issue_body="", issue_title="")) == []


# ── 2. ordering: authoritative outranks episodic ──────────────────────


def test_authoritative_outranks_episodic_even_at_lower_score():
    """★'ADR/spec decisions outrank provider conversation recollection.'"""
    adr = _code(1, atype=TYPE_ADR, score=0.1)
    ep = Artifact(artifact_id="e1", uri="episode://x", artifact_type=TYPE_EPISODE,
                  score=99.0, provenance="episode", excerpt="we tried X")
    pack = plan_pack(_query(), [_Static([ep, adr])])

    kinds = [a.artifact_type for a in pack.items]
    assert kinds.index(TYPE_ADR) < kinds.index(TYPE_EPISODE), kinds


def test_pack_is_deterministic_regardless_of_provider_order():
    """★Without this a semantic mode cannot be measured against the baseline."""
    arts = [_code(i, score=float(i % 3)) for i in range(8)]
    a = plan_pack(_query(), [IssueProvider(), _Static(arts)])
    b = plan_pack(_query(), [_Static(list(reversed(arts))), IssueProvider()])

    assert [x.artifact_id for x in a.items] == [x.artifact_id for x in b.items]
    assert a.pack_hash == b.pack_hash


def test_duplicate_artifacts_are_collapsed():
    dup = _code(1)
    pack = plan_pack(_query(), [_Static([dup, _code(1)])])
    assert len([a for a in pack.items if a.artifact_id == "c1"]) == 1


# ── 3. budget + role composition ──────────────────────────────────────


def test_role_changes_pack_composition_deterministically():
    """Reviewer leans on review/code; tester on tests."""
    assert budget_for("reviewer")["type_caps"][TYPE_EPISODE] == 2
    assert budget_for("tester")["type_caps"][TYPE_TEST] == 8
    assert budget_for("implementer")["max_tokens"] > budget_for("tester")["max_tokens"]


def test_unknown_role_gets_the_conservative_default():
    b = budget_for("archaeologist")
    assert b["max_tokens"] == 4000 and b["max_items"] == 16


def test_type_caps_stop_one_category_crowding_out_the_rest():
    """⛔20 episodes must not consume a pack that also needs code."""
    eps = [Artifact(artifact_id=f"e{i}", uri=f"episode://{i}",
                    artifact_type=TYPE_EPISODE, score=50.0,
                    provenance="ep", excerpt="y" * 40) for i in range(20)]
    code = [_code(i, tokens=10) for i in range(5)]
    pack = plan_pack(_query(), [_Static(eps + code)],
                     budget=budget_for("implementer"))

    n_ep = sum(1 for a in pack.items if a.artifact_type == TYPE_EPISODE)
    n_code = sum(1 for a in pack.items if a.artifact_type == TYPE_CODE)
    assert n_ep <= 4, n_ep
    assert n_code > 0, "code was crowded out by episodes"


def test_token_budget_is_respected_for_non_mandatory_items():
    big = [_code(i, tokens=200) for i in range(20)]
    pack = plan_pack(_query(issue_body="", issue_title=""), [_Static(big)],
                     budget={"max_tokens": 500, "max_items": 50, "type_caps": {}})
    assert pack.total_tokens <= 500


# ── 4. conflicts are surfaced, not merged ─────────────────────────────


def test_same_subject_at_two_revisions_is_a_conflict():
    """★A stale doc must not quietly override current code."""
    a = _code(1, rev="old111")
    b = _code(1, rev="new222")
    b.artifact_id = "c1-dup"
    conflicts = detect_conflicts([a, b])

    assert len(conflicts) == 1
    assert "src/mod1.py" in conflicts[0]
    assert "old111" in conflicts[0] and "new222" in conflicts[0]


def test_conflicts_reach_the_prompt():
    a, b = _code(1, rev="r1"), _code(1, rev="r2")
    b.artifact_id = "c1b"
    pack = plan_pack(_query(), [_Static([a, b])])

    assert len(pack.conflicts) == 1
    block = pack.to_prompt_block()
    assert "CONFLICTING ARTIFACTS" in block
    assert "do not average them" in block


def test_identical_revisions_are_not_a_conflict():
    a, b = _code(1, rev="same"), _code(1, rev="same")
    b.artifact_id = "c1b"
    assert detect_conflicts([a, b]) == []


def test_stale_items_are_counted_and_marked_in_the_prompt():
    s = _code(1)
    s.freshness, s.stale_reason = STALE, "predates the current branch"
    pack = plan_pack(_query(), [_Static([s])])

    assert pack.stale_count == 1
    assert "STALE" in pack.to_prompt_block()
    assert "predates the current branch" in pack.to_prompt_block()


# ── 5. degradation is never a silent empty pack ───────────────────────


def test_provider_failure_marks_the_pack_degraded():
    """★An empty pack must never masquerade as a successful one."""
    pack = plan_pack(_query(), [_Broken()])

    assert pack.degraded is True
    assert "index unavailable" in pack.degraded_reason
    assert pack.provider_errors


def test_degradation_is_stated_in_the_prompt():
    pack = plan_pack(_query(), [_Broken()])
    block = pack.to_prompt_block()

    assert "DEGRADED" in block
    assert "absence of an artifact as unknown" in block


def test_one_failing_provider_does_not_lose_the_others():
    pack = plan_pack(_query(), [_Broken(), IssueProvider()])

    assert pack.degraded is True
    assert any(a.artifact_type == TYPE_AC for a in pack.items)


def test_retrieval_timeout_is_reported_as_degradation():
    class _Slow(RetrievalProvider):
        name = "slow"

        def retrieve(self, q):
            import time as _t
            _t.sleep(0.05)
            return []

    pack = plan_pack(_query(), [_Slow(), _Slow(), _Slow()], timeout_s=0.01)
    assert pack.degraded is True
    assert "timeout" in pack.degraded_reason


def test_healthy_empty_result_is_not_degraded():
    """⛔'Nothing matched' and 'retrieval broke' must stay distinguishable."""
    pack = plan_pack(_query(issue_body="", issue_title=""), [_Static([])])
    assert pack.degraded is False
    assert pack.selected_count == 0


# ── 6. retry/recovery carries prior failure evidence ──────────────────


EPISODE = {
    "task_id": "t-prev", "issue": 42, "role": "implementer", "agent": "claude",
    "outcome": "failed:transient_agy_subscriber_lag_max_retries",
    "summary": "tried the naive fix; tester never ran",
    "findings": ["reviewer: the guard is not atomic"],
    "branch": "agent/x", "pr_number": 7,
}


def test_retry_surfaces_the_prior_attempt_first_among_episodes():
    """★A second attempt's most useful input is the first one's exact failure."""
    other = dict(EPISODE, task_id="t-other", summary="unrelated earlier work")
    pack = plan_pack(_query(retry_of="t-prev"),
                     [EpisodicProvider([other, EPISODE])])

    eps = [a for a in pack.items if a.artifact_type == TYPE_EPISODE]
    assert eps[0].artifact_id == "episode:t-prev", [e.artifact_id for e in eps]
    assert "prior attempt on this task" in eps[0].provenance
    assert "guard is not atomic" in eps[0].excerpt


def test_episodes_from_other_issues_are_excluded():
    foreign = dict(EPISODE, task_id="t-foreign", issue=999)
    pack = plan_pack(_query(), [EpisodicProvider([foreign])])
    assert not [a for a in pack.items if a.artifact_type == TYPE_EPISODE]


def test_episode_records_outcome_and_references_not_transcripts():
    """⛔Privacy-safe: references and metadata, never raw prompt/source."""
    attribution = {"task_id": "t-9", "context_id": "ctx-9", "context_generation": 3,
                   "role": "implementer", "agent": "claude", "git_branch": "b",
                   "outcome": "completed", "started_at": 1.0, "completed_at": 2.0}
    ep = build_episode(attribution, {"summary": "did it", "findings": ["f1"],
                                     "pr_number": 12}, issue=42)

    assert ep["task_id"] == "t-9" and ep["issue"] == 42
    assert ep["context_id"] == "ctx-9" and ep["context_generation"] == 3
    assert ep["outcome"] == "completed" and ep["pr_number"] == 12
    assert ep["schema_version"] == CONTEXT_PACK_SCHEMA_VERSION
    # no free-form content beyond bounded summary/findings
    assert set(ep) & {"prompt", "source", "diff", "log"} == set()


def test_episode_roundtrips_through_the_jsonl(tmp_path):
    p = tmp_path / "episodes.jsonl"
    append_episode(str(p), EPISODE)
    append_episode(str(p), dict(EPISODE, task_id="t-2"))

    got = load_episodes(str(p))
    assert [e["task_id"] for e in got] == ["t-prev", "t-2"]


def test_episode_append_never_raises(tmp_path):
    append_episode(str(tmp_path / "nested" / "deep" / "e.jsonl"), EPISODE)
    assert load_episodes("/definitely/not/here.jsonl") == []


# ── 7. telemetry contract ─────────────────────────────────────────────


def test_telemetry_is_counts_and_ids_only():
    """⛔No content may leak into telemetry, and no external system is named."""
    pack = plan_pack(_query(), [IssueProvider()])
    t = pack.telemetry()

    for key in ("context_pack_id", "context_pack_hash", "mode", "role",
                "candidate_count", "selected_count", "total_tokens",
                "tokens_by_category", "stale_count", "conflict_count",
                "latency_ms", "degraded", "degraded_reason"):
        assert key in t, key
    blob = json.dumps(t)
    assert "widget survives a restart" not in blob, "excerpt leaked into telemetry"
    assert "quota" not in blob.lower()


def test_tokens_are_split_by_category():
    ep = Artifact(artifact_id="e", uri="episode://e", artifact_type=TYPE_EPISODE,
                  provenance="ep", excerpt="z" * 40)
    pack = plan_pack(_query(), [IssueProvider(), _Static([_code(1), ep])])
    cats = pack.tokens_by_category()

    assert cats["mandatory"] > 0
    assert cats["authoritative"] > 0
    assert cats["episodic"] > 0


def test_pack_hash_changes_with_content_and_is_stable_otherwise():
    a = plan_pack(_query(), [IssueProvider()])
    b = plan_pack(_query(), [IssueProvider()])
    c = plan_pack(_query(), [IssueProvider(), _Static([_code(1)])])

    assert a.pack_hash == b.pack_hash
    assert a.pack_hash != c.pack_hash
    assert a.pack_id.startswith(f"cp{CONTEXT_PACK_SCHEMA_VERSION}-")


def test_no_quota_core_import():
    """#239 non-goal: Agent Crew must not depend on the economics consumer."""
    import agent_crew.context_pack as m

    src = open(m.__file__).read()
    assert "quota_core" not in src and "quota_ops" not in src


# ── 8. lexical baseline works without embeddings ──────────────────────


def test_lexical_provider_is_deterministic_and_typed(tmp_path):
    hits = {"widget": ["src/widget.py", "tests/test_widget.py", "docs/adr/0003-widget.md"]}

    def runner(repo_path, term):
        return hits.get(term, [])

    p = LexicalRepoProvider(runner=runner)
    got = p.retrieve(_query(repo_path=str(tmp_path), keywords=["widget"]))
    by_uri = {a.uri: a for a in got}

    assert by_uri["src/widget.py"].artifact_type == TYPE_CODE
    assert by_uri["tests/test_widget.py"].artifact_type == TYPE_TEST
    assert by_uri["docs/adr/0003-widget.md"].artifact_type == TYPE_ADR
    # score is an explained sum, never a magic number
    assert by_uri["docs/adr/0003-widget.md"].score_components["type_weight"] == 1.5


def test_lexical_provider_needs_no_embeddings_and_survives_a_broken_repo():
    p = LexicalRepoProvider()
    assert p.mode == MODE_LEXICAL
    assert p.retrieve(_query(repo_path="/definitely/not/a/repo",
                             keywords=["widget"])) == []


def test_keywords_prefer_identifiers_over_prose():
    kw = keywords_from("fix widget_drop in dispatcher",
                       "the _cap_gemini_session_size guard is wrong")
    assert "widget_drop" in kw
    assert "_cap_gemini_session_size" in kw
    assert "about" not in kw


def test_keywords_are_stable():
    a = keywords_from("fix widget_drop", "touching handler_alpha and handler_beta")
    b = keywords_from("fix widget_drop", "touching handler_alpha and handler_beta")
    assert a == b


# ── 9. provider contract admits semantic without dispatcher change ────


def test_a_semantic_provider_plugs_in_unchanged():
    """★#239 scope 3: adding semantic retrieval must not change the contract."""
    class _Semantic(RetrievalProvider):
        name, version, mode = "semantic_stub", 1, "semantic"

        def retrieve(self, q):
            return [_code(99, atype=TYPE_ADR, score=5.0)]

    pack = plan_pack(_query(), [IssueProvider(), _Semantic()], mode="semantic")

    assert pack.mode == "semantic"
    assert any(a.artifact_id == "c99" for a in pack.items)
    assert pack.telemetry()["mode"] == "semantic"


def test_estimate_tokens_is_monotonic_and_never_zero():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_empty_pack_renders_nothing():
    pack = ContextPack(task_id="t", role="implementer", mode=MODE_LEXICAL)
    assert pack.to_prompt_block() == ""


# ── 10. dispatcher integration ────────────────────────────────────────


def test_pack_is_off_by_default(monkeypatch):
    """⛔Opt-in: existing prompt composition must be untouched until an
    operator enables it and the benchmark says it helps."""
    from agent_crew import context_pack as m

    monkeypatch.delenv("AGENT_CREW_CONTEXT_PACK", raising=False)
    assert m.enabled() is False
    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "1")
    assert m.enabled() is True


def test_builder_never_raises_on_a_broken_issue_fetch(tmp_path):
    """A GitHub hiccup must degrade the pack, not the dispatch."""
    from agent_crew.context_pack import build_pack_for_task

    def boom(repo, issue):
        raise RuntimeError("gh down")

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "fix widget_drop"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path), issue_body_fn=boom)

    assert isinstance(pack.pack_id, str)
    # title alone still yields the mandatory issue artifact
    assert any(a.artifact_type == TYPE_ISSUE for a in pack.items)


def test_builder_includes_prior_episode_on_retry(tmp_path):
    from agent_crew.context_pack import append_episode, build_pack_for_task

    eps = tmp_path / "episodes.jsonl"
    append_episode(str(eps), EPISODE)

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "fix widget_drop",
         "retry_of": "t-prev"},
        task_id="t-2", task_type="implement", role="implementer",
        repo_path=str(tmp_path), episodes_path=str(eps),
        issue_body_fn=lambda r, i: ISSUE_BODY)

    eps_items = [a for a in pack.items if a.artifact_type == TYPE_EPISODE]
    assert eps_items and "prior attempt on this task" in eps_items[0].provenance
    # ...and the AC is still present alongside it
    assert any(a.artifact_type == TYPE_AC for a in pack.items)


def test_builder_with_no_issue_still_produces_a_usable_pack(tmp_path):
    """`crew run` tasks have no issue number; the pack must degrade to
    lexical-only rather than failing."""
    from agent_crew.context_pack import build_pack_for_task

    pack = build_pack_for_task(
        {"issue_title": "refactor the widget"},
        task_id="t-3", task_type="implement", role="implementer",
        repo_path=str(tmp_path))

    assert pack.degraded is False
    assert isinstance(pack.to_prompt_block(), str)


# ── 11. the production call site (review of PR #241) ──────────────────
#
# The original tests all passed `issue_body_fn=lambda ...` — something the
# server NEVER does. So every unit test saw an acceptance-criteria artifact
# while every real dispatch shipped a pack without one, and the pack called
# itself healthy while doing it. These tests exercise the call the server
# actually makes.


def test_production_shaped_call_without_a_body_fn_still_gets_the_ac(tmp_path):
    """★The exact regression: no `issue_body_fn`, body persisted at ingest."""
    from agent_crew.context_pack import build_pack_for_task

    pack = build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "fix widget_drop",
         "issue_body": ISSUE_BODY},          # what watch.py now stores
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path))             # NO issue_body_fn — as in server.py

    assert any(a.artifact_type == TYPE_AC for a in pack.items), \
        "the acceptance criteria must survive the production call shape"
    assert "widget survives a restart" in pack.to_prompt_block()
    assert pack.degraded is False


def test_missing_body_for_a_real_issue_is_visible_degradation(tmp_path, monkeypatch):
    """★A pack without the AC must never report itself healthy."""
    import agent_crew.context_pack as m

    monkeypatch.setattr(m, "fetch_issue_body", lambda *a, **k: "")
    pack = m.build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "fix widget_drop"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path))

    assert not any(a.artifact_type == TYPE_AC for a in pack.items)
    assert pack.degraded is True
    assert "acceptance criteria are NOT in this pack" in pack.degraded_reason
    assert "DEGRADED" in pack.to_prompt_block()


def test_github_fallback_supplies_the_body_when_ingest_did_not(tmp_path, monkeypatch):
    """Manual enqueues never went through the watcher, so they have no
    stored body — a bounded lookup covers them."""
    import agent_crew.context_pack as m

    monkeypatch.setattr(m, "fetch_issue_body", lambda repo, issue, **k: ISSUE_BODY)
    pack = m.build_pack_for_task(
        {"issue": 42, "repo": "org/repo", "issue_title": "fix widget_drop"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path))

    assert any(a.artifact_type == TYPE_AC for a in pack.items)
    assert pack.degraded is False


def test_a_task_with_no_issue_is_not_degraded(tmp_path, monkeypatch):
    """⛔`crew run` tasks legitimately have no issue. That is not a failure,
    and must stay distinguishable from 'issue whose body we could not read'."""
    import agent_crew.context_pack as m

    monkeypatch.setattr(m, "fetch_issue_body", lambda *a, **k: "")
    pack = m.build_pack_for_task(
        {"issue_title": "refactor the widget"},
        task_id="t-1", task_type="implement", role="implementer",
        repo_path=str(tmp_path))

    assert pack.degraded is False


def test_body_resolution_order_and_source():
    from agent_crew.context_pack import resolve_issue_body

    assert resolve_issue_body({"issue_body": "stored"}) == ("stored", "ingest")
    assert resolve_issue_body(
        {"issue": 1, "repo": "o/r"},
        issue_body_fn=lambda r, i: "injected") == ("injected", "injected")
    assert resolve_issue_body(
        {"issue": 1}, issue_body_fn=lambda r, i: "") == ("", "lookup_failed")
    assert resolve_issue_body({}) == ("", "no_issue")


def test_body_fetch_failure_does_not_raise(monkeypatch):
    import agent_crew.context_pack as m

    def boom(*a, **k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(m.subprocess, "run", boom)
    assert m.fetch_issue_body("o/r", 1) == ""


def test_watch_persists_the_issue_body_it_already_fetched():
    """★Fix at the source: the watcher fetched the body and threw it away."""
    from agent_crew.watch import ISSUE_BODY_MAX_CHARS, build_task

    issue = {"number": 42, "title": "fix widget_drop", "labels": ["bug"],
             "body": ISSUE_BODY, "url": "https://github.com/org/repo/issues/42"}
    task = build_task(issue, "org/repo", "main")

    assert task.context["issue_body"] == ISSUE_BODY
    # ...and one enormous issue cannot bloat the queue row.
    huge = dict(issue, body="x" * (ISSUE_BODY_MAX_CHARS + 5000))
    assert len(build_task(huge, "org/repo", "main").context["issue_body"]) \
        == ISSUE_BODY_MAX_CHARS


# ── 12. ENABLED dispatcher-level integration (review of PR #241) ──────


def test_enabled_dispatcher_puts_the_ac_into_the_real_dispatched_prompt(
    tmp_path, monkeypatch,
):
    """★★The test whose absence let this ship broken.

    Drives the actual `_dispatch_task` path with the pack ENABLED and captures
    the command the dispatcher would spawn. Nothing is stubbed between the
    task context and the prompt, so a call site that forgets to supply the
    issue body fails here — which is exactly what happened on PR #241 while
    every unit test passed.
    """
    import asyncio
    import json as _json

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")

    spawned = {}

    async def _fake_exec(*cmd, **kwargs):
        spawned["cmd"] = cmd

        class _P:
            returncode = 0
            pid = 1234

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

    from fastapi.testclient import TestClient

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state_file),
                     watchdog_disabled=True, anomaly_disabled=True)

    # The queue is bound during lifespan startup, so the dispatch has to run
    # with the app actually started — same shape as production.
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(
            task_id="disp-1", task_type="implement", description="fix the widget",
            branch="main",
            context={"issue": 42, "repo": "org/repo",
                     "issue_title": "fix widget_drop",
                     "issue_body": ISSUE_BODY,    # as watch.py now persists it
                     "source": "watch"},
        ))
        task = q.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    blob = " ".join(str(c) for c in spawned.get("cmd", ()))
    assert "CONTEXT PACK" in blob, "the pack never reached the dispatched prompt"

    # ⛔Assert against the PACK BLOCK, not the whole command. `_format_task_message`
    #   serialises task.context — which now holds issue_body — into the prompt,
    #   so a naive substring check over the command passes even when the pack
    #   omits the AC entirely. That looseness is what let the original bug hide.
    start = blob.index("=== CONTEXT PACK")
    end = blob.index("=== END CONTEXT PACK")
    pack_block = blob[start:end]

    assert "acceptance_criteria" in pack_block, \
        f"no acceptance-criteria artifact in the dispatched pack:\n{pack_block[:600]}"
    assert "widget survives a restart" in pack_block, \
        "the acceptance criteria are missing from the dispatched Context Pack"
    assert "no duplicate widgets" in pack_block
    assert "DEGRADED" not in pack_block
