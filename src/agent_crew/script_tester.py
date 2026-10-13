"""Deterministic script test stage (#772).

The test stage runs the project's own test command on the rebased PR head (the
PR head squashed onto its base branch). No LLM provider is called, so a
provider quota can no longer block or skip this stage.

Failures that also fail on the base head are ``pre_existing`` and are not
blamed on the PR. The base run is cached per (base sha, command).

⛔No command configured is ``SKIPPED_NO_TEST_COMMAND`` — reported as
  ``needs_human`` so the server never auto-merges it. It never auto-passes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Optional

from agent_crew.protocol import TaskResult

logger = logging.getLogger(__name__)

#: The tester role's agent name for this stage (role_mapping default).
SCRIPT_TESTER = "script"
DEFAULT_TEST_COMMAND = "python3 -m pytest -q"
DEFAULT_TEST_TIMEOUT_S = 1800.0

PASSED = "PASSED"
FAILED = "FAILED"
SKIPPED_NO_TEST_COMMAND = "SKIPPED_NO_TEST_COMMAND"
SETUP_ERROR = "SETUP_ERROR"

_OUTPUT_TAIL_CHARS = 4000
_ID_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_COUNT_RE = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


@dataclass
class RunOutcome:
    exit_code: Optional[int]
    counts: dict = field(default_factory=dict)
    failing_ids: list = field(default_factory=list)
    timed_out: bool = False
    duration_s: float = 0.0
    output_tail: str = ""

    @property
    def clean(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.failing_ids

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "counts": dict(self.counts),
                "failing_ids": list(self.failing_ids), "timed_out": self.timed_out,
                "duration_s": round(self.duration_s, 3)}

    @classmethod
    def from_dict(cls, data: Mapping) -> "RunOutcome":
        return cls(exit_code=data.get("exit_code"), counts=dict(data.get("counts") or {}),
                   failing_ids=list(data.get("failing_ids") or []),
                   timed_out=bool(data.get("timed_out")),
                   duration_s=float(data.get("duration_s") or 0.0))


def resolve_test_config(state: Optional[Mapping], checkout: str) -> tuple[Optional[str], float]:
    """Return ``(command, timeout_s)`` for a project.

    ``state.test_command`` wins; an explicit ``""``/``null`` opts out. With the
    key absent, ``python3 -m pytest -q`` applies when ``tests/`` exists.
    """
    state = state if isinstance(state, Mapping) else {}
    timeout = DEFAULT_TEST_TIMEOUT_S
    raw_timeout = state.get("test_timeout_s")
    if not isinstance(raw_timeout, bool):
        try:
            value = float(raw_timeout)
            if value > 0 and value != float("inf"):
                timeout = value
        except (TypeError, ValueError, OverflowError):
            pass
    if "test_command" in state:
        command = state.get("test_command")
        command = command.strip() if isinstance(command, str) else ""
        return (command or None), timeout
    if os.path.isdir(os.path.join(checkout, "tests")):
        return DEFAULT_TEST_COMMAND, timeout
    return None, timeout


def parse_test_output(text: str) -> tuple[dict, list]:
    """Pytest summary counts and failing node ids (``FAILED``/``ERROR`` lines)."""
    ids = list(dict.fromkeys(_ID_RE.findall(text or "")))
    counts: dict = {}
    for line in reversed((text or "").splitlines()):
        found = _COUNT_RE.findall(line)
        if found and (" in " in line or line.startswith("=")):
            for number, kind in found:
                kind = "errors" if kind.startswith("error") else kind
                counts[kind] = int(number)
            break
    return counts, ids


def run_command(command: str, cwd: str, timeout_s: float) -> RunOutcome:
    """Run ``command`` in ``cwd``; kill its process group on timeout."""
    started = time.monotonic()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True)
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        output, _ = proc.communicate()
    counts, ids = parse_test_output(output or "")
    return RunOutcome(exit_code=None if timed_out else proc.returncode, counts=counts,
                      failing_ids=ids, timed_out=timed_out,
                      duration_s=time.monotonic() - started,
                      output_tail=(output or "")[-_OUTPUT_TAIL_CHARS:])


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=300)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc


def resolve_refs(repo: str, *, base_branch: str, pr_number: Optional[int],
                 head_sha: str = "", branch: str = "", remote: str = "origin") -> tuple[str, str]:
    """Fetch and return ``(base_sha, head_sha)`` for this test."""
    _git(repo, "fetch", "--quiet", remote,
         f"+refs/heads/{base_branch}:refs/agent_crew/script_test/base")
    base_sha = _git(repo, "rev-parse", "refs/agent_crew/script_test/base").stdout.strip()
    head_ref = (f"refs/pull/{pr_number}/head" if isinstance(pr_number, int)
                else f"refs/heads/{branch}" if branch else "")
    if head_ref:
        _git(repo, "fetch", "--quiet", remote,
             f"+{head_ref}:refs/agent_crew/script_test/head")
    if head_sha:
        # Test the exact revision the reviewer approved, never a newer one.
        _git(repo, "cat-file", "-e", f"{head_sha}^{{commit}}")
        return base_sha, head_sha
    if not head_ref:
        raise RuntimeError("no PR number, branch or reviewed sha to test")
    return base_sha, _git(repo, "rev-parse", "refs/agent_crew/script_test/head").stdout.strip()


def pr_base_branch(pr_number: int, repo: str = "") -> str:
    """The PR's base branch name via ``gh``; ``""`` when it cannot be read."""
    cmd = ["gh", "pr", "view", str(pr_number), "--json", "baseRefName", "-q", ".baseRefName"]
    if repo:
        cmd += ["--repo", repo]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


