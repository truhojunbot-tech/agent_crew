"""리뷰 결과 계약 — fail-closed (오너 지시 2026-09-23).

왜 이 파일이 있나: alpha_engine PR #5676 에서 리뷰 3라운드가 `verdict=None` 인 채
provenance(`provider=…; model=…; task_id=…`)를 `findings` 에 실어 보냈다. 옛 resolver 는
그 조합을 `request_changes` 로 **추론**했고, 그 판정마다 fix → retry-fix → retry-retry-fix
연쇄가 자동 생성돼 전부 실패했다. 리뷰어의 산문은 내내 "고칠 것 없음" 이었다.

⇒ 추론을 없앤다. 명시적 verdict 가 없으면 판정을 만들지 않는다.
"""
import pytest

from agent_crew.loop import INVALID_REVIEW_RESULT, _resolve_verdict
from agent_crew.protocol import TaskResult

PROVENANCE = "provider=codex; model=GPT-5; task_id=review-c394eeca; commit=45f94b840"
ACTIONABLE = "src/x/y.py:42 — queue lookup fails closed; make it fail open or say so"


def _r(verdict, findings, status="completed"):
    return TaskResult(task_id="t", status=status, summary="s",
                      verdict=verdict, findings=list(findings))


def test_approve_with_no_findings_is_approve():
    assert _resolve_verdict(_r("approve", [])) == "approve"


def test_request_changes_with_an_actionable_finding_is_request_changes():
    assert _resolve_verdict(_r("request_changes", [ACTIONABLE])) == "request_changes"


def test_null_verdict_with_no_findings_is_invalid():
    """⛔예전에는 approve 였다. 리뷰어가 아무 말도 안 한 것을 승인으로 읽으면 안 된다."""
    assert _resolve_verdict(_r(None, [])) == INVALID_REVIEW_RESULT


def test_null_verdict_with_provenance_in_findings_is_invalid():
    """⛔이것이 #5676 에서 실제로 온 형태다 — 예전에는 request_changes 로 찍혔다."""
    assert _resolve_verdict(_r(None, [PROVENANCE])) == INVALID_REVIEW_RESULT


def test_approve_carrying_findings_is_a_schema_contradiction():
    assert _resolve_verdict(_r("approve", [ACTIONABLE])) == INVALID_REVIEW_RESULT


def test_request_changes_with_no_findings_is_invalid():
    assert _resolve_verdict(_r("request_changes", [])) == INVALID_REVIEW_RESULT


@pytest.mark.parametrize("verdict,findings,expected", [
    ("approve", [], "approve"),
    ("request_changes", [ACTIONABLE], "request_changes"),
])
def test_summary_and_notes_do_not_change_the_verdict(verdict, findings, expected):
    """provenance·범위한정이 summary 에 있으면 판정에 영향이 없어야 한다."""
    r = TaskResult(task_id="t", status="completed",
                   summary=f"{PROVENANCE} — runtime evidence was coordinator-provided",
                   verdict=verdict, findings=list(findings))
    assert _resolve_verdict(r) == expected


def test_unknown_verdict_string_is_invalid():
    assert _resolve_verdict(_r("looks-fine-to-me", [])) == INVALID_REVIEW_RESULT


def test_dispatcher_failure_still_never_silently_approves():
    """⛔#301 의 계약은 유지한다 — 안 끝난 리뷰를 승인으로 바꾸지 않는다."""
    assert _resolve_verdict(_r("approve", [], status="timed_out")) == "request_changes"


def test_invalid_is_not_one_of_the_two_verdicts():
    """호출부는 전부 approve/request_changes 와 비교한다 — invalid 는 어디에도 안 걸려야 한다."""
    assert INVALID_REVIEW_RESULT not in ("approve", "request_changes")
