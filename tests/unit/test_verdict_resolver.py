"""Review verdicts require an explicit, internally consistent decision (#376).

#100's null/empty review must not consume a fix round: silence is neither an
approval nor a request for changes. The fail-closed result stops the CLI loop.
"""
from agent_crew.loop import INVALID_REVIEW_RESULT, _resolve_verdict, handle_review_result
from agent_crew.protocol import TaskResult


def _result(verdict=None, findings=None, status="completed"):
    return TaskResult(
        task_id="t-1", status=status, summary="ok", verdict=verdict,
        findings=findings or [], pr_number=None,
    )


class TestResolveVerdict:
    def test_explicit_approve(self):
        assert _resolve_verdict(_result(verdict="approve")) == "approve"

    def test_explicit_request_changes_with_finding(self):
        assert _resolve_verdict(_result(
            verdict="request_changes", findings=["src/x.py:1: fix the bug"]
        )) == "request_changes"

    def test_request_changes_without_findings_is_invalid(self):
        # A final rejection must name a change; otherwise it cannot drive a fix.
        assert _resolve_verdict(_result(verdict="request_changes")) == INVALID_REVIEW_RESULT

    def test_approve_with_findings_is_invalid(self):
        assert _resolve_verdict(_result(
            verdict="approve", findings=["src/x.py:1: fix the bug"]
        )) == INVALID_REVIEW_RESULT

    def test_null_verdict_no_findings_is_invalid(self):
        # #100: a clean-looking but silent review cannot authorize approval.
        assert _resolve_verdict(_result(verdict=None, findings=[])) == INVALID_REVIEW_RESULT

    def test_null_verdict_with_findings_is_invalid(self):
        assert _resolve_verdict(_result(
            verdict=None, findings=["bug: off-by-one"]
        )) == INVALID_REVIEW_RESULT

    def test_empty_string_verdict_no_findings_is_invalid(self):
        assert _resolve_verdict(_result(verdict="", findings=[])) == INVALID_REVIEW_RESULT

    def test_empty_string_verdict_with_findings_is_invalid(self):
        result = _result(verdict="", findings=[{
            "severity": "high", "file": "loop.py", "line": 1,
            "title": "Bug", "detail": "The result needs changes",
        }])
        assert _resolve_verdict(result) == INVALID_REVIEW_RESULT

    def test_unknown_verdict_with_findings_is_invalid(self):
        result = _result(verdict="changes_requested", findings=["src/x.py:1: bug"])
        assert _resolve_verdict(result) == INVALID_REVIEW_RESULT


class TestHandleReviewResult:
    def test_null_verdict_clean_review_stops_without_fix(self):
        outcome = handle_review_result(
            _result(verdict=None, findings=[]), iteration=2, max_iter=5,
            no_tester=True,
        )
        assert outcome == INVALID_REVIEW_RESULT

    def test_null_verdict_with_findings_stops_without_fix(self):
        outcome = handle_review_result(
            _result(verdict=None, findings=["unrelated nit"]),
            iteration=2, max_iter=5, no_tester=True,
        )
        assert outcome == INVALID_REVIEW_RESULT

    def test_clean_null_at_max_iter_does_not_escalate(self):
        # Malformed feedback must neither consume a fix round nor open a gate.
        outcome = handle_review_result(
            _result(verdict=None, findings=[]), iteration=5, max_iter=5,
            no_tester=True,
        )
        assert outcome == INVALID_REVIEW_RESULT

    def test_real_request_changes_at_max_iter_escalates(self):
        outcome = handle_review_result(
            _result(verdict="request_changes", findings=["src/x.py:1: real bug"]),
            iteration=5, max_iter=5, no_tester=True,
        )
        assert outcome == "escalate"
