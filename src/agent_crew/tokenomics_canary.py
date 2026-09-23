"""ONE-TASK tokenomics canary — the narrowest suppression the evidence supports.

The matched evidence at quota-core ``sev0/phaseb-80-contract-emitter`` 869a3cf
REJECTED the generic "no-progress review-fix suppression" and supports only one
shape: a review dispatch is wasted when a ``request_changes`` verdict ALREADY
STANDS on the identical ``(PR or branch, reviewed_sha)`` — i.e. the reviewer
would be sent to read a commit that has not moved since it was already told to
change. In the baseline window 11 organic tasks took that shape and 0 of them
produced an ``approve``; the measured effect is >= 64,223,256 cache-read and
151,940 output tokens.

⛔This module implements the CONFIG FLIP ONLY. It does not arm itself and it
  does not pick a task: :data:`CANARY_ENV` is read from the environment on
  every dispatch, so unsetting it restores the shadow (dispatch-everything)
  behaviour immediately, with no restart and no state to unwind. That is the
  entire rollback plan — one stateless dispatch-time boolean.

⛔Scope is one task by construction. The pin names the IMPLEMENT (parent) task
  id, so only reviews in that one lineage can ever be suppressed. Every other
  task in the fleet stays shadow: it is evaluated, a receipt records what would
  have happened, and the review dispatches exactly as before.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: The single arming switch. Alfred sets it; this repo never does.
CANARY_ENV = "AGENT_CREW_TOKENOMICS_CANARY_TASK_ID"

#: The one recommendation kind the matched evidence supports.
RECOMMENDATION_KIND = "suppress_identical_sha_rereview"

#: Written into the receipt as ``shadow_decision_source`` — the recommendation
#: comes from the quota-core contract, not from a policy this repo invented.
DECISION_SOURCE = "quota_core_contract"

#: Terminal reason recorded on a suppressed review task.
SUPPRESSED_REASON = "tokenomics_canary_suppressed_identical_sha_rereview"

#: Stamped on every finding a suppression copies forward, naming the review
#: that actually produced it. Without it a fix agent reads findings whose
#: ``task_id`` provenance points at a task no reviewer ever ran, and the
#: measurement of what the canary reused stops being reconstructible.
REUSED_FROM_KEY = "canary_reused_from"

#: A full git object id — 40 hex for sha1, 64 for sha256. Deliberately not a
#: prefix match, for the same reason ``protocol._OBJECT_ID_RE`` is not: an
#: abbreviation cannot be compared for identity, and identity is the whole
#: condition here.
_OBJECT_ID_RE = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


def reuse_findings(findings, standing_review_task_id: str) -> list:
    """Copy a standing review's findings forward, tagging each with its origin.

    A suppressed review is a review that was deliberately not run, so it has
    nothing of its own to say. What it carries instead is the judgement that
    already stands on the identical commit — copied verbatim, never
    paraphrased, and each item stamped with :data:`REUSED_FROM_KEY` so a reader
    (or a fix agent) can always get back to the review that made it.

    Non-dict findings are wrapped rather than dropped: the stamp needs
    somewhere to live, and losing a finding to make room for its provenance
    would defeat the point. ``loop.build_feedback`` parses the wrapped text the
    same way it parses a bare string.
    """
    reused = []
    for finding in findings or []:
        if isinstance(finding, dict):
            reused.append({**finding, REUSED_FROM_KEY: standing_review_task_id})
        else:
            reused.append({"issue": str(finding),
                           REUSED_FROM_KEY: standing_review_task_id})
    return reused


def canary_pin(env: Optional[dict] = None) -> str:
    """The armed task id, read fresh on every call.

    ⛔Read per dispatch, never cached at import. A cached pin would mean
      arming and — far more importantly — DISARMING require a server restart,
      and a rollback that needs a restart is not a rollback.
    """
    source = os.environ if env is None else env
    return (source.get(CANARY_ENV) or "").strip()


@dataclass
class CanaryDecision:
    """What the canary would do, and whether it did it.

    ``applied`` is the only field that changes behaviour. Everything else
    exists so the receipt can be read later without re-deriving anything.
    """

    applied: bool
    reason: str
    reviewed_sha: str = ""
    target: str = ""
    pinned_task_id: str = ""
    standing_review_task_id: str = ""
    #: The standing review's verdict and findings, carried so the suppression
    #: can be recorded as that verdict instead of as a failure.
    standing_verdict: str = ""
    standing_findings: list = field(default_factory=list)
    counterfactual: str = ""
    kind: str = RECOMMENDATION_KIND
    decision_source: str = DECISION_SOURCE
    extra: dict = field(default_factory=dict)

    def recommendation(self) -> dict[str, Any]:
        """The receipt payload — self-contained on purpose.

        The column set may grow; a consumer reading only this JSON must still
        be able to say what was recommended, whether it was applied, and what
        the counterfactual was.
        """
        return {
            "kind": self.kind,
            # The field name the owner's §11 canary spec asks for, kept inside
            # the JSON as well as in its own column: the column is what
            # queries filter on, this is what survives being copied around.
            "shadow_decision_source": self.decision_source,
            "applied": self.applied,
            "reason": self.reason,
            "counterfactual": self.counterfactual,
            "reviewed_sha": self.reviewed_sha,
            "target": self.target,
            "pinned_task_id": self.pinned_task_id,
            "standing_review_task_id": self.standing_review_task_id,
            **self.extra,
        }


def _target_of(task) -> tuple[str, Optional[int], str]:
    """``(label, pr_number, branch)`` — what this review is about.

    A PR number identifies the review target exactly; a branch is the fallback
    for crews that review without opening one. Never both: comparing against a
    branch when a PR number exists would match reviews of a different head.
    """
    ctx = task.context if isinstance(getattr(task, "context", None), dict) else {}
    pr_number = ctx.get("pr_number")
    if pr_number is None:
        pr_number = getattr(task, "pr_number", None)
    try:
        pr_number = int(pr_number) if pr_number is not None and not isinstance(pr_number, bool) else None
    except (TypeError, ValueError):
        pr_number = None
    branch = (getattr(task, "branch", "") or "").strip()
    if pr_number is not None:
        return (f"pr:{pr_number}", pr_number, "")
    if branch:
        return (f"branch:{branch}", None, branch)
    return ("", None, "")


def _is_pinned(task, pin: str) -> bool:
    """Does the pin name this review's lineage?

    The pin is the IMPLEMENT task id (``context.prev_task_id``).  The owner
    named that identity explicitly; accepting the review id would create a
    second, undocumented armed surface.
    """
    ctx = task.context if isinstance(getattr(task, "context", None), dict) else {}
    parent = (ctx.get("prev_task_id") or "").strip()
    return pin == parent and bool(pin)


def evaluate_review_dispatch(
    task,
    *,
    reviewed_sha: str,
    standing_lookup: Callable[..., Optional[dict]],
    pin: Optional[str] = None,
) -> CanaryDecision:
    """Decide whether THIS review dispatch is the one to suppress.

    ``standing_lookup(pr_number=, branch=, reviewed_sha=, exclude_task_id=)``
    returns the most recent terminal review that carries a verdict for the same
    target and the same commit, or ``None``.

    ⛔The condition is evaluated for EVERY review, armed or not; the pin only
      decides whether the answer is acted on. Measuring only the pinned task
      would make the canary unfalsifiable — there would be no fleet-wide count
      of how often the condition holds to compare the one applied case against,
      which is precisely the "estimate presented as measurement" this
      organisation forbids.

    ⛔Every ``return`` below that is not the last one is ``applied=False`` —
      the default is always "dispatch". A bug, a missing field or an unreadable
      row can only produce a shadow receipt, never a silent suppression.
    """
    pin = canary_pin() if pin is None else (pin or "").strip()
    label, pr_number, branch = _target_of(task)
    base = {"reviewed_sha": reviewed_sha or "", "target": label, "pinned_task_id": pin}

    if (getattr(task, "task_type", "") or "") != "review":
        return CanaryDecision(applied=False, reason="not_a_review_task", **base)
    if not _OBJECT_ID_RE.match(reviewed_sha or ""):
        # No pin on the commit means "identical sha" cannot be established.
        # #253's rule applies: when the target's state cannot be verified,
        # change nothing.
        return CanaryDecision(applied=False, reason="no_reviewed_sha", **base)
    if not label:
        return CanaryDecision(applied=False, reason="no_review_target", **base)

    try:
        standing = standing_lookup(
            pr_number=pr_number, branch=branch, reviewed_sha=reviewed_sha,
            exclude_task_id=getattr(task, "task_id", "") or "",
        )
    except Exception:
        logger.exception(
            "tokenomics canary: standing-review lookup failed for %s — dispatching",
            getattr(task, "task_id", "?"),
        )
        return CanaryDecision(applied=False, reason="standing_lookup_failed", **base)

    if not standing:
        return CanaryDecision(applied=False, reason="no_prior_verdict_on_this_sha", **base)
    verdict = (standing.get("verdict") or "").strip()
    standing_id = standing.get("task_id") or ""
    if verdict != "request_changes":
        # An ``approve`` on the identical commit means the request_changes no
        # longer stands. Suppressing here would hide a real state change.
        return CanaryDecision(
            applied=False, reason=f"standing_verdict_is_{verdict or 'unknown'}",
            standing_review_task_id=standing_id, **base)

    # From here the condition HOLDS: a request_changes stands on this exact
    # commit, so this dispatch would re-review a sha that has not moved.
    counterfactual = f"review would have been dispatched on unchanged sha {reviewed_sha}"
    extra = {"standing_verdict": verdict,
             "standing_findings_count": standing.get("findings_count"),
             "condition_holds": True}
    if not pin:
        return CanaryDecision(
            applied=False, reason="condition_holds_canary_unarmed",
            standing_review_task_id=standing_id, counterfactual=counterfactual,
            extra=extra, **base)
    if not _is_pinned(task, pin):
        return CanaryDecision(
            applied=False, reason="condition_holds_not_the_pinned_task",
            standing_review_task_id=standing_id, counterfactual=counterfactual,
            extra=extra, **base)
    return CanaryDecision(
        applied=True,
        reason="standing_request_changes_on_identical_sha",
        standing_review_task_id=standing_id,
        standing_verdict=verdict,
        standing_findings=list(standing.get("findings") or []),
        counterfactual=counterfactual,
        extra=extra,
        **base,
    )
