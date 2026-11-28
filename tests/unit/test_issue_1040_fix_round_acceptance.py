"""#1040 — a fix round carries its root task's acceptance and test gate.

``auto_enqueue_fix`` built the fix description from the reviewer summary and
capped findings only, so the root implement task's acceptance criteria and
full-suite gate never reached fix rounds (athena: fix descriptions ~1.4k chars
against parents of up to 7k; 15 of 30 request_changes rounds traced to this).
"""

import logging
import uuid

import pytest

pytestmark = pytest.mark.usefixtures("pinned_task_transport")

from agent_crew.pipeline import (
    MAX_FINDING_CHARS,
    auto_enqueue_fix,
    extract_acceptance_sections,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

BRANCH = "agent/claude/1040-x"
HEADING = "Parent acceptance and test gate (verbatim from impl-1040):"
SUITE = ("python3 -m pytest -q --timeout=180 -p no:cacheprovider "
         "--numprocesses=8 tests .")
ACCEPTANCE = (
    "Acceptance (all required):\n"
    "A. Unit tests in tests/unit/test_x.py:\n"
    "   - case one;\n"
    "   - case two.\n"
    f"B. Full suite in the FOREGROUND: {SUITE}\n"
    "C. PR title starts with '[agent: claude]'."
)


def _open(_pr):
    return "open"


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


def _root(q, description, context=None):
    q.enqueue(TaskRequest(task_id="impl-1040", task_type="implement",
                          description=description, branch=BRANCH,
                          context=dict(context or {})))


def _review(q, *, prev="impl-1040", findings=("HIGH x.py:1 - fix it",), context=None):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    ctx = {"prev_task_id": prev, "risk_tier": 1}
    ctx.update(context or {})
    q.enqueue(TaskRequest(task_id=review_id, task_type="review",
                          description="review", branch=BRANCH, context=ctx))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="request_changes: fix it",
        verdict="request_changes", findings=list(findings)))
    return review_id


def _fix(q, review_id):
    fix_id = auto_enqueue_fix(q, review_id, pr_state_fn=_open)
    assert fix_id is not None
    return q.get_task(fix_id)


def _long_root():
    head = "Problem statement. " + "x" * 2000 + "\n\n"
    tail = "\n\nBackground notes after the gate. " + "y" * 7000
    description = (head + ACCEPTANCE + tail)[:7000]
    assert description.index("Acceptance") > 1500 and ACCEPTANCE in description
    return description


def test_long_root_acceptance_block_copied_byte_for_byte(q):
    _root(q, _long_root())
    fix = _fix(q, _review(q))
    assert HEADING in fix.description
    assert ACCEPTANCE in fix.description
    assert SUITE in fix.description
    # Only the sections, not the unrelated 7k of background.
    assert "Background notes" not in fix.description


def test_root_without_marker_is_included_whole(q):
    description = "Make the widget blue.\n\nThen make it green as well."
    _root(q, description)
    fix = _fix(q, _review(q))
    assert f"{HEADING}\n{description}" in fix.description


def test_root_context_acceptance_keys_copied_unchanged(q):
    ctx = {"acceptance_verbatim": "A. do it\nB. test it",
           "verification_contract": {"cmd": SUITE, "zero_branch_only": True},
           "test_gate": ["tests/unit/test_x.py"],
           "ops_deps": {"deploy": False}}
    _root(q, ACCEPTANCE, context=ctx)
    fix = _fix(q, _review(q))
    for key, value in ctx.items():
        assert fix.context[key] == value


def test_second_round_takes_acceptance_from_root_once(q):
    _root(q, _long_root())
    fix1 = _fix(q, _review(q))
    review2 = _review(q, prev=fix1.task_id,
                      context={"fix_round": fix1.context["fix_round"],
                               "lineage_root_task_id": "impl-1040"})
    fix2 = _fix(q, review2)
    assert fix2.context["fix_round"] == 2
    assert fix2.description.count(ACCEPTANCE) == 1
    assert fix2.description.count("Parent acceptance and test gate") == 1
    assert HEADING in fix2.description


def test_second_round_walks_prev_chain_without_lineage_key(q):
    _root(q, ACCEPTANCE)
    fix1 = _fix(q, _review(q))
    review2 = _review(q, prev=fix1.task_id,
                      context={"fix_round": fix1.context["fix_round"]})
    fix2 = _fix(q, review2)
    assert fix2.description.count(ACCEPTANCE) == 1
    assert HEADING in fix2.description


def test_long_finding_still_capped_with_pointer(q):
    _root(q, ACCEPTANCE)
    review_id = _review(q, findings=("F" * (MAX_FINDING_CHARS + 500),))
    fix = _fix(q, review_id)
    assert "F" * MAX_FINDING_CHARS in fix.description
    assert "F" * (MAX_FINDING_CHARS + 1) not in fix.description
    assert f"[... truncated; full text in review task {review_id}]" in fix.description


def test_missing_root_row_still_enqueues_findings_with_warning(q, caplog):
    review_id = _review(q, prev="impl-gone")
    with caplog.at_level(logging.WARNING, logger="agent_crew.pipeline"):
        fix = _fix(q, review_id)
    assert "HIGH x.py:1 - fix it" in fix.description
    assert "Parent acceptance and test gate" not in fix.description
    assert any("no root implement task" in r.getMessage() for r in caplog.records)


def test_prev_cycle_does_not_hang(q, caplog):
    review_id = _review(q, prev="review-loop")
    q.enqueue(TaskRequest(task_id="review-loop", task_type="review",
                          description="r", branch=BRANCH,
                          context={"prev_task_id": review_id}))
    # #1094 rejects persisted cycles at admission; acceptance traversal still
    # terminates, but it must no longer produce an admitted fix for this graph.
    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) is None
    assert "cyclic_lineage" in caplog.text
    assert q.get_task(f"fix-{review_id}-r1") is None


def test_heading_section_runs_to_same_level_heading():
    description = ("## Scope\nstuff\n\n## Acceptance\n- one\n\nprose inside\n"
                   "### Detail\nmore\n\n## Notes\nunrelated")
    out = extract_acceptance_sections(description)
    assert out == "## Acceptance\n- one\n\nprose inside\n### Detail\nmore"


def test_plain_marker_stops_at_non_list_paragraph():
    description = "Intro.\n\nDone when:\n- a\n\n- b\n\nUnrelated prose."
    assert extract_acceptance_sections(description) == "Done when:\n- a\n\n- b"


@pytest.mark.parametrize("marker", ["Out of scope", "out of scope", "OUT OF SCOPE"])
def test_separate_out_of_scope_paragraph_copied_byte_for_byte(marker):
    out_of_scope = f"{marker}: other markers; enqueue logic.\nKeep tasks.db unchanged."
    expected = f"{ACCEPTANCE}\n\n{out_of_scope}"
    description = f"Problem statement.\n\n{expected}\n\nUnrelated background."
    assert extract_acceptance_sections(description).encode() == expected.encode()
