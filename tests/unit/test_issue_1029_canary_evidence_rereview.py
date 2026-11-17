"""#1029: a coordinator's evidence comment, not a queue flag, re-opens a review.

The queue's DUPLICATE_REVIEW guard already makes every same-SHA review carry
``allow_duplicate_review`` + ``duplicate_review_override``, so that flag cannot
tell the canary anything. The discriminator is a PR comment that is on the same
PR, newer than the standing verdict, and names the commit under review.
Anything less keeps today's suppression.
"""
import json

import pytest

from agent_crew import tokenomics_canary as canary
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

REPO = "truhojunbot-tech/agent_crew"
SHA = "2305154e" + "0123456789abcdef" * 2  # Quota's fit case, 40 hex
OTHER_SHA = "c" * 40
PR = 368
URL = f"https://github.com/{REPO}/pull/{PR}#issuecomment-555"
POSTED = 1_000_000.0


def _review(*, url=URL, allow_duplicate_review=True, pr=PR, repo=REPO):
    ctx = {"risk_tier": 1, "prev_task_id": "impl-1", "pr_number": pr, "repo": repo,
           "reviewed_sha": SHA, "allow_duplicate_review": allow_duplicate_review}
    if url is not None:
        ctx[canary.EVIDENCE_URL_KEY] = url
    return TaskRequest(task_id="review-impl-1-r1", task_type="review",
                       description="Review", branch="feat/x", context=ctx)


def _standing(posted_at=POSTED):
    def lookup(**_kw):
        return {"task_id": "review-impl-1-r0", "verdict": "request_changes",
                "status": "completed", "findings": ["f1"], "findings_count": 1,
                "result_posted_at": posted_at}
    return lookup


def _evidence(*, repo=REPO, pr=PR, created_at=POSTED + 60, body=f"re-review {SHA} please"):
    def lookup(url):
        return {"repo": repo, "pr_number": pr, "created_at": created_at, "body": body}
    return lookup


def _decide(task=None, *, evidence_lookup=None, standing=None):
    return canary.evaluate_review_dispatch(
        task or _review(), reviewed_sha=SHA,
        standing_lookup=standing or _standing(), project="agent_crew",
        evidence_lookup=evidence_lookup)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "project:agent_crew")


def _assert_suppressed(decision):
    assert decision.applied is True
    assert decision.reason == "standing_request_changes_on_identical_sha"


def test_full_pass_dispatches_with_condition_still_recorded():
    decision = _decide(evidence_lookup=_evidence())
    assert decision.applied is False
    assert decision.reason == "coordinator_evidence_rereview"
    assert decision.extra["condition_holds"] is True
    assert decision.standing_review_task_id == "review-impl-1-r0"
    assert decision.recommendation()[canary.EVIDENCE_URL_KEY] == URL


def test_quota_fit_case_comment_after_r0_qualifies():
    body = f"Evidence: head {SHA} fixes the finding; re-review requested."
    assert _decide(evidence_lookup=_evidence(body=body)).reason == "coordinator_evidence_rereview"


@pytest.mark.parametrize("kw", [
    {"pr": PR + 1},                       # (a) another PR
    {"repo": "someone/else"},             # (a) another repo
    {"created_at": POSTED},               # (b) at the standing result time
    {"created_at": POSTED - 1},           # (b) before it
    {"body": "re-review please"},         # (c) no SHA
    {"body": f"re-review {OTHER_SHA}"},   # (c) another SHA
    {"body": f"re-review {SHA[:12]}"},    # (c) abbreviation only
    {"body": f"re-review {SHA}ab"},       # (c) only as a prefix of a longer hex
])
def test_each_failing_condition_alone_keeps_the_suppression(kw):
    _assert_suppressed(_decide(evidence_lookup=_evidence(**kw)))


@pytest.mark.parametrize("url", [
    f"https://github.com/{REPO}/pull/{PR + 1}#issuecomment-555",  # URL names other PR
    f"https://github.com/someone/else/pull/{PR}#issuecomment-555",
    "not a url", f"https://github.com/{REPO}/pull/{PR}", "", 7,
])
def test_wrong_or_malformed_url_keeps_the_suppression(url):
    _assert_suppressed(_decide(_review(url=url), evidence_lookup=_evidence()))


