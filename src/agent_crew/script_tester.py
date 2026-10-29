"""Deterministic script test stage (#772).

The test stage runs the project's own test command on the rebased PR head (the
PR head squashed onto its base branch). No LLM provider is called, so a
provider quota can no longer block or skip this stage.

Failures that also fail on the base head are ``pre_existing`` and are not
blamed on the PR. The base run is cached per base sha, command and test
environment/config identity.

⛔No command configured is ``SKIPPED_NO_TEST_COMMAND`` — reported as
  ``needs_human`` so the server never auto-merges it. It never auto-passes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import site
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from agent_crew.protocol import TaskResult

logger = logging.getLogger(__name__)

#: The tester role's agent name for this stage (role_mapping default).
SCRIPT_TESTER = "script"
DEFAULT_TEST_COMMAND = "python3 -m pytest -q"
DEFAULT_TEST_TIMEOUT_S = 1800.0

PASSED = "PASSED"
FAILED = "FAILED"
INCOMPLETE = "INCOMPLETE"
SKIPPED_NO_TEST_COMMAND = "SKIPPED_NO_TEST_COMMAND"
SETUP_ERROR = "SETUP_ERROR"

_OUTPUT_TAIL_CHARS = 4000
_ID_RE = re.compile(r"^(?:FAILED|ERROR) (.+)$")
_COUNT_RE = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")
_TEST_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
_PRIVATE_ENV_KEY = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|AUTH|PRIVATE_KEY)", re.I)
_MAX_RERUN_CANDIDATES = 5
_RERUN_BUDGET_S = 600.0  # watchdog adds this capped budget plus checkout allowance


class TestConfig(NamedTuple):
    command: Optional[str]
    timeout_s: float
    max_rerun_candidates: int
    rerun_budget_s: float


def _rerun_limit(state: Mapping, key: str, env_key: str, default: int | float,
                 *, integer: bool) -> int | float:
    raw = os.environ.get(env_key) if env_key in os.environ else state.get(key)
    if raw is None and env_key not in os.environ and key not in state:
        return default
    try:
        if isinstance(raw, bool):
            raise ValueError("boolean is not a limit")
        value = int(raw) if integer else float(raw)
        if integer and str(raw).strip() != str(value):
            raise ValueError("not an integer")
        if value <= 0 or not math.isfinite(value):
            raise ValueError("limit must be positive and finite")
        return value
    except (TypeError, ValueError, OverflowError):
        logger.warning("invalid %s=%r; using default %s", env_key if env_key in os.environ
                       else key, raw, default)
        return default


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


def resolve_test_config(state: Optional[Mapping], checkout: str) -> TestConfig:
    """Return the suite command, timeout, and isolated rerun limits.

    ``state.test_command`` wins; an explicit ``""``/``null`` opts out. With the
    key absent, ``python3 -m pytest -q`` applies when ``tests/`` exists.
    """
    state = state if isinstance(state, Mapping) else {}
    max_candidates = _rerun_limit(
        state, "max_rerun_candidates", "AGENT_CREW_SCRIPT_TEST_MAX_RERUN_CANDIDATES",
        _MAX_RERUN_CANDIDATES, integer=True)
    rerun_budget = _rerun_limit(
        state, "rerun_budget_s", "AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S",
        _RERUN_BUDGET_S, integer=False)
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
        return TestConfig(command or None, timeout, max_candidates, rerun_budget)
    if os.path.isdir(os.path.join(checkout, "tests")):
        return TestConfig(DEFAULT_TEST_COMMAND, timeout, max_candidates, rerun_budget)
    return TestConfig(None, timeout, max_candidates, rerun_budget)


def _after_balanced_group(line: str, start: int) -> Optional[int]:
    opener = line[start]
    closer = ")" if opener == "(" else "]"
    depth = 1
    end = start + 1
    while end < len(line) and depth:
        if line[end] == opener:
            depth += 1
        elif line[end] == closer:
            depth -= 1
        end += 1
    return end if depth == 0 else None


def parse_test_output(text: str) -> tuple[dict, list]:
    """Pytest summary counts and failing parent node ids."""
    ids = []
    for line in (text or "").splitlines():
        match = _ID_RE.match(line)
        if match:
            entry = match.group(1)
        elif line.startswith(("SUBFAILED(", "SUBFAILED[")):
            # Pytest subtests put their case detail before the parent node ID.
            # Find the matching delimiter: detail can contain nested delimiters
            # and the same " - " separator used before a failure reason.
            end = _after_balanced_group(line, len("SUBFAILED"))
            if end is None or not line.startswith(" ", end):
                continue
            entry = line[end + 1:]
            if entry.startswith("("):
                params_end = _after_balanced_group(entry, 0)
                if params_end is None or not entry.startswith(" ", params_end):
                    continue
                entry = entry[params_end + 1:]
        else:
            continue
        # The pytest summary separates a node id from its reason with " - ".
        # A parametrized id can contain both spaces and that separator inside
        # brackets, so split only after the closing bracket.
        depth = 0
        for index, char in enumerate(entry):
            if char == "[":
                depth += 1
            elif char == "]" and depth:
                depth -= 1
            elif depth == 0 and entry.startswith(" - ", index):
                entry = entry[:index]
                break
        path, separator, name = entry.partition("::")
        if (separator and path.endswith(".py") and path and name
                and not re.search(r"[\s:]", path) and entry not in ids):
            ids.append(entry)
    counts: dict = {}
    for line in reversed((text or "").splitlines()):
        found = _COUNT_RE.findall(line)
        if found and (" in " in line or line.startswith("=")):
            for number, kind in found:
                kind = "errors" if kind.startswith("error") else kind
                counts[kind] = int(number)
            break
    return counts, ids


def watchdog_timeout_seconds(state: Optional[Mapping], ordinary_timeout_s: float) -> float:
    """Bound both suite runs, isolated reruns, and fetch/checkout setup."""
    config = resolve_test_config(state, "")
    rerun_budget_s = config.rerun_budget_s
    if isinstance(state, Mapping) and "script_test_rerun_budget_s" in state:
        try:
            stored = float(state["script_test_rerun_budget_s"])
            if not math.isfinite(stored) or stored <= 0:
                raise ValueError("invalid persisted rerun budget")
            rerun_budget_s = stored
        except (TypeError, ValueError, OverflowError):
            logger.warning("invalid persisted script_test_rerun_budget_s=%r; "
                           "using configured budget", state["script_test_rerun_budget_s"])
    return max(ordinary_timeout_s,
               2 * config.timeout_s + min(rerun_budget_s, config.timeout_s) + 600.0)


def _isolated_rerun_command(command: str, node_id: str) -> Optional[str]:
    """Run one node from a plain pytest invocation with at most one suite target.

    A project command can invoke make, a shell pipeline, or an unrelated target.
    Appending a node to any of those does not run that node in isolation.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    if os.path.basename(words[0]) in {"pytest", "py.test"}:
        index = 1
    elif (re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", os.path.basename(words[0]))
          and words[1:3] == ["-m", "pytest"]):
        index = 3
    else:
        return None
    target_index = None
    node_path = node_id.split("::", 1)[0]
    while index < len(words):
        word = words[index]
        if word in {"-q", "-v", "-x", "--disable-warnings"}:
            index += 1
        elif word == "-p" and index + 1 < len(words):
            index += 2
        elif word == "--timeout" and index + 1 < len(words) and re.fullmatch(
                r"\d+(?:\.\d+)?", words[index + 1]):
            index += 2
        elif re.fullmatch(r"--[A-Za-z][A-Za-z0-9-]*=.+", word):
            index += 1
        elif target_index is None and node_path.startswith(word.rstrip("/") + "/"):
            target_index = index
            index += 1
        else:
            return None
    if target_index is not None:
        words[target_index] = node_id
        return shlex.join(words)
    return f"{command} {shlex.quote(node_id)}"


