"""#1048: the AC and linked-review detectors behind `is_sufficient`.

Arm B (#660) found the pack missing acceptance criteria that were present in
the issue under `## Done when` / `**Acceptance:**`, or only in the task
description, and missing the review on every fix round although the fix
task's context carries it. These fixtures pin each form.
"""

import json

import pytest

from agent_crew import context_pack as cp


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # As conftest does: never reach a live Forge (:8769) or memory DB.
    for var in ("AGENT_CREW_FORGE_PROVIDER", "AGENT_CREW_SHADOW_MEMORY_DB"):
        monkeypatch.delenv(var, raising=False)


def _build(tmp_path, ctx, **kw):
    return cp.build_pack_for_task(
        ctx, task_id=kw.pop("task_id", "impl-x"), task_type="implement",
        role="implementer", repo_path=str(tmp_path), **kw)


def _ac_items(pack):
    return [a for a in pack.items if a.artifact_type == cp.TYPE_AC]


def _no_ac(pack):
    return any(a.artifact_type == cp.TYPE_ISSUE and "no_ac=true" in a.provenance
               for a in pack.items)


def test_f1a_done_when_heading_gives_ac(tmp_path):
    body = ("Problem text.\n\n## Done when\n- the thing works\n- tests pass\n\n"
            "## Notes\nunrelated")
    assert cp.IssueProvider.extract_ac(body) == "- the thing works\n- tests pass"
    pack = _build(tmp_path, {"issue": 908, "issue_title": "t", "issue_body": body})
    [ac] = _ac_items(pack)
    assert ac.excerpt == "- the thing works\n- tests pass"
    assert not _no_ac(pack)


@pytest.mark.parametrize("body,expected", [
    ("Intro.\n\n**Acceptance:** the replay reports before/after counts.\n\n"
     "**Output:** a comment",
     "the replay reports before/after counts."),
    ("Intro.\n\n**Acceptance:**\n\n- A. first\n- B. second\n\nTrailing prose.",
     "- A. first\n- B. second"),
])
def test_f1b_bold_acceptance_label_gives_ac(tmp_path, body, expected):
    assert cp.IssueProvider.extract_ac(body) == expected
    pack = _build(tmp_path, {"issue": 1022, "issue_title": "t", "issue_body": body})
    [ac] = _ac_items(pack)
    assert ac.excerpt == expected
    assert "Output" not in ac.excerpt and "Trailing" not in ac.excerpt


def test_f1c_description_acceptance_when_issue_body_empty(tmp_path):
    description = ("Implement the thing.\n\nAcceptance (all required):\n"
                   "- A. tests pass\n- B. counts in PR body\n\nRequired in this task: x")
    pack = _build(tmp_path, {"issue": 1040}, description=description,
                  issue_body_fn=lambda *a, **k: "")
    [ac] = _ac_items(pack)
    assert ac.excerpt == "- A. tests pass\n- B. counts in PR body"
    assert "task description" in ac.provenance
    # ctx["description"] is read the same way when no kwarg is given.
    pack2 = _build(tmp_path, {"issue": 1040, "description": description},
                   issue_body_fn=lambda *a, **k: "")
    assert [a.excerpt for a in _ac_items(pack2)] == [ac.excerpt]


def test_f1d_no_ac_form_keeps_no_ac(tmp_path):
    body = ("Measure is_sufficient over live rows.\n\n"
            "**Output:** one comment on this issue with the three tables, the "
            "method, and the data window.\n")
    assert cp.IssueProvider.extract_ac(body) == ""
    pack = _build(tmp_path, {"issue": 660, "issue_title": "t", "issue_body": body})
    assert _ac_items(pack) == []
    assert _no_ac(pack)


def _fix_ctx(**extra):
    ctx = {"issue": "1033", "issue_title": "t", "issue_body": "## Acceptance\n- ok",
           "fix_round": "1", "prev_task_id": "review-impl-1033d-r0",
           "pr_number": "1042", "repo": "truhojunbot-tech/agent_crew"}
    ctx.update(extra)
    return ctx


def test_f2a_fix_round_with_findings_gives_linked_review(tmp_path):
    findings = json.dumps(["P1 — fail-open on stale signal", "P2 — missing test"])
    pack = _build(tmp_path, _fix_ctx(review_findings=findings),
                  task_id="fix-review-impl-1033d-r0-r1",
                  budget={"max_tokens": 1, "max_items": 1})
    [review] = [a for a in pack.items if a.artifact_type == cp.TYPE_REVIEW]
    assert review.mandatory  # survives a budget that admits nothing optional
    assert "review-impl-1033d-r0" in review.excerpt
    assert "P1 — fail-open on stale signal" in review.excerpt
    assert "P2 — missing test" in review.excerpt
    assert review.uri.endswith("/pull/1042")
    gate = cp.is_sufficient(pack, fix_round=1)
    assert not any("linked_review" in s for s in gate.missing_signals)


def test_f2b_fix_round_without_findings_has_no_review(tmp_path):
    pack = _build(tmp_path, _fix_ctx(), task_id="fix-review-impl-1033d-r0-r1")
    assert not any(a.artifact_type == cp.TYPE_REVIEW for a in pack.items)
    gate = cp.is_sufficient(pack, fix_round=1)
    assert any("linked_review" in s for s in gate.missing_signals)