def test_missing_url_keeps_the_suppression_even_with_the_duplicate_flag():
    calls = []

    def lookup(url):
        calls.append(url)
        return _evidence()(url)
    _assert_suppressed(_decide(_review(url=None, allow_duplicate_review=True),
                               evidence_lookup=lookup))
    assert calls == []


def test_lookup_none_raise_or_absent_keeps_the_suppression():
    def boom(_url):
        raise RuntimeError("gh down")
    _assert_suppressed(_decide(evidence_lookup=lambda _u: None))
    _assert_suppressed(_decide(evidence_lookup=boom))
    _assert_suppressed(_decide(evidence_lookup=None))
    _assert_suppressed(_decide(evidence_lookup=_evidence(created_at="garbage")))


def test_unknown_standing_result_time_keeps_the_suppression():
    _assert_suppressed(_decide(evidence_lookup=_evidence(), standing=_standing(None)))


def test_review_without_repo_keeps_the_suppression():
    _assert_suppressed(_decide(_review(repo=""), evidence_lookup=_evidence()))


# ── the standing row carries its result time ───────────────────────────────

def test_standing_lookup_reports_result_posted_at(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="rev-0", task_type="review", description="r",
                              branch="feat/x", context={"pr_number": PR, "reviewed_sha": SHA}))
    queue.submit_result("rev-0", TaskResult(task_id="rev-0", status="completed",
                                            summary="reviewed", verdict="request_changes",
                                            findings=["f1"], pr_number=PR))
    hit = queue.standing_request_changes_review(pr_number=PR, reviewed_sha=SHA)
    assert isinstance(hit["result_posted_at"], float) and hit["result_posted_at"] > 0


# ── the lookup the server wires in ─────────────────────────────────────────

class _FakeGh:
    def __init__(self, record):
        self.record, self.calls = record, []

    def _api(self, args, stdin=None):
        self.calls.append(args)
        return None if self.record is None else json.dumps(self.record)


def _record(**over):
    record = {"id": 555, "body": f"re-review {SHA}", "created_at": "2026-11-16T22:00:00Z",
              "issue_url": f"https://api.github.com/repos/{REPO}/issues/{PR}"}
    record.update(over)
    return record


def test_server_wires_github_comment_evidence(monkeypatch):
    from agent_crew import server
    assert "evidence_lookup=_canary.github_comment_evidence" in open(server.__file__).read()


def test_github_comment_evidence_reads_the_comment():
    gh = _FakeGh(_record())
    evidence = canary.github_comment_evidence(URL, gh=gh)
    assert gh.calls == [[f"repos/{REPO}/issues/comments/555"]]
    assert evidence == {"repo": REPO, "pr_number": PR, "body": f"re-review {SHA}",
                        "created_at": 1794866400.0}
    # and it feeds the canary end to end
    decision = _decide(evidence_lookup=lambda u: canary.github_comment_evidence(u, gh=gh),
                       standing=_standing(1794866400.0 - 1))
    assert decision.reason == "coordinator_evidence_rereview"


@pytest.mark.parametrize("record", [
    None,
    _record(issue_url=f"https://api.github.com/repos/{REPO}/issues/{PR + 1}"),
    _record(issue_url="https://api.github.com/repos/someone/else/issues/368"),
    _record(created_at="yesterday"),
    _record(body=None),
])
def test_github_comment_evidence_failures_are_none(record):
    assert canary.github_comment_evidence(URL, gh=_FakeGh(record)) is None


def test_github_comment_evidence_uses_gh_api_with_timeout(monkeypatch):
    import subprocess
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, json.dumps(_record()), ""

    def run(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")
        return Proc()
    monkeypatch.setattr(subprocess, "run", run)
    assert canary.github_comment_evidence(URL)["pr_number"] == PR
    assert seen["cmd"] == ["gh", "api", f"repos/{REPO}/issues/comments/555"]
    assert seen["timeout"] == 10.0

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(subprocess, "run", timeout)
    assert canary.github_comment_evidence(URL) is None
    assert canary.github_comment_evidence("garbage") is None