def test_environment(checkout: str, state: Optional[Mapping]) -> dict[str, str]:
    """Small, explicit environment for a test checkout, never the dispatcher env.

    ``state.test_env`` supplies project-specific non-secret values. PYTHONPATH
    always points at this checkout, so pytest imports the code under test.
    """
    env = {key: os.environ[key] for key in _TEST_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", os.defpath)
    if site.USER_BASE:
        # Tests that isolate HOME still need the installed pytest in user site.
        env["PYTHONUSERBASE"] = site.USER_BASE
    env["PYTHONPATH"] = os.path.join(checkout, "src")
    extra = (state or {}).get("test_env", {})
    if not isinstance(extra, Mapping):
        raise ValueError("test_env must be an object")
    for key, value in extra.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid test_env key {key!r}")
        if (key.startswith("AGENT_CREW_") or key == "PYTHONPATH"
                or _PRIVATE_ENV_KEY.search(key)):
            raise ValueError(f"test_env key {key} is reserved")
        if not isinstance(value, str):
            raise ValueError(f"test_env value for {key} must be text")
        env[key] = value
    return env


def _test_environment_identity(state: Optional[Mapping], timeout_s: float) -> str:
    template = test_environment("<CHECKOUT>", state)
    payload = json.dumps({"environment": template, "timeout_s": timeout_s},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def run_command(command: str, cwd: str, timeout_s: float,
                *, env: Optional[Mapping[str, str]] = None) -> RunOutcome:
    """Run ``command`` in ``cwd``; kill its process group on timeout."""
    started = time.monotonic()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
        env=dict(env) if env is not None else test_environment(cwd, {}))
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
    if not repo or not repo.strip():
        raise RuntimeError("missing context.repo for PR base branch lookup")
    cmd = ["gh", "pr", "view", str(pr_number), "--json", "baseRefName", "-q", ".baseRefName",
           "--repo", repo]
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


def _cache_path(cache_dir: str, base_sha: str, command: str,
                environment_identity: str) -> str:
    digest = hashlib.sha256((command + "\0" + environment_identity).encode()).hexdigest()[:12]
    return os.path.join(cache_dir, f"{base_sha}-{digest}.json")


def base_outcome(repo: str, base_sha: str, command: str, timeout_s: float,
                 cache_dir: str, state: Optional[Mapping],
                 runner: Callable = run_command) -> tuple[RunOutcome, bool]:
    """The base outcome, cached per base sha, command and test environment."""
    path = _cache_path(cache_dir, base_sha, command,
                       _test_environment_identity(state, timeout_s))
    try:
        with open(path) as f:
            return RunOutcome.from_dict(json.load(f)), True
    except (OSError, ValueError):
        pass
    with Checkout(repo, base_sha) as checkout:
        outcome = runner(command, checkout, timeout_s,
                         env=test_environment(checkout, state))
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
                    "base_sha": base_sha, "head_sha": head_sha, "rebased": True,
                    "pre_existing": [], "new_failures": [], "flaky": [], "reruns": {}}
    try:
        with Checkout(repo, base_sha, head_sha) as checkout:
            config = resolve_test_config(state, checkout)
            command, timeout_s = config.command, config.timeout_s
            record.update(command=command, timeout_s=timeout_s)
            if not command:
                record["outcome"] = SKIPPED_NO_TEST_COMMAND
                return record
            head_env = test_environment(checkout, state)
            head = runner(command, checkout, timeout_s, env=head_env)
            record["head"] = head.to_dict()
            record["counts"] = dict(head.counts)
            record["duration_s"] = head.duration_s
            if head.timed_out:
                record.update(outcome=INCOMPLETE, reason="script_test_timeout")
                return record
            if head.failing_ids:
                base, cached = base_outcome(repo, base_sha, command, timeout_s,
                                            cache_dir, state, runner)
                record["base"] = {**base.to_dict(), "cached": cached}
                record["duration_s"] += base.duration_s
                if base.timed_out:
                    record.update(outcome=INCOMPLETE, reason="script_test_timeout")
                    return record
                base_ids = set(base.failing_ids)
                record["pre_existing"] = [i for i in head.failing_ids if i in base_ids]
                candidates = [i for i in head.failing_ids if i not in base_ids]
                if head.exit_code == 1:
                    skip_reason = ("script_test_rerun_limit"
                                   if len(candidates) > config.max_rerun_candidates else None)
                    rerun_commands = ({
                        node_id: _isolated_rerun_command(command, node_id)
                        for node_id in candidates
                    } if not skip_reason else {})
                    if not skip_reason and any(
                            value is None for value in rerun_commands.values()):
                        skip_reason = "script_test_rerun_unavailable"
                    if skip_reason:
                        # The head suite produced a test verdict; unavailable
                        # flake checks must not suppress its new failures.
                        record["reruns_skipped"] = skip_reason
                    else:
                        record["rerun_budget_s"] = min(config.rerun_budget_s, timeout_s)
                        rerun_deadline = time.monotonic() + record["rerun_budget_s"]
                        for node_id in candidates:
                            attempts = record["reruns"][node_id] = []
                            for _ in range(2):
                                remaining = rerun_deadline - time.monotonic()
                                if remaining <= 0:
                                    record.update(outcome=INCOMPLETE,
                                                  reason="script_test_timeout")
                                    return record
                                rerun = runner(rerun_commands[node_id], checkout,
                                               min(timeout_s, remaining), env=head_env)
                                attempts.append(rerun.to_dict())
                                record["duration_s"] += rerun.duration_s
                                if rerun.timed_out:
                                    record.update(outcome=INCOMPLETE,
                                                  reason="script_test_timeout")
                                    return record
                                if rerun.exit_code == 0 and not rerun.failing_ids:
                                    record["flaky"].append(node_id)
                                    break
                                if rerun.exit_code != 1 or node_id not in rerun.failing_ids:
                                    record.update(outcome=INCOMPLETE,
                                                  reason="script_test_rerun_unjudged")
                                    return record
                record["new_failures"] = [i for i in candidates if i not in record["flaky"]]
    except MergeConflict as exc:
        record.update(outcome=FAILED, reason="merge_conflict", detail=str(exc))
        return record
    if head.exit_code not in (0, 1):
        # Collection, configuration and interrupted runs can print ERROR ids,
        # but they did not produce a test verdict about the PR.
        record.update(outcome=FAILED, reason=f"exit_code_{head.exit_code}")
        record["output_tail"] = head.output_tail[-1500:]
    elif record["new_failures"]:
        record.update(outcome=FAILED, reason="new_failures")
    elif head.exit_code == 0 or (head.exit_code == 1 and head.failing_ids):
        record["outcome"] = PASSED
    else:
        record.update(outcome=FAILED, reason=f"exit_code_{head.exit_code}")
    return record