class Checkout:
    """A detached temporary worktree at ``base``, optionally with ``head`` squashed on."""

    def __init__(self, repo: str, base_sha: str, head_sha: str = ""):
        self.repo, self.base_sha, self.head_sha = repo, base_sha, head_sha
        self.path = ""

    def __enter__(self) -> str:
        self.path = tempfile.mkdtemp(prefix="agent_crew_script_test_")
        _git(self.repo, "worktree", "add", "--detach", "--force", self.path, self.base_sha)
        if self.head_sha:
            merged = _git(self.path, "merge", "--squash", "--no-commit", self.head_sha,
                          check=False)
            if merged.returncode != 0:
                raise MergeConflict(merged.stdout.strip()[-300:] or merged.stderr.strip()[-300:])
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path:
            _git(self.repo, "worktree", "remove", "--force", self.path, check=False)
            shutil.rmtree(self.path, ignore_errors=True)
            _git(self.repo, "worktree", "prune", check=False)


class MergeConflict(RuntimeError):
    pass


def _cache_path(cache_dir: str, base_sha: str, command: str) -> str:
    digest = hashlib.sha256(command.encode()).hexdigest()[:12]
    return os.path.join(cache_dir, f"{base_sha}-{digest}.json")


def base_outcome(repo: str, base_sha: str, command: str, timeout_s: float,
                 cache_dir: str, runner: Callable = run_command) -> tuple[RunOutcome, bool]:
    """The base head's outcome, cached per (base sha, command). Returns (outcome, cached)."""
    path = _cache_path(cache_dir, base_sha, command)
    try:
        with open(path) as f:
            return RunOutcome.from_dict(json.load(f)), True
    except (OSError, ValueError):
        pass
    with Checkout(repo, base_sha) as checkout:
        outcome = runner(command, checkout, timeout_s)
    if not outcome.timed_out:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(outcome.to_dict(), f)
        os.replace(tmp, path)
    return outcome, False


def run_script_test(repo: str, *, base_sha: str, head_sha: str, state: Optional[Mapping],
                    cache_dir: str, runner: Callable = run_command) -> dict:
    """Run the stage and return its durable record (``outcome`` is the verdict)."""
    record: dict = {"executor": SCRIPT_TESTER, "provider_calls": 0,
                    "base_sha": base_sha, "head_sha": head_sha, "rebased": True}
    try:
        with Checkout(repo, base_sha, head_sha) as checkout:
            command, timeout_s = resolve_test_config(state, checkout)
            record.update(command=command, timeout_s=timeout_s)
            if not command:
                record["outcome"] = SKIPPED_NO_TEST_COMMAND
                return record
            head = runner(command, checkout, timeout_s)
    except MergeConflict as exc:
        record.update(outcome=FAILED, reason="merge_conflict", detail=str(exc))
        return record
    record["head"] = head.to_dict()
    record["counts"] = dict(head.counts)
    pre_existing: list = []
    new_failures = list(head.failing_ids)
    if head.failing_ids:
        base, cached = base_outcome(repo, base_sha, command, timeout_s, cache_dir, runner)
        record["base"] = {**base.to_dict(), "cached": cached}
        base_ids = set(base.failing_ids)
        pre_existing = [i for i in head.failing_ids if i in base_ids]
        new_failures = [i for i in head.failing_ids if i not in base_ids]
    record["pre_existing"] = pre_existing
    record["new_failures"] = new_failures
    if head.timed_out:
        record.update(outcome=FAILED, reason="timeout")
    elif new_failures:
        record.update(outcome=FAILED, reason="new_failures")
    elif head.exit_code == 0 or (head.exit_code == 1 and head.failing_ids):
        record["outcome"] = PASSED
    else:
        # Any other exit (pytest 2-5: interrupted/collection abort, internal
        # error, usage, no tests; or a non-pytest failure with no ids) means
        # the suite did not judge the PR. Even if base aborts the same way,
        # that is not a pass.
        record.update(outcome=FAILED, reason=f"exit_code_{head.exit_code}")
        record["output_tail"] = head.output_tail[-1500:]
    return record


