"""P1 (owner alfred#51 5804834008, D-11567) — scope_anchors normalisation.

Observed live on :8105 before this: every organic task carried
``scope_anchors=()`` and ``repo=""``, so the P4 identity of two unrelated
tasks on one branch was byte-identical and the second was refused
``DUPLICATE_INTENT`` / ``ALREADY_COMPLETED``. Empty anchors are not a neutral
default — they are an assertion that two tasks are the same work, made on the
evidence of two empty fields.

These tests pin the derivation order and, just as importantly, the thing the
derivation must **not** break: work a caller declared identically still
collapses into one intent (P4; permanent fixture CX-4c).
"""
from __future__ import annotations

import pytest

from agent_crew.cea.engine import intent_hash
from agent_crew.protocol import TaskRequest
from agent_crew.queue import intent_for_task


def task(task_id="t1", *, task_type="implement", context=None, project="agent_crew",
         description="add a --json flag", branch="main", pr_number=None) -> TaskRequest:
    return TaskRequest(task_id=task_id, task_type=task_type, description=description,
                       branch=branch, priority=3, context=dict(context or {}),
                       project=project, pr_number=pr_number)


def anchors(*args, **kwargs) -> tuple[str, ...]:
    return intent_for_task(task(*args, **kwargs)).identity.target.scope_anchors


def hashed(*args, **kwargs) -> str:
    return intent_hash(intent_for_task(task(*args, **kwargs)).identity)


# ═══════════════════════════════════════════════════════════════════════════
# the incident: two organic tasks on one branch are two intents
# ═══════════════════════════════════════════════════════════════════════════

def test_two_organic_tasks_on_one_branch_are_two_intents():
    """The live failure. Same project, same branch, nothing declared — and
    before this they hashed the same, so the second was a DUPLICATE_INTENT."""
    assert hashed("impl-1a2b3c4d") != hashed("impl-5e6f7a8b")


def test_the_same_task_resubmitted_twice_is_one_intent():
    """P4 replay still works: a re-submission of one task_id must not open a
    second lineage just because the anchor is now derived."""
    assert hashed("impl-1a2b3c4d") == hashed("impl-1a2b3c4d")


def test_an_organic_task_anchors_on_its_own_id():
    assert anchors("impl-1a2b3c4d") == ("task://impl-1a2b3c4d",)


def test_rewording_an_organic_task_does_not_change_its_intent():
    """P4: the description is not an identity member, and deriving an anchor
    from the id must not smuggle the text in behind it."""
    assert (hashed("impl-1a2b3c4d", description="add a --json flag")
            == hashed("impl-1a2b3c4d", description="ADD A JSON FLAG, please"))


# ═══════════════════════════════════════════════════════════════════════════
# the derivation order
# ═══════════════════════════════════════════════════════════════════════════

def test_explicit_anchors_win_over_every_other_source():
    declared = {"scope_anchors": ["src/agent_crew/queue.py"],
                "paths": ["src/other.py"], "pr_number": 51, "repo": "example/agent_crew"}
    assert anchors("t-explicit", context=declared) == ("src/agent_crew/queue.py",)


def test_declared_paths_are_used_when_no_anchors_were_given():
    assert anchors("t-paths", context={"paths": ["src/b.py", "src/a.py"]}) == (
        "src/a.py", "src/b.py")


@pytest.mark.parametrize("key", ["artifacts", "changed_paths", "paths", "files", "touches"])
def test_every_declared_path_key_anchors(key):
    """The same keys the deterministic risk classifier reads. A task cannot be
    about ``src/x.py`` for tiering and about nothing for identity."""
    assert anchors("t-key", context={key: ["src/x.py"]}) == ("src/x.py",)


def test_a_pr_task_anchors_on_the_pr():
    ctx = {"pr_number": 51, "repo": "example/agent_crew"}
    assert anchors("review-aa", context=ctx) == ("pr://example/agent_crew/51",)