def _counts_text(counts: Mapping) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no counts parsed"


def record_to_result(task_id: str, record: Mapping, pr_number=None) -> TaskResult:
    """Map the stage record onto the existing test task result contract."""
    outcome = record.get("outcome")
    info = {"script_test": dict(record)}
    if outcome == PASSED:
        pre = record.get("pre_existing") or []
        flaky = record.get("flaky") or []
        summary = (f"script test {PASSED}: {_counts_text(record.get('counts') or {})}"
                   + (f"; pre_existing failures not blamed on PR: {len(pre)}" if pre else "")
                   + (f"; flaky: {', '.join(flaky[:20])}" if flaky else ""))
        return TaskResult(task_id, "completed", summary, pr_number=pr_number, error_info=info)
    if outcome == SKIPPED_NO_TEST_COMMAND:
        return TaskResult(task_id, "needs_human",
                          f"{SKIPPED_NO_TEST_COMMAND}: no test command configured; "
                          "PR left for the coordinator gate (not auto-passed)",
                          pr_number=pr_number,
                          error_info={**info, "kind": "config_error"})
    if outcome == SETUP_ERROR:
        # The suite never ran: neither a pass nor a PR failure to fix.
        return TaskResult(task_id, "needs_human",
                          f"script test {SETUP_ERROR}: {record.get('detail', '')[:300]}; "
                          "PR left for the coordinator gate",
                          pr_number=pr_number,
                          error_info={**info, "kind": "config_error"})
    if outcome == INCOMPLETE:
        return TaskResult(task_id, "needs_human",
                          f"script test {INCOMPLETE}: {record.get('reason')}; "
                          "PR left for the coordinator gate",
                          pr_number=pr_number,
                          error_info={**info, "kind": "timeout",
                                      "reason": record.get("reason")})
    reason = record.get("reason") or "failed"
    new = record.get("new_failures") or []
    summary = f"script test {FAILED} ({reason}): {_counts_text(record.get('counts') or {})}"
    if new:
        summary += f"; new failures: {', '.join(new[:20])}"
    flaky = record.get("flaky") or []
    if flaky:
        summary += f"; flaky: {', '.join(flaky[:20])}"
    return TaskResult(task_id, "failed", summary, findings=[f"new failure: {i}" for i in new],
                      pr_number=pr_number,
                      error_info={**info, "kind": "test_failure" if new else "agent_failed",
                                  "reason": f"script_test_{reason}", "final": True})