def _counts_text(counts: Mapping) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no counts parsed"


def record_to_result(task_id: str, record: Mapping, pr_number=None) -> TaskResult:
    """Map the stage record onto the existing test task result contract."""
    outcome = record.get("outcome")
    info = {"script_test": dict(record)}
    if outcome == PASSED:
        pre = record.get("pre_existing") or []
        summary = (f"script test {PASSED}: {_counts_text(record.get('counts') or {})}"
                   + (f"; pre_existing failures not blamed on PR: {len(pre)}" if pre else ""))
        return TaskResult(task_id, "completed", summary, pr_number=pr_number, error_info=info)
    if outcome == SKIPPED_NO_TEST_COMMAND:
        return TaskResult(task_id, "needs_human",
                          f"{SKIPPED_NO_TEST_COMMAND}: no test command configured; "
                          "PR left for the coordinator gate (not auto-passed)",
                          pr_number=pr_number, error_info=info)
    if outcome == SETUP_ERROR:
        # The suite never ran: neither a pass nor a PR failure to fix.
        return TaskResult(task_id, "needs_human",
                          f"script test {SETUP_ERROR}: {record.get('detail', '')[:300]}; "
                          "PR left for the coordinator gate",
                          pr_number=pr_number, error_info=info)
    reason = record.get("reason") or "failed"
    new = record.get("new_failures") or []
    summary = f"script test {FAILED} ({reason}): {_counts_text(record.get('counts') or {})}"
    if new:
        summary += f"; new failures: {', '.join(new[:20])}"
    return TaskResult(task_id, "failed", summary, findings=[f"new failure: {i}" for i in new],
                      pr_number=pr_number,
                      error_info={**info, "reason": f"script_test_{reason}", "final": True})


def execute_test_task(task, *, repo: str, state: Optional[Mapping], cache_dir: str,
                      runner: Callable = run_command) -> TaskResult:
    """Run one ``test`` task by script. Never raises; never calls a provider."""
    ctx = task.context if isinstance(task.context, dict) else {}
    pr_number = ctx.get("pr_number")
    pr_number = pr_number if isinstance(pr_number, int) and not isinstance(pr_number, bool) else None
    try:
        if not repo:
            raise RuntimeError("no project checkout to run tests from")
        base_branch = (ctx.get("base_branch")
                       or (pr_base_branch(pr_number, ctx.get("repo") or "") if pr_number else "")
                       or "main")
        base_sha, head_sha = resolve_refs(
            repo, base_branch=base_branch, pr_number=pr_number,
            head_sha=ctx.get("reviewed_sha") or ctx.get("expected_head_sha") or "",
            branch=task.branch or "")
        record = run_script_test(repo, base_sha=base_sha, head_sha=head_sha, state=state,
                                 cache_dir=cache_dir, runner=runner)
        record["base_branch"] = base_branch
    except Exception as exc:  # noqa: BLE001 — the stage reports, never crashes
        logger.exception("script test setup failed for %s", task.task_id)
        record = {"executor": SCRIPT_TESTER, "provider_calls": 0,
                  "outcome": SETUP_ERROR, "detail": str(exc)}
    return record_to_result(task.task_id, record, pr_number=pr_number)


def is_script_test_result(result: TaskResult) -> bool:
    return isinstance(result.error_info, dict) and "script_test" in result.error_info
