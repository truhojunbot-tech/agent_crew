"""Defensive verdict parsing for `verdict=null` reviews (Issue #100).

Reviewer agents have been observed sending ``verdict=null`` with empty
``findings`` when they have nothing to flag. The literal CLI rule
``verdict == "approve"`` interpreted that as a rejection and forced
unnecessary re-implementation rounds.
"""
from agent_crew.loop import _resolve_verdict, handle_review_result
from agent_crew.protocol import TaskResult


def _result(verdict=None, findings=None, status="completed"):
    return TaskResult(
        task_id="t-1",
        status=status,
        summary="ok",
        verdict=verdict,
        findings=findings or [],
        pr_number=None,
    )


# ---------------------------------------------------------------------------
# _resolve_verdict
# ---------------------------------------------------------------------------


class TestResolveVerdict:
    def test_explicit_approve(self):
        assert _resolve_verdict(_result(verdict="approve")) == "approve"

    def test_explicit_request_changes(self):
        assert _resolve_verdict(_result(verdict="request_changes")) == "request_changes"

    def test_null_verdict_no_findings_treated_as_approve(self):
        # The exact #100 scenario — codex sent verdict=None with [] findings.
        assert _resolve_verdict(_result(verdict=None, findings=[])) == "approve"

    def test_null_verdict_with_findings_is_request_changes(self):
        result = _result(verdict=None, findings=["bug: off-by-one"])
        assert _resolve_verdict(result) == "request_changes"

    def test_empty_string_verdict_no_findings_is_approve(self):
        # Some agents may send "" instead of null.
        assert _resolve_verdict(_result(verdict="", findings=[])) == "approve"

    def test_empty_string_verdict_with_findings_is_request_changes(self):
        result = _result(verdict="", findings=[{"severity": "high", "msg": "x"}])
        assert _resolve_verdict(result) == "request_changes"

    def test_unknown_verdict_with_findings_is_request_changes(self):
        # Defensive — anything that isn't 'approve' falls through.
        result = _result(verdict="changes_requested", findings=["x"])
        assert _resolve_verdict(result) == "request_changes"


# ---------------------------------------------------------------------------
# handle_review_result honors the resolver
# ---------------------------------------------------------------------------


class TestHandleReviewResult:
    def test_null_verdict_clean_review_returns_approved(self):
        outcome = handle_review_result(
            _result(verdict=None, findings=[]),
            iteration=2,
            max_iter=5,
            no_tester=True,
        )
        assert outcome == "approved"

    def test_null_verdict_with_findings_returns_request_changes(self):
        outcome = handle_review_result(
            _result(verdict=None, findings=["unrelated nit"]),
            iteration=2,
            max_iter=5,
            no_tester=True,
        )
        assert outcome == "request_changes"

    def test_clean_null_at_max_iter_does_not_escalate(self):
        """The #100 regression case: at iteration 5 of 5 with verdict=None
        and empty findings, the loop must approve, not escalate."""
        outcome = handle_review_result(
            _result(verdict=None, findings=[]),
            iteration=5,
            max_iter=5,
            no_tester=True,
        )
        assert outcome == "approved"

    def test_request_changes_at_max_iter_escalates(self):
        outcome = handle_review_result(
            _result(verdict=None, findings=["real bug"]),
            iteration=5,
            max_iter=5,
            no_tester=True,
        )
        assert outcome == "escalate"
