"""Stage cascade hooks — transport-agnostic (Issue #123).

When a task gets a result submitted, three follow-up flows may fire:

  1. ``auto_enqueue_review``           impl ✓  → enqueue a review task
  2. ``auto_enqueue_test``             review approve → enqueue a test task
  3. ``auto_enqueue_fix``              review request_changes → enqueue a fix
  4. ``auto_fallback_failed_task``     rate-limit ✗ → reroute to next agent

Both transports — HTTP ``submit_result`` and MCP ``submit_result`` — must
trigger these so the pipeline doesn't stall after the first stage when an
agent is on the MCP-only path (#106 cutover prerequisite).

The helpers operate on a ``TaskQueue`` and never touch tmux. Push
notifications (paste-buffer + send-keys) are an HTTP-side concern that
remains in ``server.py``: ``_auto_enqueue_review`` etc. wrap these
functions, run them first, and *then* call ``_try_push_next``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import json
import subprocess
import uuid
from dataclasses import dataclass, replace
from typing import Optional

from agent_crew.fallback import (
    default_agent_for_role,
    has_rate_limit_signal,
    load_fallback_chains,
    next_agent,
)
from agent_crew.loop import _resolve_verdict
from agent_crew.notify import notify_telegram
from agent_crew.protocol import (
    GateRequest,
    TaskRequest,
    TaskResult,
    normalize_pr_number,
    RESULT_BRANCH_CONTEXT_KEY,
    RESULT_COMMIT_CONTEXT_KEY,
)
from agent_crew.queue import TaskQueue, _TYPE_TO_ROLE, PausedError, TaskAlreadyExistsError
from agent_crew.risk_tier import (
    TIER_0, TIER_1, TIER_2, TIER_3, cascade_metadata, classify_task,
    effective_fix_round_cap, risk_tier_enforcement_enabled, shadow_decision,
)

logger = logging.getLogger(__name__)

MAX_FALLBACK_CHAIN_DEPTH = 3

#: Automated fix rounds allowed per review lineage (#244). The cap is the
#: whole reason this transition is safe to automate: a reviewer that keeps
#: rejecting would otherwise drive review→fix→review forever, burning quota on
#: a disagreement no additional round will settle. After the cap the loop stops
#: and says so on the PR, because the next move is a human's.
DEFAULT_REVIEW_FIX_MAX_ROUNDS = 3
#: Bounds on how much review text is copied into the fix task description.
MAX_EMBEDDED_FINDINGS = 20
MAX_FINDING_CHARS = 1000


#: Marker every automated-exhaustion comment carries, so the cascade can
#: recognise its own prior announcement instead of repeating it (#250).
FIX_EXHAUSTED_MARKER = "[agent_crew] Automated fix rounds exhausted"
#: Announcement kind for the durable one-shot claim (see TaskQueue).
FIX_EXHAUSTED_KIND = "fix_exhausted"

#: Prefix on the stored summary of a result whose PR number disagreed with the
#: one its task was dispatched for (#268). Grep-able on purpose: it is the only
#: trace a human has that the reviewer answered a different question.
PR_MISMATCH_MARKER = "[agent_crew] PR MISMATCH"


def verify_implement_artifact(
    task: TaskRequest, result: TaskResult, *, repo_cwd: str,
) -> tuple[bool, str]:
    """Fail closed unless a completed implementation names pushed new code (#353).

    This is deliberately a git proof, not a trust decision over the worker's
    prose: the result commit must descend from the worktree base and be
    reachable from the reported branch on ``origin``.  Any unavailable git
    evidence is indistinguishable from no artifact for handoff purposes.
    """
    context = task.context if isinstance(task.context, dict) else {}
    base = str(context.get("worktree_base_sha") or context.get("reviewed_sha") or "").strip()
    branch = (result.branch or task.branch or "").strip()
    commit = (result.commit or "").strip()
    if not repo_cwd:
        return False, "artifact repository unavailable"
    if not base or not branch:
        return False, "missing base or branch"
    try:
        # Workers should report their full SHA, but a missing typed field must
        # not discard code that origin can prove was pushed.  Deriving only
        # from the reported branch's fetched origin ref keeps this a git proof,
        # never a trust decision over summary prose.
        fetch = subprocess.run(
            ["git", "-C", repo_cwd, "fetch", "origin", branch, "--quiet"],
            capture_output=True, text=True, timeout=60,
        )
        derived_commit = False
        if not commit:
            if fetch.returncode != 0:
                return False, "origin branch unavailable for commit derivation"
            origin_head = subprocess.run(
                ["git", "-C", repo_cwd, "rev-parse", "--verify",
                 f"origin/{branch}^{{commit}}"],
                capture_output=True, text=True, timeout=30,
            )
            commit = origin_head.stdout.strip()
            if origin_head.returncode != 0 or not commit:
                return False, "origin branch head is not resolvable for commit derivation"
            # Persist the independently-derived full SHA so the accepted
            # handoff remains auditable through the normal result path.
            result.commit = commit
            derived_commit = True

        if commit == base:
            return False, "reported commit is the dispatch base (no new artifact)"
        local_commit = subprocess.run(
            ["git", "-C", repo_cwd, "rev-parse", "--verify", f"{commit}^{{commit}}"],
            capture_output=True, text=True, timeout=30,
        )
        if local_commit.returncode != 0:
            return False, "reported commit is not locally resolvable"
        descends_from_base = subprocess.run(
            ["git", "-C", repo_cwd, "merge-base", "--is-ancestor", base, commit],
            capture_output=True, text=True, timeout=30,
        )
        if descends_from_base.returncode != 0:
            return False, "reported commit does not descend from the dispatch base"

        if fetch.returncode == 0:
            reachable = subprocess.run(
                ["git", "-C", repo_cwd, "merge-base", "--is-ancestor", commit,
                 f"origin/{branch}"],
                capture_output=True, text=True, timeout=30,
            )
            if reachable.returncode == 0:
                if derived_commit:
                    return True, "origin branch contains derived commit"
                return True, "origin branch contains reported commit"

        # A PR is an alternate durable handoff artifact: a branch may be
        # unavailable to the worker's remote configuration while GitHub still
        # has an open PR pinned to precisely this commit.  It is not a prose
        # bypass — both the PR number and its immutable head SHA must agree.
        pr_number = result.pr_number or context.get("pr_number")
        if isinstance(pr_number, bool):
            pr_number = None
        if pr_number is not None:
            view = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "state,headRefOid"],
                cwd=repo_cwd, capture_output=True, text=True, timeout=30,
            )
            if view.returncode == 0:
                try:
                    pr = json.loads(view.stdout)
                    if pr.get("state") == "OPEN" and pr.get("headRefOid") == commit:
                        return True, f"open PR #{pr_number} pins reported commit"
                except (TypeError, ValueError):
                    pass
        return False, "reported commit is not reachable from origin or a linked open PR"
    except Exception as exc:  # git availability is evidence, not a bypass.
        return False, f"artifact verification unavailable: {type(exc).__name__}"


#: Declared completion contracts (#374; owner ruling alfred#51 c5776940407 §4).
#: The artifact a task must hand back depends on what it was asked to do: a
#: rebase rewrites history, so its commit cannot descend from the old dispatch
#: base; a read-only investigation must not commit at all. One commit rule for
#: every task rejected both structurally. `none` is deliberately absent — a
#: task with no artifact has nothing a gate can prove.
ARTIFACT_KIND_CONTEXT_KEY = "artifact_kind"
ARTIFACT_KINDS = ("commit", "rebase", "report", "review")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: A ref name git will not read as an option, a range or a path escape.
_SAFE_REF_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")


def declared_artifact_kind(task: Optional[TaskRequest]) -> Optional[str]:
    """The contract the task declared, or None when it declared none.

    Returned verbatim — an unsupported value is still "declared", so the gate
    applies and refuses it rather than treating a typo as "no contract".
    """
    context = task.context if task is not None and isinstance(task.context, dict) else {}
    if ARTIFACT_KIND_CONTEXT_KEY not in context:
        return None
    return str(context.get(ARTIFACT_KIND_CONTEXT_KEY) or "").strip().lower()


def artifact_gate_applies(task: Optional[TaskRequest], result: TaskResult) -> bool:
    """Whether a completion must prove its artifact before it is accepted.

    * A declared contract is always checked, whatever the task_type.
    * An undeclared task keeps exactly the #353 rule: an implement task with a
      recorded dispatch base must prove a commit. This change adds contracts;
      it does not loosen the default for anyone who did not declare one.
    """
    if task is None or result.status != "completed":
        return False
    if declared_artifact_kind(task) is not None:
        return True
    context = task.context if isinstance(task.context, dict) else {}
    return task.task_type == "implement" and bool(
        context.get("worktree_base_sha") or context.get("reviewed_sha"))


def verify_task_artifact(
    task: TaskRequest, result: TaskResult, *, repo_cwd: str,
    commit_verifier=None,
) -> tuple[bool, str]:
    """Check a completion against its declared contract (#374). Fail-closed.

    ``commit_verifier`` is the #353 check; callers pass the name they imported
    so the commit contract stays the one existing function, unchanged.
    """
    kind = declared_artifact_kind(task)
    if kind is None:            # undeclared → the #353 default; "" is declared, and refused
        kind = "commit"
    if kind == "commit":
        return (commit_verifier or verify_implement_artifact)(task, result, repo_cwd=repo_cwd)
    if kind == "rebase":
        return verify_rebase_artifact(task, result, repo_cwd=repo_cwd)
    if kind == "report":
        return verify_report_artifact(task, result, repo_cwd=repo_cwd)
    if kind == "review":
        return verify_review_artifact(task, result)
    return False, (f"unsupported artifact_kind {kind!r}; declare one of "
                   f"{', '.join(ARTIFACT_KINDS)}")


def _git(repo_cwd: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo_cwd, *args],
                          capture_output=True, text=True, timeout=timeout)


def _patch_ids(repo_cwd: str, rev_range: str) -> Optional[list]:
    """Stable patch-ids of every commit in ``rev_range``, oldest first.

    ``None`` if git cannot say. An empty commit has no patch-id, so it shows
    up as a *missing* id — which is exactly how the equivalence check below
    notices one being added or a real one being dropped.
    """
    revs = _git(repo_cwd, "rev-list", "--reverse", "--no-merges", rev_range)
    if revs.returncode != 0:
        return None
    ids = []
    for sha in revs.stdout.split():
        show = _git(repo_cwd, "show", "--format=", sha)
        if show.returncode != 0:
            return None
        pid = subprocess.run(["git", "-C", repo_cwd, "patch-id", "--stable"],
                             input=show.stdout, capture_output=True, text=True, timeout=30)
        if pid.returncode != 0:
            return None
        ids.append(pid.stdout.split()[0] if pid.stdout.strip() else None)
    return ids


def _diff_patch_id(repo_cwd: str, old: str, new: str) -> Optional[str]:
    """Stable patch-id of the whole change ``old..new`` (None if empty/unknown)."""
    diff = _git(repo_cwd, "diff", old, new)
    if diff.returncode != 0 or not diff.stdout.strip():
        return None
    pid = subprocess.run(["git", "-C", repo_cwd, "patch-id", "--stable"],
                         input=diff.stdout, capture_output=True, text=True, timeout=30)
    return pid.stdout.split()[0] if pid.returncode == 0 and pid.stdout.strip() else None


def verify_rebase_artifact(
    task: TaskRequest, result: TaskResult, *, repo_cwd: str,
) -> tuple[bool, str]:
    """A rebase is proven by carrying the SAME change onto the declared target.

    Location alone is not proof (review of 37cb8af, P1): any new commit on
    top of main passes "descends from origin/main and is pushed", including
    a force-push that threw the dispatched feature away. So:

    * the dispatched head — ``context.rebase_source_sha`` if the coordinator
      pinned one, else ``worktree_base_sha``, which dispatch records as the
      branch head it handed out — defines the change: its commits since it
      forked from the target;
    * the submitted commit must be pushed on ``origin/<branch>``, descend from
      ``origin/<rebase_onto>``, and add commits of its own;
    * and those commits must be patch-equivalent to the dispatched ones, one
      for one (``git patch-id --stable``), or — for a squash — the whole
      change must be. An added empty commit or a dropped one breaks the
      one-for-one match; a changed or unrelated diff breaks both.

    A rebase that needed conflict resolution changes the patch and fails
    here. That is deliberate: this gate cannot tell a resolution from a
    rewrite, and a human can.
    """
    context = task.context if isinstance(task.context, dict) else {}
    target = str(context.get("rebase_onto") or "").strip()
    branch = (result.branch or task.branch or "").strip()
    commit = (result.commit or "").strip()
    source = str(context.get("rebase_source_sha") or context.get("worktree_base_sha")
                 or "").strip()
    if not repo_cwd:
        return False, "artifact repository unavailable"
    if not target:
        return False, "rebase contract requires context.rebase_onto"
    if not branch:
        return False, "rebase contract requires the rebased branch"
    if not (_SAFE_REF_RE.match(target) and _SAFE_REF_RE.match(branch)):
        return False, "rebase target or branch is not a plain ref name"
    if branch == target:
        return False, "rebased branch is the rebase target"
    try:
        if _git(repo_cwd, "fetch", "origin", target, "--quiet", timeout=60).returncode != 0:
            return False, f"rebase target origin/{target} unavailable"
        # Fetching the branch first is what #374's second case lacked: after a
        # force-push the new head is not local until it is fetched.
        if _git(repo_cwd, "fetch", "origin", branch, "--quiet", timeout=60).returncode != 0:
            return False, f"rebased branch origin/{branch} is not pushed"
        head = _git(repo_cwd, "rev-parse", "--verify", f"origin/{branch}^{{commit}}")
        tip = _git(repo_cwd, "rev-parse", "--verify", f"origin/{target}^{{commit}}")
        if head.returncode != 0 or tip.returncode != 0:
            return False, "rebase refs are not resolvable after fetch"
        head_sha, tip_sha = head.stdout.strip(), tip.stdout.strip()
        if not commit:
            commit = head_sha
            result.commit = commit
        if _git(repo_cwd, "rev-parse", "--verify", f"{commit}^{{commit}}").returncode != 0:
            return False, "reported commit is not resolvable after fetching the pushed branch"
        if commit != head_sha and _git(
                repo_cwd, "merge-base", "--is-ancestor", commit, head_sha).returncode != 0:
            return False, f"reported commit is not on the pushed branch origin/{branch}"
        if commit == tip_sha:
            return False, f"reported commit is the origin/{target} tip (no artifact)"
        if source and commit == source:
            return False, "reported commit is the dispatch base (nothing was rewritten)"
        if _git(repo_cwd, "merge-base", "--is-ancestor", tip_sha, commit).returncode != 0:
            return False, f"reported commit does not descend from origin/{target}"

        # ── same content ────────────────────────────────────────────────
        if not source:
            return False, "rebase contract requires the dispatched head (worktree_base_sha)"
        if _git(repo_cwd, "rev-parse", "--verify", f"{source}^{{commit}}").returncode != 0:
            return False, "dispatched head is not resolvable; cannot prove the same content"
        fork = _git(repo_cwd, "merge-base", source, tip_sha)
        if fork.returncode != 0 or not fork.stdout.strip():
            return False, f"dispatched head shares no history with origin/{target}"
        fork_sha = fork.stdout.strip()
        before = _patch_ids(repo_cwd, f"{fork_sha}..{source}")
        after = _patch_ids(repo_cwd, f"{tip_sha}..{commit}")
        if before is None or after is None:
            return False, "patch-ids unavailable; cannot prove the same content"
        if not before or all(p is None for p in before):
            return False, f"dispatched head has no changes beyond origin/{target} to rebase"
        if not after or None in after:
            return False, "rebased range is empty or contains an empty commit"
        if after == [p for p in before if p is not None]:
            return True, (f"rebased {len(after)} commit(s) onto origin/{target} ({tip_sha[:12]}), "
                          f"patch-equivalent to dispatched {source[:12]}; pushed to origin/{branch}")
        whole_before = _diff_patch_id(repo_cwd, fork_sha, source)
        whole_after = _diff_patch_id(repo_cwd, tip_sha, commit)
        if whole_before and whole_before == whole_after and len(after) < len(before):
            return True, (f"squashed {len(before)}→{len(after)} commit(s) onto origin/{target}; "
                          f"same total change as dispatched {source[:12]}")
        return False, "rebased commits are not patch-equivalent to the dispatched change"
    except Exception as exc:  # git availability is evidence, not a bypass.
        return False, f"artifact verification unavailable: {type(exc).__name__}"


def verify_report_artifact(
    task: TaskRequest, result: TaskResult, *, repo_cwd: str,
) -> tuple[bool, str]:
    """A report is proven by content the server can hash, not by prose.

    ``result.artifact`` must carry ``sha256`` and either ``body`` (inline) or
    ``path`` (read at the reported commit from git, never from the
    filesystem). The content must be non-empty, match its hash, and match
    ``context.report_format`` (``text`` default, ``markdown``, ``json``).
    """
    context = task.context if isinstance(task.context, dict) else {}
    artifact = result.artifact if isinstance(result.artifact, dict) else None
    if artifact is None:
        return False, "report contract requires result.artifact {body|path, sha256}"
    sha = str(artifact.get("sha256") or "").strip().lower()
    if not _SHA256_RE.match(sha):
        return False, "report artifact requires a 64-hex sha256"
    body = artifact.get("body")
    path = str(artifact.get("path") or "").strip()
    if isinstance(body, str) and body:
        content, source = body, "inline body"
    elif path:
        commit = (result.commit or "").strip()
        branch = (result.branch or task.branch or "").strip()
        if not commit:
            return False, "report path requires the commit it was written at"
        if path.startswith("/") or ".." in path.split("/"):
            return False, "report path must be repository-relative"
        if not repo_cwd:
            return False, "artifact repository unavailable"
        try:
            if branch and _SAFE_REF_RE.match(branch):
                _git(repo_cwd, "fetch", "origin", branch, "--quiet", timeout=60)
            shown = _git(repo_cwd, "show", f"{commit}:{path}")
        except Exception as exc:
            return False, f"artifact verification unavailable: {type(exc).__name__}"
        if shown.returncode != 0:
            return False, f"report path {path!r} is not present at the reported commit"
        content, source = shown.stdout, f"{path}@{commit[:12]}"
    else:
        return False, "report artifact requires a non-empty body or a path"
    if not content.strip():
        return False, "report is empty"
    if "\x00" in content:
        return False, "report is not text"
    fmt = str(context.get("report_format") or "text").strip().lower()
    if fmt == "markdown":
        if not any(line.lstrip().startswith("#") for line in content.splitlines()):
            return False, "markdown report has no heading"
    elif fmt == "json":
        try:
            json.loads(content)
        except ValueError:
            return False, "json report does not parse"
    elif fmt != "text":
        return False, f"unsupported report_format {fmt!r}"
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != sha:
        return False, "report sha256 does not match its content"
    return True, f"report {len(content)} chars sha256={sha} ({source})"


def verify_review_artifact(task: TaskRequest, result: TaskResult) -> tuple[bool, str]:
    """A review is proven by a verdict bound to a PR.

    request_changes must say what to change, or the fix cascade has nothing
    to act on — that is the empty review the gate exists to catch.
    """
    context = task.context if isinstance(task.context, dict) else {}
    if result.verdict not in ("approve", "request_changes"):
        return False, "review contract requires verdict approve or request_changes"
    if result.pr_number is None and context.get("pr_number") in (None, "", False):
        return False, "review contract requires the reviewed pr_number"
    if result.verdict == "request_changes" and not [f for f in result.findings if str(f).strip()]:
        return False, "request_changes review has no findings"
    return True, f"review verdict {result.verdict}"


def no_artifact_result(result: TaskResult, detail: str) -> TaskResult:
    """Preserve the worker report while making the absent artifact terminal."""
    return TaskResult(
        task_id=result.task_id, status="failed",
        summary=f"[no_artifact] {detail}; worker summary: {result.summary}",
        verdict=result.verdict, findings=result.findings, pr_number=result.pr_number,
        branch=result.branch, commit=result.commit,
        error_info={"reason": "no_artifact", "detail": detail}, artifact=result.artifact,
    )


#: The spelling normaliser lives on the protocol type, because FastAPI has to
#: apply it at the boundary — a helper only this module could reach was one a
#: 422 fired before (review of PR #270). Aliased rather than re-implemented so
#: the endpoint and the cross-check can never drift apart on what "#268" means.
_as_pr_number = normalize_pr_number


def pr_number_mismatch(reported, requested) -> Optional[tuple]:
    """``(requested, reported)`` when the two name different PRs, else ``None``.

    #268 / alpha_engine#5288: a reviewer read PR #N+1, reviewed it honestly, and
    reported `pr_number=N+1` for a task dispatched against PR #N. `submit_result`
    resolved the pair with ``result.pr_number or ctx["pr_number"]`` — the
    result won outright, so the one moment the server could have caught the
    misread produced no signal at all.

    ⛔Silence on either side is *not* disagreement, and this is the load-bearing
      case: an implement task is dispatched with no PR and its result reports
      the PR it just opened. Requiring both sides would hold the most common
      hop in the crew. We only claim a mismatch when both sides actually named
      a PR and named different ones — an unparseable value means we could not
      cross-check, not that we found a conflict.
    """
    a = _as_pr_number(requested)
    b = _as_pr_number(reported)
    if a is None or b is None:
        return None
    return None if a == b else (a, b)


def hold_mismatched_pr_result(task_id: str, result: TaskResult,
                              ctx: Optional[dict]) -> tuple:
    """``(result, mismatch)`` — swap in a held result when the PRs disagree.

    The result is **stored, never rejected**. A 4xx would leave the row to time
    out and throw away the agent's own account of what it read — which is the
    single most useful artifact for diagnosing the misread. What stops is the
    cascade, not the audit trail: the caller sees a non-``None`` mismatch and
    skips the GitHub comment, the fix, the test and the merge.

    The status becomes ``needs_human`` for every reported status, `failed`
    included — rerouting a failure whose result named the wrong PR just spends
    another agent on the same confusion.
    """
    mismatch = pr_number_mismatch(result.pr_number,
                                  (ctx or {}).get("pr_number") if isinstance(ctx, dict) else None)
    if mismatch is None:
        return (result, None)
    requested, reported = mismatch
    logger.error(
        f"{PR_MISMATCH_MARKER}: task {task_id} was dispatched for PR #{requested} "
        f"but its result reports PR #{reported}. Holding the result for a human "
        f"and stopping the cascade — no comment, no fix, no test, no merge (#268). "
        f"requested_pr=#{requested} reported_pr=#{reported} "
        f"reported_status={result.status!r}"
    )
    note = (
        f"{PR_MISMATCH_MARKER}: this task was dispatched for PR #{requested}, "
        f"but the result reports PR #{reported}. The reported status was "
        f"{result.status!r}; held for a human because the two disagree and the "
        f"server cannot tell which PR the findings below are about (#268)."
    )
    return (
        replace(result, status="needs_human",
                summary=f"{note}\n\n{result.summary or ''}".rstrip()),
        mismatch,
    )


def pr_is_actionable(pr_number, *, pr_state_fn=None, repo: str = "",
                     repo_cwd: str = "") -> tuple:
    """``(actionable, state)`` — may the cascade still create work for this PR?

    A round budget bounds ONE lineage; it does not make the work useful. #250
    caught the difference: PR #241 merged at 01:42Z and the cascade kept
    completing reviews and posting "a human needs to decide" for another 13.8
    hours. Nothing produced after the merge could reach the artifact, but it
    still burned provider invocations and polluted the review-outcome metrics.

    Three answers, and the third is the one that needs stating:

      * no `pr_number` — actionable. A task can legitimately precede its PR
        (the first implement → review hop), and "no PR" is not "terminal PR".
      * `open` — actionable, unchanged behaviour.
      * `merged`/`closed`/**`unknown`** — not actionable.

    ⛔`unknown` blocks NEW work on purpose. When GitHub cannot be reached we do
      not know whether the PR is terminal, and #250 asks for deferral over
      speculative work in exactly that case: a skipped cascade is recoverable
      (the result is still persisted, and a human or a later task can resume
      it), whereas work spawned against a merged PR is unrecoverable spend. The
      asymmetry is the whole argument — this is not a general fail-closed rule.
    """
    if not pr_number:
        return (True, "no_pr")
    if pr_state_fn is None and not repo and not repo_cwd:
        logger.warning(
            f"pr_is_actionable: no repo identity for PR #{pr_number} (no explicit repo, "
            f"no worktree) — treating as unknown rather than inferring from process cwd"
        )
        return (False, "unknown")
    try:
        if pr_state_fn is not None:
            state = pr_state_fn(int(pr_number))
        else:
            from agent_crew.github import pr_state as _pr_state

            state = _pr_state(int(pr_number), repo=repo or None, cwd=repo_cwd or None)
    except Exception as e:  # noqa: BLE001 — a lookup never breaks a cascade
        logger.warning(f"pr_is_actionable: lookup failed for PR #{pr_number}: {e}")
        return (False, "unknown")
    return (state == "open", state or "unknown")


def _skip_terminal_pr(what: str, task_id: str, pr_number, *, pr_state_fn=None,
                      repo: str = "", repo_cwd: str = "") -> Optional[str]:
    """Shared guard for the cascade entry points.

    Returns the blocking state, or ``None`` when the cascade may proceed.
    """
    actionable, state = pr_is_actionable(pr_number, pr_state_fn=pr_state_fn,
                                         repo=repo, repo_cwd=repo_cwd)
    if actionable:
        return None
    if state == "unknown":
        # ⛔Louder than the terminal case, deliberately. "The PR is merged" is a
        #   correct, permanent stop; "we could not ask GitHub" is a stop that
        #   nothing retries, so it must be visible rather than look like normal
        #   cascade completion. #250 asks for deferral here, not for silence.
        logger.warning(
            f"{what}: could not determine the state of PR #{pr_number} — deferring "
            f"follow-up work for {task_id} rather than spending it on a "
            f"possibly-terminal PR. The task result is recorded; re-run the cascade "
            f"once GitHub is reachable (#250)."
        )
    else:
        logger.info(
            f"{what}: PR #{pr_number} is {state} — not creating follow-up work for "
            f"{task_id}. The task result is still recorded; only the cascade stops (#250)."
        )
    return state


def review_is_current(review_ctx: dict, pr_number, *, head_sha_fn=None,
                      repo: str = "", repo_cwd: str = "") -> tuple:
    """``(current, reason)`` — is this review's finding still about the PR head?

    A review examines a commit. By the time its result comes back, someone may
    have pushed — including the very fix the finding asks for. #253 measured
    this on PR #251: of seven review rounds, five produced fix tasks for work
    that already existed, each costing a reviewer and an implementer invocation
    to conclude "does not reproduce".

    ⛔The round cap cannot catch it. `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` counts
      rounds within ONE lineage, and every new review starts a fresh lineage at
      round 1 — two of those duplicates arrived labelled `3/3`, a spent budget,
      while the next review opened a new one. Only comparing the reviewed
      commit against the current head closes it.

    Three answers:

      * no `reviewed_sha` recorded — treated as current. Older tasks and
        producers that never went through worktree prep have none, and refusing
        to act on every one of them would break the pipeline to fix a subset;
      * head matches, or cannot be distinguished — see below;
      * head has moved — stale, and no work is created.
    """
    status, _head, why = review_head_status(
        review_ctx, pr_number, head_sha_fn=head_sha_fn, repo=repo, repo_cwd=repo_cwd)
    return (status in ("current", "unpinned"), why)


#: What a review's pin is, relative to the PR's live head.
#:
#: ``unpinned``  no pin or no PR to compare against — not staleness
#: ``current``   the pin IS the head
#: ``stale``     the head has moved since the review was prepared
#: ``unknown``   the head could not be read — never assumed to be either
ReviewHeadStatus = str


def review_head_status(
    review_ctx: dict | None,
    pr_number,
    *,
    head_sha_fn=None,
    repo: str = "",
    repo_cwd: str = "",
) -> tuple[str, str, str]:
    """Where a review sits relative to its PR's current head (#304).

    Returns ``(status, current_head, why)``. ``current_head`` is ``""`` unless
    the lookup actually produced one — a caller that wants to requeue against
    the new head needs to know it, and must not have to ask twice.

    ⛔One primitive, two call sites. :func:`review_is_current` gates the FIX
      CASCADE and is built on this; :func:`review_publication_decision` gates
      the VERDICT. #304 happened in the gap between them: the cascade gate
      existed and worked, and the verdict was published before anything
      consulted it. Two independent comparisons would drift, and the drift
      would be invisible until one of them published a verdict the other would
      have stopped.

    ⛔``unknown`` is a state, not a synonym for ``current``. Publishing a
      verdict because a head lookup failed is the same error as publishing one
      against a head that moved.
    """
    reviewed = (review_ctx or {}).get("reviewed_sha") or ""
    if not reviewed:
        return ("unpinned", "", "no reviewed_sha recorded")
    if not pr_number:
        return ("unpinned", "", "no pr_number to compare against")

    # ⛔The repository must be named, not inferred from the process's working
    #   directory. The server runs in the instance directory, which belongs to
    #   a DIFFERENT repository — an implicit repository lookup there answers a different slug, so
    #   the head lookup asks the wrong repo about this PR number and returns
    #   nothing (review of PR #255). Same root cause as the reviewer-branch
    #   resolution fixed in PR #251: `gh` inheriting a cwd nobody chose.
    repo = repo or (review_ctx or {}).get("repo") or ""
    if head_sha_fn is None and not repo and not repo_cwd:
        logger.warning(
            f"review_head_status: no repo known for PR #{pr_number} (review context "
            f"has no 'repo' and no worktree was supplied) — cannot compare the "
            f"reviewed commit. Fix the task context or pass repo_cwd."
        )
        return ("unknown", "", "no repo to compare against — cannot verify")
    try:
        if head_sha_fn is not None:
            head = head_sha_fn(int(pr_number)) or ""
        else:
            from agent_crew.github import pr_head_sha

            head = pr_head_sha(int(pr_number), repo=repo or None,
                               cwd=repo_cwd or None) or ""
    except Exception as e:  # noqa: BLE001 — a lookup never breaks a cascade
        logger.warning(f"review_head_status: head lookup failed for PR #{pr_number}: {e}")
        return ("unknown", "", "current head unknown")
    if not head:
        # ⛔Defer rather than guess, the same rule the terminal-PR gate uses: a
        #   skipped cascade is recoverable, and so is an unpublished verdict —
        #   the result itself is still recorded either way.
        return ("unknown", "", "current head unknown")
    if head == reviewed:
        return ("current", head, f"reviewed {reviewed[:9]} is still the head")
    return ("stale", head,
            f"reviewed {reviewed[:9]} but the head is now {head[:9]}")


@dataclass(frozen=True)
class ReviewPublication:
    """Whether a review's verdict may be posted, and what to do instead (#304)."""

    publish: bool
    status: str
    reason: str
    #: The head to requeue a review against, or ``""`` for "do not requeue".
    requeue_head: str = ""


def review_publication_decision(
    review_ctx: dict | None,
    pr_number,
    *,
    head_sha_fn=None,
    repo: str = "",
    repo_cwd: str = "",
) -> ReviewPublication:
    """May this verdict be posted to the PR, and against what (#304)?

    Reported downstream 2026-09-14: a review prepared at `5b496a9f` published
    `request_changes` while the PR head was `28419e96`, so a blocker was
    attributed to code the reviewer never read, and the PR moved on again with
    no head-anchored review anywhere.

    ⛔A verdict describes the commit that was READ. Posting it against a
      different one is a false statement about the code, not merely a stale
      one — which is why `stale` suppresses publication rather than annotating
      it.

    ⛔`stale` requeues; `unknown` does not. Not publishing is recoverable — the
      result is still recorded and a later head-anchored round can act on it.
      Requeueing against a head we failed to READ would spend a reviewer on a
      commit nobody can name, so the unverifiable case stops instead of
      guessing. The same asymmetry #253 rests on.
    """
    status, head, why = review_head_status(
        review_ctx, pr_number, head_sha_fn=head_sha_fn, repo=repo, repo_cwd=repo_cwd)
    if status in ("current", "unpinned"):
        return ReviewPublication(publish=True, status=status, reason=why)
    if status == "stale":
        return ReviewPublication(publish=False, status=status, reason=why,
                                 requeue_head=head)
    return ReviewPublication(publish=False, status=status, reason=why)


def stale_review_task_id(pr_number, head_sha: str) -> str:
    """The task id a requeued head-anchored review MUST have.

    ⛔Derived from the PR and the head, never random — the same idempotency
      mechanism as :func:`fix_task_id`. A result POST can arrive twice, and two
      random ids would put two reviewers on one commit. Two results observing
      the SAME new head produce one task; a head that moves again is a genuinely
      different review and gets its own.

    The head is readable in the id on purpose: an opaque hash cannot be traced
    back to the commit it was created for.
    """
    return f"review-{int(pr_number)}-{(head_sha or '')[:12]}"


def review_fix_max_rounds() -> int:
    """Cap on automated fix rounds. ``0`` disables the transition entirely.

    Read at call time rather than import time so an operator can change the
    limit (or switch the whole feature off) without restarting the server.
    """
    raw = (os.getenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS") or "").strip()
    if not raw:
        return DEFAULT_REVIEW_FIX_MAX_ROUNDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            f"AGENT_CREW_REVIEW_FIX_MAX_ROUNDS={raw!r} is not an integer — "
            f"using default {DEFAULT_REVIEW_FIX_MAX_ROUNDS}"
        )
        return DEFAULT_REVIEW_FIX_MAX_ROUNDS


def _record_risk_tier_shadow(queue: TaskQueue, task, actual_action: str) -> None:
    """Append a best-effort counterfactual receipt without changing work."""
    try:
        context = task.context if isinstance(task.context, dict) else {}
        receipt = shadow_decision(
            task.description, context, task.task_id, actual_action, review_fix_max_rounds(),
        )
        receipts = context.get("risk_tier_shadow")
        receipts = list(receipts) if isinstance(receipts, list) else []
        if receipt not in receipts:
            queue.patch_context(task.task_id, {"risk_tier_shadow": receipts + [receipt]})
    except Exception:
        logger.exception("risk-tier shadow receipt failed for %s", getattr(task, "task_id", "unknown"))


def fix_task_id(review_task_id: str, fix_round: int) -> str:
    """The task id a given review round's fix MUST have.

    ⛔Derived, not random, and that is the entire idempotency mechanism. A
      result POST can arrive twice — a retried submission, a duplicate delivery,
      an operator re-posting — and the cascade runs again in full each time. A
      random id made every replay a NEW task, so two implementers could be
      handed the same branch and the same findings concurrently (review of
      PR #245).

    Deriving the id puts the guard on the `tasks` PRIMARY KEY, where a race
    cannot slip through it. A "does one already exist?" check cannot do that
    job: two concurrent submissions both read "no" and both insert. Same
    reasoning as the #224 claim ledger, where the GitHub label could not be the
    mutex because `--add-label` is idempotent.

    ⛔The review id goes in VERBATIM, not as a truncated hash. The first
      version used `sha256(review_task_id)[:8]`, and 32 bits collide by
      birthday at roughly 65k reviews — at which point two unrelated reviews
      share a key and the second one's fix is silently skipped as "already
      exists". That is strictly worse than the duplicate this key was added to
      prevent: a duplicate is visible, a dropped fix is not (review of PR #245,
      round 2).

      Verbatim makes the mapping injective rather than merely improbable.
      `int()` renders the round as digits only, so the trailing `-r<digits>` is
      unambiguous and no two (review, round) pairs can produce the same string
      — `"-"` and `"r"` are not digits, so a shorter round cannot be misread as
      part of a longer one. Nothing constrains `task_id` length (TEXT PRIMARY
      KEY), and a longer id that is always correct beats a short one that is
      usually correct. It also reads: `fix-review-4be7401c-r1` says which
      review it came from without a lookup.
    """
    return f"fix-{review_task_id}-r{int(fix_round)}"


def _findings_block(findings: list, review_task_id: str) -> str:
    """Render review findings for the fix task description.

    Bounded — but never silently. A dropped finding is a defect the fix task
    would not know to address, so what was omitted is stated along with where
    to read the rest.
    """
    items = [str(f).strip() for f in (findings or []) if str(f).strip()]
    shown = items[:MAX_EMBEDDED_FINDINGS]
    lines = []
    for f in shown:
        if len(f) > MAX_FINDING_CHARS:
            f = (f[:MAX_FINDING_CHARS]
                 + f" [... truncated; full text in review task {review_task_id}]")
        lines.append(f"- {f}")
    if len(items) > len(shown):
        lines.append(
            f"- [... {len(items) - len(shown)} further findings omitted here — "
            f"read them all via GET /tasks/{review_task_id}]"
        )
    return "\n".join(lines)


def auto_enqueue_fix(
    queue: TaskQueue,
    review_task_id: str,
    *,
    pane_map: Optional[dict] = None,
    server_project: Optional[str] = None,
    comment_fn=None,
    pr_state_fn=None,
    already_announced_fn=None,
    head_sha_fn=None,
    repo: str = "",
    repo_cwd: str = "",
    suppress_side_effects: bool = False,
) -> Optional[str]:
    """Create the fix task that follows a ``request_changes`` review (#244).

    #314 §4 P0: ``suppress_side_effects`` (replay 경로) 시 fix-budget 소진 PR comment 게시
    (_announce_fix_budget_exhausted)를 skip한다. pr_announcements claim이 동시 중복엔 강하나
    comment 성공→posted_at 기록 전 crash 구간은 exactly-once가 아니므로, GitHub feedback이 root
    cause에 포함된 이번 사고에서는 replay에서 announcement를 아예 수행하지 않는다. fix task enqueue
    (결정론 id, 멱등)는 durable transition이므로 계속 수행된다.

    The cascade had transitions for `implement completed → review` and
    `review approve → test`, but the rejection path just ended. Every
    multi-round review therefore needed an operator to hand-enqueue the fix
    with the findings pasted in — `crew triage --watch` automated the first
    claim and nothing after it.

    Returns the new implement task_id, or ``None`` when no fix is created.
    The ``None`` cases are deliberate and each closes a different way this
    transition could misbehave:

      * the review task itself failed — `_resolve_verdict` maps a crashed or
        timed-out review to `request_changes` so a broken review can never
        silently approve (#100), but that is not a fix request: there are no
        findings, and the failure path already retries or falls back. Spawning
        a fix here would double-handle it AND hand an agent nothing to do;
      * the reviewer requested changes without stating anything actionable;
      * `coordinator_managed` — `crew run` drives its own loop;
      * cross-project, as in `auto_enqueue_review`;
      * the round cap is reached;
      * a fix task for this review round already exists. The transition is
        idempotent per review round: a replayed result POST produces no second
        task, and the caller gets ``None`` because nothing NEW was created.
        Note this holds regardless of the existing task's state — including a
        cancelled one, so an operator's explicit cancel is not quietly undone
        by a duplicate delivery.

    Callers swallow the ``None`` — auto-enqueue must never crash a result
    submission.
    """
    try:
        review_tasks = [t for t in queue.list_tasks() if t.task_id == review_task_id]
        if not review_tasks:
            return None
        review_task = review_tasks[0]

        review_result = queue.get_result(review_task_id)
        if not review_result:
            return None
        if getattr(review_result, "status", None) != "completed":
            logger.info(
                f"auto_enqueue_fix: review {review_task_id} did not complete "
                f"(status={getattr(review_result, 'status', None)!r}) — leaving it "
                f"to the retry/fallback path, not enqueueing a fix"
            )
            return None
        if _resolve_verdict(review_result) != "request_changes":
            return None

        review_ctx = review_task.context if isinstance(review_task.context, dict) else {}
        # Same guard as the other two transitions: `crew run`'s foreground loop
        # enqueues its own follow-ups, and a second one here would race it.
        # Checked here rather than only in the caller so the MCP transport gets
        # the guard too (#123 — both transports run the same cascade).
        if review_ctx.get("coordinator_managed"):
            logger.info(
                f"auto_enqueue_fix: review {review_task_id} is coordinator_managed "
                f"— skipping"
            )
            return None

        review_project = review_task.project
        if review_project and server_project and review_project != server_project:
            logger.warning(
                f"auto_enqueue_fix: skipping cross-project fix — review "
                f"project={review_project!r}, server project={server_project!r}"
            )
            return None

        pr_number = review_result.pr_number or review_ctx.get("pr_number")
        # #250: a terminal PR ends the cascade regardless of the round budget.
        # Checked BEFORE the budget so an exhausted lineage on a merged PR stays
        # silent instead of announcing itself to an already-decided artifact.
        if _skip_terminal_pr("auto_enqueue_fix", review_task_id, pr_number,
                             pr_state_fn=pr_state_fn, repo=repo, repo_cwd=repo_cwd):
            return None

        # #253: the finding has to be about the CURRENT code, not about a state
        # that has already been fixed. Checked before the round budget so a
        # stale review neither spends a round nor announces anything.
        current, why = review_is_current(
            review_ctx, pr_number, head_sha_fn=head_sha_fn,
            repo=repo or "", repo_cwd=repo_cwd or "")
        if not current:
            logger.info(
                f"auto_enqueue_fix: {review_task_id} reviewed a state that is no longer "
                f"current ({why}) — not creating follow-up work. The review result is "
                f"still recorded; only the cascade stops (#253)."
            )
            return None

        # Council #39 A-4: the configured value remains a hard ceiling, while
        # low-risk work stops once an additional fix is less valuable than its
        # independent review/test cost.
        if not risk_tier_enforcement_enabled():
            _record_risk_tier_shadow(queue, review_task, "fix_legacy_cap")
        max_rounds = effective_fix_round_cap(review_ctx, review_fix_max_rounds())
        # The lineage counter rides in the task context, so it survives a
        # server restart and counts ROUNDS rather than tasks. An in-memory
        # per-task_id counter (the transient-retry shape) could not work here:
        # every round mints new task ids, so it would always read zero.
        fix_round = int(review_ctx.get("fix_round") or 0) + 1
        if max_rounds <= 0 or fix_round > max_rounds:
            logger.warning(
                f"auto_enqueue_fix: review {review_task_id} requested changes but "
                f"the automated fix budget is spent (round {fix_round} > "
                f"max {max_rounds}) — stopping, this needs a human"
            )
            # #314 §4 P0: replay 중에는 exhaustion PR comment를 게시하지 않는다(중복 방지).
            if suppress_side_effects:
                logger.debug(f"auto_enqueue_fix: replay 중 — fix-budget exhaustion comment skip "
                             f"(review {review_task_id})")
                return None
            # #314 재리뷰: fix-budget comment도 GitHub 외부 mutation → merge/review comment와 동일한
            # 원자 STOP admission. external_op_reserve가 같은 txn에서 runtime_stop 확인 후 admit할 때만
            # 게시(STOP이 먼저 linearize되면 차단). receipt(done)로 게시→기록 전 crash 중복도 방지.
            _cop = f"comment:fixbudget:{review_task_id}"
            _cresv = queue.external_op_reserve(
                _cop, pr_number=pr_number if isinstance(pr_number, int) else None)
            if not _cresv.get("admitted"):
                logger.warning(f"auto_enqueue_fix: fix-budget comment 억제 — STOP admission 거부 "
                               f"(review {review_task_id}, {_cresv.get('state')})")
            elif _cresv.get("state") == "done":
                logger.info(f"auto_enqueue_fix: fix-budget comment 이미 done(receipt) — skip")
            else:
                _announce_fix_budget_exhausted(
                    pr_number=pr_number, review_task_id=review_task_id,
                    max_rounds=max_rounds, findings=review_result.findings or [],
                    comment_fn=comment_fn, already_announced_fn=already_announced_fn,
                    queue=queue, repo=repo, repo_cwd=repo_cwd)
                try:
                    queue.external_op_mark(_cop, "done")
                except Exception:
                    logger.exception(f"auto_enqueue_fix: external_op_mark({_cop}) 실패")
            return None

        findings_text = _findings_block(review_result.findings or [], review_task_id)
        summary = (review_result.summary or "").strip()
        if not findings_text and not summary:
            logger.warning(
                f"auto_enqueue_fix: review {review_task_id} requested changes with "
                f"neither findings nor a summary — nothing to act on, skipping"
            )
            return None

        where = (f"PR #{pr_number}" if pr_number
                 else f"branch {review_task.branch!r}")
        parts = [
            f"Fix {where} per {review_task_id} request_changes "
            f"(automated fix round {fix_round}/{max_rounds}).",
        ]
        if summary:
            parts.append(f"\nReviewer summary: {summary}")
        if findings_text:
            parts.append(f"\nFindings to address:\n{findings_text}")
        parts.append(
            f"\nCommit to the SAME branch {review_task.branch!r} — do not open a "
            f"new PR. Reproduce each finding before fixing it, and say so if one "
            f"does not reproduce."
        )

        fix_context: dict = {
            "prev_task_id": review_task_id,
            "fix_round": fix_round,
            "review_findings": list(review_result.findings or []),
        }
        if pr_number is not None:
            # #186: lets the dispatcher check out the PR head for this task.
            fix_context["pr_number"] = pr_number
        for key in ("no_tester", "issue", "issue_title", "issue_body",
                    "issue_url", "repo"):
            if review_ctx.get(key) is not None:
                fix_context[key] = review_ctx[key]
        implementer_agent = (
            review_ctx.get("implementer_agent")
            or (default_agent_for_role("implementer", pane_map) if pane_map else None)
        )
        if implementer_agent:
            fix_context["implementer_agent"] = implementer_agent

        fix_id = fix_task_id(review_task_id, fix_round)
        # Cheap early-out with a legible log. It is NOT the guard — the
        # `enqueue` below is, because only the PRIMARY KEY is atomic.
        existing = next((t for t in queue.list_tasks() if t.task_id == fix_id), None)
        if existing is not None:
            logger.info(
                f"auto_enqueue_fix: {fix_id} already exists for {review_task_id} "
                f"(status={existing.status!r}) — this review round already has "
                f"its fix task, not enqueueing another"
            )
            return None
        try:
            queue.enqueue(TaskRequest(
                task_id=fix_id,
                task_type="implement",  # type: ignore[arg-type]
                description="\n".join(parts),
                branch=review_task.branch,
                context=fix_context,
                project=review_project,
            ),
                          ingress="cascade.fix")
        except (sqlite3.IntegrityError, TaskAlreadyExistsError):
            # A concurrent submission (or replay 재실행) won the insert. That is the
            # mechanism working, not an error: exactly one fix task exists.
            logger.info(
                f"auto_enqueue_fix: {fix_id} was created concurrently for "
                f"{review_task_id} — leaving the winner in place"
            )
            return None
        logger.info(
            f"auto_enqueue_fix: enqueued {fix_id} for {review_task_id} "
            f"(round {fix_round}/{max_rounds})"
        )
        return fix_id
    except PausedError:
        # #314 §4 P0-2: STOP race — 부모(review) outbox reopen + 전파.
        try:
            queue.outbox_reopen(review_task_id)
        except Exception:
            logger.exception(f"auto_enqueue_fix: outbox_reopen({review_task_id}) 실패")
        raise
    except Exception as e:
        logger.warning(f"auto_enqueue_fix: unexpected error: {e}")
        return None


def _announce_fix_budget_exhausted(*, pr_number, review_task_id: str,
                                   max_rounds: int, findings: list,
                                   comment_fn=None, already_announced_fn=None,
                                   queue=None, repo: str = "", repo_cwd: str = "") -> None:
    """Say on the PR that automation has stopped. Best-effort, never raises.

    ⛔A silent stop is the worst outcome available here: the PR would simply go
      quiet after a rejection and look like it was still being worked on.

    ⛔Said ONCE per PR. #250 found 25 of these on PR #241, seven inside three
      minutes: every late or duplicate review result whose lineage was already
      over budget announced the same exhaustion again. The message is about the
      PR, not about the individual review task, so repeating it adds nothing and
      buries the one that mattered. When we cannot check (GitHub unreachable) we
      post — a missing escalation is worse than a duplicate one, and unlike
      spawning work a comment costs no provider invocation.
    """
    if not pr_number:
        return
    pr_number = int(pr_number)
    from agent_crew.github import get_repo
    _fix_repo = repo or (get_repo(cwd=repo_cwd) if repo_cwd else "") or ""

    # ⛔The claim comes FIRST, and it is a row, not a question. Asking GitHub
    #   "is the notice already there?" and then posting is check-then-act: two
    #   results completing together both read "no" and both post. Reproduced
    #   before fixing — two notices for one PR. This is the same reasoning that
    #   made the fix task id derived rather than checked in #244, applied to the
    #   thing #250 added right next to it.
    claim_token = ""
    claimed = True
    if queue is not None:
        try:
            claim_token = queue.claim_pr_announcement(
                pr_number, FIX_EXHAUSTED_KIND, claimed_by=review_task_id) or ""
            claimed = bool(claim_token)
        except Exception as e:  # noqa: BLE001 — telemetry never breaks a cascade
            logger.warning(f"auto_enqueue_fix: announcement claim failed: {e}")
            claim_token = ""
            claimed = True          # fall back to best-effort, never go silent
    if not claimed:
        logger.info(
            f"auto_enqueue_fix: another result already owns the exhaustion notice "
            f"for PR #{pr_number} — not repeating it for {review_task_id} (#250)"
        )
        return

    # Second line of defence, and the only one that sees OTHER crews: the claim
    # table is per-database, so a separate crew sharing this PR would not be in
    # it. Best-effort by nature — `None` means "could not tell", and we post.
    try:
        if already_announced_fn is not None:
            seen = already_announced_fn(pr_number, FIX_EXHAUSTED_MARKER)
        else:
            from agent_crew.github import pr_has_comment_containing

            seen = pr_has_comment_containing(pr_number, FIX_EXHAUSTED_MARKER, repo=_fix_repo)
    except Exception as e:  # noqa: BLE001
        # ⛔A failing best-effort check must not swallow the escalation. It is
        #   the same rule as `None`: when we cannot tell, we post.
        logger.warning(f"auto_enqueue_fix: prior-notice check failed for PR "
                       f"#{pr_number}: {e}")
        seen = None
    if seen is True:
        logger.info(
            f"auto_enqueue_fix: PR #{pr_number} already carries an exhaustion notice "
            f"— not repeating it for {review_task_id} (#250)"
        )
        # Keep the claim and mark it done: someone posted, so nobody should
        # post again, and releasing here would re-open the race on the next result.
        if queue is not None and claim_token:
            try:
                queue.mark_pr_announcement_posted(
                    pr_number, FIX_EXHAUSTED_KIND, claim_token)
            except Exception:  # noqa: BLE001
                pass
        return
    body = (
        f"{FIX_EXHAUSTED_MARKER} "
        f"(AGENT_CREW_REVIEW_FIX_MAX_ROUNDS={max_rounds}).\n\n"
        f"The reviewer still requests changes after {max_rounds} automated "
        f"round(s), so the loop has stopped rather than spend another one. "
        f"Latest review task: `{review_task_id}`.\n\n"
        + ("**Outstanding findings:**\n"
           + "\n".join(f"- {str(f)[:MAX_FINDING_CHARS]}" for f in findings[:MAX_EMBEDDED_FINDINGS])
           if findings else "")
        + "\n\nA human needs to decide the next move."
    )
    # ⛔Last check before the external side effect. Everything above may have
    #   taken time — a GitHub read, a large findings render — and a lease that
    #   expired in the meantime means someone else now owns this notice. A
    #   comment cannot be un-posted, so a worker that has lost the claim must
    #   not post at all, rather than post and discover the loss afterwards.
    if queue is not None and claim_token:
        try:
            if not queue.owns_pr_announcement(pr_number, FIX_EXHAUSTED_KIND, claim_token):
                logger.warning(
                    f"auto_enqueue_fix: lost the exhaustion-notice claim for PR "
                    f"#{pr_number} before posting (lease taken over) — not posting "
                    f"for {review_task_id}"
                )
                return
        except Exception:  # noqa: BLE001
            pass            # cannot verify ownership → fall through and post

    if not _fix_repo:
        logger.warning(
            f"auto_enqueue_fix: canonical repo identity unresolved — not posting the "
            f"fix-budget notice for PR #{pr_number}, mutation 0"
        )
        if queue is not None and claim_token:
            try:
                queue.release_pr_announcement(pr_number, FIX_EXHAUSTED_KIND, claim_token)
            except Exception:  # noqa: BLE001
                pass
        return

    try:
        if comment_fn is not None:
            comment_fn(pr_number, body)
        else:
            from agent_crew.github import post_pr_comment

            if not post_pr_comment(pr_number, body, repo=_fix_repo):
                raise RuntimeError(
                    f"post_pr_comment returned False for PR #{pr_number} "
                    f"(gh not installed, repo unresolved, or gh exited non-zero)"
                )
    except Exception as e:  # noqa: BLE001
        # ⛔Give the claim back. A failed post that kept its claim would
        #   suppress the escalation forever — the PR would go quiet, which is
        #   the outcome this notice exists to prevent.
        logger.warning(f"auto_enqueue_fix: could not comment on PR #{pr_number}: {e}")
        if queue is not None and claim_token:
            try:
                queue.release_pr_announcement(
                    pr_number, FIX_EXHAUSTED_KIND, claim_token)
            except Exception:  # noqa: BLE001
                pass
        return
    if queue is not None and claim_token:
        try:
            if not queue.mark_pr_announcement_posted(
                    pr_number, FIX_EXHAUSTED_KIND, claim_token):
                # We posted, but the row is no longer ours. Say so: this is the
                # one case where a duplicate notice can still reach the PR, and
                # a silent log would hide it from whoever investigates.
                logger.warning(
                    f"auto_enqueue_fix: posted the exhaustion notice for PR "
                    f"#{pr_number} but the claim had already been taken over — "
                    f"a duplicate notice is possible"
                )
        except Exception:  # noqa: BLE001
            pass


def auto_enqueue_review(
    queue: TaskQueue,
    impl_task_id: str,
    pr_number: Optional[int] = None,
    *,
    pane_map: Optional[dict] = None,
    server_project: Optional[str] = None,
    pr_state_fn=None,
    result=None,
    repo_cwd: str = "",
) -> Optional[str]:
    """Create the review task that follows a completed impl task.

    ``result`` is the implementer's own report (#305). When it names a branch
    that differs from the task's, THAT is where the work is and the review is
    routed there — see ``_review_target`` below.

    Returns the new review task_id, or ``None`` when no review is created
    (cross-project guard, missing impl task, exception). Callers swallow
    the None — auto-enqueue must never crash a result submission.
    """
    try:
        impl_tasks = [t for t in queue.list_tasks() if t.task_id == impl_task_id]
        if not impl_tasks:
            return None
        impl_task = impl_tasks[0]
        impl_ctx = impl_task.context if isinstance(impl_task.context, dict) else {}
        enforce_risk_tier = risk_tier_enforcement_enabled()
        risk = cascade_metadata(impl_task.description, impl_ctx)
        # A review→fix lineage created before Council #39 has no tier receipt.
        # Do not retroactively alter its already-running cap halfway through.
        # Fresh implementation tasks receive the classification below.
        legacy_fix_lineage = "fix_round" in impl_ctx and "risk_tier" not in impl_ctx
        if legacy_fix_lineage:
            risk = {}
            tier = TIER_2
        else:
            tier = risk["risk_tier"]
        if not enforce_risk_tier:
            _record_risk_tier_shadow(queue, impl_task, "review_enqueued")
        # Tier 0 is intentionally implement-only. It remains observable via
        # its task/result and can still be manually reviewed by an operator.
        if enforce_risk_tier and tier == TIER_0:
            logger.info("auto_enqueue_review: Tier 0 task %s is implement-only", impl_task_id)
            return None
        # Tier 3 contains irreversible/external work. Do not make a new worker
        # runnable until a human resolves the durable approval gate.
        if enforce_risk_tier and tier == TIER_3 and not impl_ctx.get("tier3_gate_approved"):
            gate_id = f"risk-tier3-{impl_task_id}"
            if not any(g.id == gate_id for g in queue.list_gates()):
                queue.create_gate(GateRequest(
                    id=gate_id, type="approval",
                    message=(f"Tier 3 human/independent gate for {impl_task_id}: "
                             "irreversible or external work must be explicitly approved "
                             "before review/test continuation."),
                ))
            logger.warning("auto_enqueue_review: Tier 3 task %s held at %s", impl_task_id, gate_id)
            return None
        from agent_crew.github import get_repo
        _impl_repo = (impl_ctx.get("repo") or "") or (
            get_repo(cwd=repo_cwd) if repo_cwd else "") or ""

        # #305: route to where the implementer actually pushed, not to the
        # branch the TASK happened to name.
        # ⛔A watch-ingested issue task carries `branch: main` and no PR,
        #   because an issue has no branch. The implementer creates one and
        #   says so; before this the cascade never looked, so the reviewer was
        #   told to `gh pr list --head main`, found nothing, and returned an
        #   accurate `request_changes` about routing that then drove a fix
        #   round against `main` (measured 2026-09-14, review-9aeb0354).
        #
        # ⛔Only overrides what the result actually reports. No existing agent
        #   fills these fields — they did not exist until #305 — so a result
        #   that says nothing must leave routing byte-for-byte as it was.
        _pushed_branch = (getattr(result, "branch", "") or "").strip()
        _pushed_commit = (getattr(result, "commit", "") or "").strip()
        if _pushed_branch and _pushed_branch != impl_task.branch:
            logger.info(
                f"auto_enqueue_review: {impl_task_id} pushed to {_pushed_branch!r} "
                f"but its task named {impl_task.branch!r} — routing the review to "
                f"the pushed branch (#305)"
            )
            impl_task = replace(impl_task, branch=_pushed_branch)

        # ⛔The PR this task is about is not necessarily the one the RESULT
        #   names. Both transports pass `result.pr_number`, and an agent may
        #   simply omit it — while the task context has carried the PR since it
        #   was created. Gating on the argument alone therefore read "no PR"
        #   for a task whose PR was merged, and cheerfully queued a review of
        #   a closed artifact (review of PR #251). Resolve the effective PR
        #   once, and use that everywhere below: the gate, the freshness
        #   directive, and the review context the next hop is gated on.
        if pr_number is None:
            ctx_pr = impl_ctx.get("pr_number")
            if isinstance(ctx_pr, int) or (isinstance(ctx_pr, str) and ctx_pr.isdigit()):
                pr_number = int(ctx_pr)
                logger.info(
                    f"auto_enqueue_review: {impl_task_id} reported no pr_number; "
                    f"using #{pr_number} from the task context"
                )

        # #250: reviewing a merged/closed PR cannot change the artifact.
        if _skip_terminal_pr("auto_enqueue_review", impl_task_id, pr_number,
                             pr_state_fn=pr_state_fn, repo=_impl_repo, repo_cwd=repo_cwd):
            return None

        # #161: no-PR guard — if the impl task has neither a branch nor a
        # pr_number, there is nothing for the reviewer to locate. Retrying
        # will also fail, creating an unbounded loop. Skip auto-review
        # and log so the operator can investigate.
        if not impl_task.branch and pr_number is None:
            logger.warning(
                f"auto_enqueue_review: skipping — impl task {impl_task_id} has "
                f"no branch and no pr_number; reviewer has nothing to find (#161)"
            )
            return None

        # Cross-project guard: if the impl task carries a top-level project tag
        # and the server was started for a different project, skip auto-review
        # to prevent misrouting tasks across project queues.
        impl_project = impl_task.project
        if impl_project and server_project and impl_project != server_project:
            logger.warning(
                f"auto_enqueue_review: skipping cross-project review — "
                f"impl project={impl_project!r}, server project={server_project!r}"
            )
            return None

        # Build a freshness directive that is unambiguous about reviewing
        # the live PR HEAD, not a stale local copy or an earlier round.
        if pr_number is not None:
            pr_directive = (
                f"\n\nFRESHNESS: review PR #{pr_number} at its CURRENT head. "
                f"Run `gh pr diff {pr_number}` (and/or "
                f"`gh pr view {pr_number} --json commits`) FIRST. Do NOT "
                f"reuse line numbers from any earlier review round — they "
                f"reference the prior commit. Pin every finding to the "
                f"latest commit's file:line."
            )
        else:
            pr_directive = (
                f"\n\nFRESHNESS: identify the PR for branch "
                f"{impl_task.branch!r} via `gh pr list --head "
                f"{impl_task.branch}`, then `gh pr diff <num>` to fetch "
                f"the live head before pinning findings. Do NOT review "
                f"from a stale local copy."
            )

        # Identify which agent actually implemented this task. Prefer the
        # explicit override (set by upstream fallback), else fall back to
        # the role's default mapping. Recorded so the rate-limit fallback
        # handler can skip it during reviewer selection (#117 — self-review
        # prevention).
        implementer_agent = (
            impl_ctx.get("agent_override")
            or (default_agent_for_role("implementer", pane_map) if pane_map else None)
        )

        review_context = {
            "checklist_layers": ["test_quality", "code_quality", "business_gap"],
            "reviewer_rejects_happy_path_only": True,
            "instructions": (
                "3-layer review: "
                "1) test_quality — coverage, edge cases, mocks; "
                "2) code_quality — naming, error handling, SOLID; "
                "3) business_gap — requirements met, logging, observability."
                + pr_directive
            ),
            "prev_task_id": impl_task_id,
            "pr_number": pr_number,
        }
        if enforce_risk_tier:
            review_context.update(risk)
        if enforce_risk_tier and impl_ctx.get("tier3_gate_approved"):
            review_context["tier3_gate_approved"] = True
        if enforce_risk_tier and tier == TIER_2:
            review_context["review_mode"] = "adversarial"
            review_context["instructions"] += "\n\nTier 2: perform an adversarial independent review; actively seek regression and safety gaps."
        if implementer_agent:
            review_context["implementer_agent"] = implementer_agent
        if impl_ctx.get("no_tester"):
            review_context["no_tester"] = True
        # #244: carry the fix-round counter along the lineage. Without this the
        # counter resets every time a fix task produces a fresh review, and the
        # cap that makes review→fix safe to automate would never be reached.
        for key in ("fix_round", "issue", "issue_title", "issue_body", "issue_url"):
            if impl_ctx.get(key) is not None:
                review_context[key] = impl_ctx[key]
        if _impl_repo:
            review_context["repo"] = _impl_repo

        # #305: pin the review to the commit the implementer actually pushed.
        # ⛔Only when it is a real object id — `TaskResult` normalises anything
        #   else away, so a report of `"HEAD"` leaves the task's own pin alone
        #   rather than replacing it with a string no consumer can compare.
        #   #304 compares `reviewed_sha` for equality against a live head, and
        #   a junk pin there would read as "the head moved" forever.
        if _pushed_commit:
            review_context["reviewed_sha"] = _pushed_commit

        # #164: compact review description — avoid re-injecting the full
        # original spec into the reviewer's context. The reviewer should
        # use get_task(prev_task_id) when the full spec is needed.
        if pr_number is not None:
            compact_desc = f"Review PR #{pr_number} for task {impl_task_id}."
        else:
            compact_desc = (
                f"Review branch {impl_task.branch!r} for task {impl_task_id}."
            )

        # #314 §4 P0-1: 결정론 successor id(stable transition key) — UUID 금지. crash 후 replay가
        # 같은 (impl parent, review, fix_round)에 대해 동일 id를 만들어 PK dedup으로 at-most-once.
        _round = int(impl_ctx.get("fix_round", 0) or 0)
        review_id = f"review-{impl_task_id}-r{_round}"
        review_req = TaskRequest(
            task_id=review_id,
            task_type="review",  # type: ignore[arg-type]
            description=compact_desc,
            branch=impl_task.branch,
            context=review_context,
            project=impl_project,
        )
        try:
            queue.enqueue(review_req, ingress="cascade.review")
        except TaskAlreadyExistsError:
            # 이미 생성됨(replay 재실행/중복 cascade) → 멱등 no-op.
            logger.info(f"auto_enqueue_review: {review_id} 이미 존재 — 멱등 skip")
        return review_id
    except PausedError:
        # #314 §4 P0-2: STOP race — successor enqueue가 원자 거부됨. 부모(impl) outbox를 reopen해
        # 재개 후 result-carrying replay로 이 review를 복구하고, PausedError를 전파(handler suppressed 200).
        try:
            queue.outbox_reopen(impl_task_id)
        except Exception:
            logger.exception(f"auto_enqueue_review: outbox_reopen({impl_task_id}) 실패")
        raise
    except Exception as e:
        logger.warning(f"auto_enqueue_review: unexpected error: {e}")
        return None


def auto_enqueue_test(
    queue: TaskQueue,
    review_task_id: str,
    *,
    pane_map: Optional[dict] = None,
    pr_state_fn=None,
    repo: str = "",
    repo_cwd: str = "",
) -> Optional[str]:
    """Create the test task that follows an approved review.

    Returns the new test task_id, or ``None`` when no test is created
    (review missing/rejected, exception).
    """
    try:
        review_tasks = [t for t in queue.list_tasks() if t.task_id == review_task_id]
        if not review_tasks:
            return None
        review_task = review_tasks[0]

        # Use the defensive verdict resolver from loop.py so reviewers that
        # post `verdict=null` with empty findings still trip the auto-test
        # (#100).
        review_result = queue.get_result(review_task_id)
        if not review_result:
            return None
        if _resolve_verdict(review_result) != "approve":
            return None

        # Propagate upstream agent identities so review/test fallback can
        # avoid self-review and self-test (#117).
        review_ctx = review_task.context if isinstance(review_task.context, dict) else {}
        enforce_risk_tier = risk_tier_enforcement_enabled()
        tier = classify_task(review_task.description, review_ctx)
        if not enforce_risk_tier:
            _record_risk_tier_shadow(queue, review_task, "test_enqueued")
        if enforce_risk_tier and tier == TIER_0:
            return None
        # An approved Tier 3 test gate replays this exact transition.  The
        # durable receipt lives on the reviewed task, so a restart/replay does
        # not create a second gate or strand the already-approved lineage.
        if enforce_risk_tier and tier == TIER_3 and not review_ctx.get("tier3_gate_approved"):
            gate_id = f"risk-tier3-test-{review_task_id}"
            if not any(g.id == gate_id for g in queue.list_gates()):
                queue.create_gate(GateRequest(
                    id=gate_id, type="approval",
                    message=(f"Tier 3 human/independent gate for {review_task_id}: "
                             "approve before test continuation."),
                ))
            return None
        from agent_crew.github import get_repo
        _test_repo = repo or (review_ctx.get("repo") or "") or (
            get_repo(cwd=repo_cwd) if repo_cwd else "") or ""
        implementer_agent = review_ctx.get("implementer_agent")
        reviewer_agent = (
            review_ctx.get("agent_override")
            or (default_agent_for_role("reviewer", pane_map) if pane_map else None)
        )

        pr_number = review_ctx.get("pr_number")
        # #250: same gate — a merged PR does not need testing on our account.
        if _skip_terminal_pr("auto_enqueue_test", review_task_id, pr_number,
                             pr_state_fn=pr_state_fn, repo=_test_repo, repo_cwd=repo_cwd):
            return None
        test_context: dict = {"prev_task_id": review_task_id}
        if enforce_risk_tier:
            test_context.update({
                "risk_tier": tier,
                "risk_tier_source": review_ctx.get("risk_tier_source", "metadata"),
            })
        if enforce_risk_tier and tier == TIER_1:
            # #272's tester consumes this as an explicit treatment rather than
            # guessing scope from the project/provider.
            test_context["test_scope"] = "targeted"
            test_context["test_scope_source"] = "risk_tier"
        if pr_number is not None:
            test_context["pr_number"] = pr_number  # #171: propagate for post-test merge
        if _test_repo:
            test_context["repo"] = _test_repo
        if implementer_agent:
            test_context["implementer_agent"] = implementer_agent
        if reviewer_agent:
            test_context["reviewer_agent"] = reviewer_agent

        # #164: compact test description — reviewer can fetch full spec via
        # get_task(prev_task_id) chain if needed.
        if pr_number is not None:
            compact_desc = f"Test PR #{pr_number} for reviewed task {review_task_id}."
        else:
            compact_desc = (
                f"Test branch {review_task.branch!r} for reviewed task {review_task_id}."
            )

        # #314 §4 P0-1: 결정론 successor id — review당 test 1개(review id는 이미 round별 결정론).
        test_id = f"test-{review_task_id}"
        test_req = TaskRequest(
            task_id=test_id,
            task_type="test",  # type: ignore[arg-type]
            description=compact_desc,
            branch=review_task.branch,
            context=test_context,
        )
        try:
            queue.enqueue(test_req, ingress="cascade.test")
        except TaskAlreadyExistsError:
            logger.info(f"auto_enqueue_test: {test_id} 이미 존재 — 멱등 skip")
        return test_id
    except PausedError:
        # #314 §4 P0-2: STOP race — 부모(review) outbox reopen + 전파.
        try:
            queue.outbox_reopen(review_task_id)
        except Exception:
            logger.exception(f"auto_enqueue_test: outbox_reopen({review_task_id}) 실패")
        raise
    except Exception as e:
        logger.warning(f"auto_enqueue_test: unexpected error: {e}")
        return None


def resume_tier3_gate(queue: TaskQueue, gate_id: str, *, pr_state_fn=None) -> Optional[str]:
    """Resume the exact Tier 3 successor held by an approved approval gate."""
    gate = next((item for item in queue.list_gates() if item.id == gate_id), None)
    if gate is None or gate.status != "approved":
        return None
    # Test gates deliberately come first: their prefix is a strict extension
    # of the review-gate prefix and must resume a test, never be misread as an
    # implementation task named ``test-...``.
    if gate_id.startswith("risk-tier3-test-"):
        review_task_id = gate_id[len("risk-tier3-test-"):]
        queue.patch_context(review_task_id, {"tier3_gate_approved": True})
        return auto_enqueue_test(queue, review_task_id, pr_state_fn=pr_state_fn)
    if gate_id.startswith("risk-tier3-"):
        impl_task_id = gate_id[len("risk-tier3-"):]
        queue.patch_context(impl_task_id, {"tier3_gate_approved": True})
        return auto_enqueue_review(queue, impl_task_id, pr_state_fn=pr_state_fn)
    return None


def auto_fallback_failed_task(
    queue: TaskQueue,
    task_id: str,
    result: TaskResult,
    task_type: str,
    *,
    pane_map: Optional[dict] = None,
    state_path: Optional[str] = None,
    fallback_disabled: bool = False,
    suppress_side_effects: bool = False,
) -> bool:
    """Reroute a rate-limit-shaped failure to the next agent in the chain.

    #314 §4 P0: ``suppress_side_effects`` (replay 경로) 시 escalation gate 생성과 telegram
    notification 같은 non-idempotent side effect를 skip한다. fallback successor enqueue(stable id)와
    task cancel(idempotent)은 durable transition이므로 계속 수행된다.

    Returns ``True`` when fallback handled the task — caller should skip
    auto-retry. ``False`` means caller should fall through to its normal
    retry path. On chain exhaustion, opens an ``escalation`` gate and
    sends a Telegram alert (best-effort).
    """
    if fallback_disabled:
        return False
    if not has_rate_limit_signal(result.summary, result.findings):
        return False

    try:
        tasks = [t for t in queue.list_tasks() if t.task_id == task_id]
        if not tasks:
            return False
        original = tasks[0]
        ctx = dict(original.context) if isinstance(original.context, dict) else {}
        ctx.pop(RESULT_BRANCH_CONTEXT_KEY, None)
        ctx.pop(RESULT_COMMIT_CONTEXT_KEY, None)

        # #167: stop infinite fallback loops — if the chain has already been
        # retried MAX_FALLBACK_CHAIN_DEPTH times, cancel the original task and
        # escalate without creating another fallback task.
        if ctx.get("fallback_chain_depth", 0) >= MAX_FALLBACK_CHAIN_DEPTH:
            logger.warning(
                f"auto_fallback: fallback_chain_depth={ctx.get('fallback_chain_depth')} "
                f">= MAX ({MAX_FALLBACK_CHAIN_DEPTH}) for {task_id} — cancelling chain."
            )
            # Cancel the original root task so the chain has a definitive
            # terminal state of "cancelled" (not "failed") in the DB.
            original_task_id = ctx.get("original_task_id")
            if original_task_id:
                try:
                    queue.cancel(original_task_id)
                    logger.info(
                        f"auto_fallback: cancelled original task {original_task_id} "
                        f"due to fallback loop detection"
                    )
                except Exception as e:
                    logger.warning(
                        f"auto_fallback: failed to cancel original task {original_task_id}: {e}"
                    )
            msg = (
                f"agent_crew fallback loop detected\n"
                f"task_id: {task_id}\n"
                f"task_type: {task_type}\n"
                f"chain_depth: {ctx.get('fallback_chain_depth')}\n"
                f"original_task_id: {original_task_id or '(unknown)'}\n"
                f"last summary: {(result.summary or '')[:200]}"
            )
            # #314 §4 P0: escalation gate 생성 + telegram notify는 non-idempotent side effect.
            # replay(suppress_side_effects)에서는 skip해 중복 gate/notification을 막는다.
            if not suppress_side_effects:
                try:
                    queue.create_gate(
                        GateRequest(
                            id=f"escalation-{task_id}-{uuid.uuid4().hex[:4]}",
                            type="escalation",
                            message=msg,
                            status="pending",
                        )
                    )
                except Exception as e:
                    logger.warning(f"auto_fallback: failed to create escalation gate: {e}")
                try:
                    notify_telegram(msg)
                except Exception:
                    pass
            return True

        role = _TYPE_TO_ROLE.get(task_type)
        current_agent = (
            ctx.get("agent_override")
            or (default_agent_for_role(role, pane_map) if (role and pane_map) else None)
        )
        excluded = list(ctx.get("fallback_excluded") or [])
        # Self-review/self-test prevention (#117): any upstream agent
        # already in the lineage (impl→review→test) must be excluded so
        # the chain doesn't loop the task back to a participant whose
        # output is being judged.
        for upstream_key in ("implementer_agent", "reviewer_agent"):
            upstream = ctx.get(upstream_key)
            if upstream and upstream not in excluded:
                excluded.append(upstream)
        if current_agent and current_agent not in excluded:
            excluded.append(current_agent)

        chains = load_fallback_chains(state_path)
        successor = next_agent(task_type, current_agent, excluded, chains)

        if successor is None:
            logger.warning(
                f"auto_fallback: chain exhausted for {task_id} "
                f"(task_type={task_type}, excluded={excluded}). Escalating."
            )
            msg = (
                f"agent_crew rate-limit fallback exhausted\n"
                f"task_id: {task_id}\n"
                f"task_type: {task_type}\n"
                f"tried agents: {', '.join(excluded) or '(none)'}\n"
                f"last summary: {(result.summary or '')[:200]}"
            )
            # #314 §4 P0: escalation gate 생성 + telegram notify는 non-idempotent side effect.
            # replay(suppress_side_effects)에서는 skip해 중복 gate/notification을 막는다.
            if not suppress_side_effects:
                try:
                    queue.create_gate(
                        GateRequest(
                            id=f"escalation-{task_id}-{uuid.uuid4().hex[:4]}",
                            type="escalation",
                            message=msg,
                            status="pending",
                        )
                    )
                except Exception as e:
                    logger.warning(f"auto_fallback: failed to create escalation gate: {e}")
                try:
                    notify_telegram(msg)
                except Exception:
                    pass
            return True

        new_ctx = dict(ctx)
        new_ctx["agent_override"] = successor
        new_ctx["fallback_excluded"] = excluded
        new_ctx["fallback_from_task_id"] = task_id
        new_ctx["fallback_chain_depth"] = ctx.get("fallback_chain_depth", 0) + 1
        # Carry the root task_id through the chain so loop detection can
        # cancel the original task when the depth limit is reached (#167).
        new_ctx["original_task_id"] = ctx.get("original_task_id") or task_id
        try:
            # #314 §4 P0-1: 결정론 successor id — chain depth로 구분(replay가 같은 depth→같은 id).
            fallback_req = TaskRequest(
                task_id=f"fallback-{task_id}-d{int(new_ctx['fallback_chain_depth'])}",
                task_type=task_type,  # type: ignore[arg-type]
                description=original.description,
                branch=original.branch,
                priority=original.priority,
                context=new_ctx,
            )
            try:
                queue.enqueue(fallback_req, ingress="cascade.fallback")
            except TaskAlreadyExistsError:
                logger.info(f"auto_fallback: {fallback_req.task_id} 이미 존재 — 멱등 skip")
            logger.info(
                f"auto_fallback: rerouted {task_id} -> {successor} "
                f"(excluded={excluded})"
            )
            return True
        except PausedError:
            raise   # 아래 outer에서 reopen+전파
        except Exception as e:
            logger.warning(f"auto_fallback: enqueue failed for {task_id}: {e}")
            return False
    except PausedError:
        # #314 §4 P0-2: STOP race — 부모(failed task) outbox reopen + 전파.
        try:
            queue.outbox_reopen(task_id)
        except Exception:
            logger.exception(f"auto_fallback: outbox_reopen({task_id}) 실패")
        raise
    except Exception as e:
        logger.warning(f"auto_fallback: unexpected error for {task_id}: {e}")
        return False
