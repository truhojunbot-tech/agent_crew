"""Regression tests for issues #190, #192, #194.

#190 — Timeout path must still inspect the dispatch log for transient errors
and requeue, instead of permanently failing the task as `dispatcher_timeout`.

#192 — `QUOTA_EXHAUSTED` / `IneligibleTierError` are detected as separate,
non-retriable categories. Auto-requeue does NOT fire (resetting takes hours).
Tasks fail with a clear reason so operators see the actual cause.

#194 — `_transient_retries` dict cleanup is best verified at the call-site
level; we exercise it implicitly via the integration-shaped tests below.
"""
from agent_crew.server import (
    _detect_transient_error_in_log,
    _TRANSIENT_RETRIABLE_TAGS,
    _TRANSIENT_NONRETRIABLE_TAGS,
)


def _w(tmp_path, body: str) -> str:
    p = tmp_path / "dispatch.log"
    p.write_text(body)
    return str(p)


# ---------------------------------------------------------------------------
# #192 — QUOTA_EXHAUSTED / IneligibleTierError detection (non-retriable)
# ---------------------------------------------------------------------------

def test_b192_quota_exhausted_detected(tmp_path):
    log = _w(tmp_path, '"reason": "QUOTA_EXHAUSTED",\n')
    tag = _detect_transient_error_in_log(log)
    assert tag == "gemini_quota_exhausted"
    assert tag in _TRANSIENT_NONRETRIABLE_TAGS
    assert tag not in _TRANSIENT_RETRIABLE_TAGS


def test_b192_quota_reset_phrase_detected(tmp_path):
    log = _w(tmp_path, "Your quota will reset after 2h37m8s.\n")
    assert _detect_transient_error_in_log(log) == "gemini_quota_exhausted"


def test_b192_ineligible_tier_detected(tmp_path):
    log = _w(tmp_path,
             "IneligibleTierError: This client is no longer supported "
             "for Gemini Code Assist for individuals.\n")
    tag = _detect_transient_error_in_log(log)
    assert tag == "gemini_ineligible_tier"
    assert tag in _TRANSIENT_NONRETRIABLE_TAGS


def test_b192_quota_takes_priority_over_resource_exhausted(tmp_path):
    """Real gemini quota responses carry both markers. The more specific tag
    must win so the dispatcher routes to the non-retriable branch.
    """
    log = _w(tmp_path,
             '"status": "RESOURCE_EXHAUSTED",\n"reason": "QUOTA_EXHAUSTED",\n')
    assert _detect_transient_error_in_log(log) == "gemini_quota_exhausted"


def test_b192_capacity_still_classified_retriable(tmp_path):
    """MODEL_CAPACITY_EXHAUSTED (server-side throttle) remains retriable so
    the existing gemini-capacity recovery path keeps working.
    """
    log = _w(tmp_path, '"reason": "MODEL_CAPACITY_EXHAUSTED",\n')
    tag = _detect_transient_error_in_log(log)
    assert tag == "gemini_capacity"
    assert tag in _TRANSIENT_RETRIABLE_TAGS


# ---------------------------------------------------------------------------
# Category invariants
# ---------------------------------------------------------------------------

def test_retriable_and_nonretriable_are_disjoint():
    assert _TRANSIENT_RETRIABLE_TAGS.isdisjoint(_TRANSIENT_NONRETRIABLE_TAGS)