def test_two_tasks_about_one_pr_stay_one_intent():
    """The dedup that must survive: a re-dispatched review of PR #51 is the
    same work, however the dispatcher named the task."""
    ctx = {"pr_number": 51, "repo": "example/agent_crew"}
    assert hashed("review-aa", context=ctx) == hashed("review-bb", context=ctx)


def test_a_pr_number_is_read_the_way_agents_write_it():
    ctx = {"repo": "example/agent_crew"}
    assert (anchors("r1", context=dict(ctx, pr_number="#51"))
            == anchors("r2", context=dict(ctx, pr_number=51))
            == ("pr://example/agent_crew/51",))


def test_a_pr_number_on_the_task_itself_anchors_too():
    assert anchors("r1", context={"repo": "example/agent_crew"}, pr_number=51) == (
        "pr://example/agent_crew/51",)


def test_anchors_beat_the_pr_and_paths_beat_the_pr():
    ctx = {"pr_number": 51, "repo": "example/agent_crew"}
    assert anchors("r", context=dict(ctx, paths=["src/a.py"])) == ("src/a.py",)


# ═══════════════════════════════════════════════════════════════════════════
# what the derivation must not break — P4 / CX-4c
# ═══════════════════════════════════════════════════════════════════════════

def test_work_declared_identically_still_collapses_under_a_new_task_id():
    """CX-4c, the incident's own permanent fixture: the same declared work
    re-sent under a new id and new wording is still one intent. The task id is
    an anchor of last resort *only* where nothing was declared to compare."""
    ctx = {"authority_decision_ids": ["T0-1234"], "repo": "example/agent_crew"}
    assert (hashed("c1", context=ctx, description="first wording")
            == hashed("c2", context=ctx, description="totally reworded"))


def test_a_declared_repo_alone_leaves_the_anchors_empty():
    assert anchors("c1", context={"repo": "example/agent_crew"}) == ()


def test_declaring_the_same_paths_collapses_two_task_ids():
    ctx = {"paths": ["src/agent_crew/queue.py"]}
    assert hashed("impl-aa", context=ctx) == hashed("impl-bb", context=ctx)


# ═══════════════════════════════════════════════════════════════════════════
# canonicalisation and repo back-fill
# ═══════════════════════════════════════════════════════════════════════════

def test_derived_anchors_go_through_the_existing_canonicaliser():
    """One spelling per target, so one target is one lineage — the same rule
    :func:`canonical_scope_anchor` already applies to declared anchors."""
    assert (anchors("t1", context={"scope_anchors": ["src/./x.py", "src/x.py"]})
            == anchors("t2", context={"scope_anchors": ["src/x.py"]})
            == ("src/x.py",))


def test_an_unspellable_explicit_anchor_reaches_the_engine_unchanged():
    """P7/P2: admission refuses it with a receipt. Raising in intent
    construction would lose the audit row that must exist either way."""
    assert anchors("t-bad", context={"scope_anchors": ["../escape"]}) == ("../escape",)


def test_an_unspellable_declared_path_does_not_block_the_task():
    """A path key is routing metadata, not a declaration of scope the caller
    chose to make — it falls through to the id rather than poisoning admission."""
    assert anchors("impl-cc", context={"paths": ["../escape"]}) == ("task://impl-cc",)


def test_the_repo_comes_from_the_context_then_the_project_then_the_queue():
    assert intent_for_task(task(context={"repo": "example/x"})).identity.target.repo == "example/x"
    assert intent_for_task(task(project="example/y")).identity.target.repo == "example/y"
    assert intent_for_task(task(project=""),
                           queue_identity="agent_crew").identity.target.repo == "agent_crew"


def test_the_repo_is_no_longer_empty_for_an_organic_task():
    """The other half of the live observation: `repo=""` on every organic task."""
    assert intent_for_task(task()).identity.target.repo == "agent_crew"