def head_has_workflows(repo: str, head_sha: str) -> bool:
    """Check the tested commit, not the checkout's current branch, for CI workflows."""
    result = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-r", "--name-only", head_sha,
         "--", ".github/workflows"],
        capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"workflow lookup failed: {result.stderr.strip()[:200]}")
    return any(path.endswith((".yml", ".yaml")) for path in result.stdout.splitlines())


def execute_test_task(task, *, repo: str, state: Optional[Mapping], cache_dir: str,
                      runner: Callable = run_command) -> TaskResult:
    """Run one ``test`` task by script. Never raises; never calls a provider."""
    ctx = task.context if isinstance(task.context, dict) else {}
    pr_number = ctx.get("pr_number")
    pr_number = pr_number if isinstance(pr_number, int) and not isinstance(pr_number, bool) else None
    try:
        if not repo:
            raise RuntimeError("no project checkout to run tests from")
        base_branch = ctx.get("base_branch") or ""
        if not base_branch and pr_number:
            from agent_crew.github import get_repo
            gh_repo = ctx.get("repo") or get_repo(cwd=repo)
            if not gh_repo:
                raise RuntimeError("missing context.repo for PR base branch lookup")
            base_branch = pr_base_branch(pr_number, gh_repo)
        base_branch = base_branch or "main"
        base_sha, head_sha = resolve_refs(
            repo, base_branch=base_branch, pr_number=pr_number,
            head_sha=ctx.get("reviewed_sha") or ctx.get("expected_head_sha") or "",
            branch=task.branch or "")
        record = run_script_test(repo, base_sha=base_sha, head_sha=head_sha, state=state,
                                 cache_dir=cache_dir, runner=runner)
        record["base_branch"] = base_branch
        if record.get("outcome") == PASSED and head_has_workflows(repo, head_sha):
            from agent_crew.github import get_repo, head_checks_state
            gh_repo = ctx.get("repo") or get_repo(cwd=repo)
            if not gh_repo:
                raise RuntimeError("missing context.repo for CI checks lookup")
            ci_state, ci_detail = head_checks_state(gh_repo, head_sha)
            record.update(ci_state=ci_state, ci_detail=ci_detail)
            if ci_state == "red":
                record.update(outcome=FAILED, reason="ci_red")
            elif ci_state != "green":
                record.update(outcome=INCOMPLETE, reason=f"ci_{ci_state}")
    except Exception as exc:  # noqa: BLE001 — the stage reports, never crashes
        logger.exception("script test setup failed for %s", task.task_id)
        record = {"executor": SCRIPT_TESTER, "provider_calls": 0,
                  "outcome": SETUP_ERROR, "detail": str(exc)}
    return record_to_result(task.task_id, record, pr_number=pr_number)


def is_script_test_result(result: TaskResult) -> bool:
    return isinstance(result.error_info, dict) and "script_test" in result.error_info
