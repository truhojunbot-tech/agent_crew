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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from agent_crew.protocol import TaskResult
from agent_crew.host_suite import host_suite_slot

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


MAX_LOAD_ENV = "AGENT_CREW_SCRIPT_TEST_MAX_LOAD"
WORKERS_ENV = "AGENT_CREW_SCRIPT_TEST_WORKERS"
DEFAULT_MAX_LOAD = 20.0
DEFAULT_WORKERS = 4
LOAD_RECHECK_S = 120.0
LOAD_MAX_WAIT_S = 3600.0
HOST_BUSY_WAIT_EVENT = "host_busy_wait"
_LOADAVG_PATH = "/proc/loadavg"


def read_load_5m() -> Optional[float]:
    """The host's 5-minute load average; ``None`` when it cannot be read."""
    try:
        with open(_LOADAVG_PATH) as f:
            value = float(f.read().split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return value if math.isfinite(value) else None


def max_load() -> float:
    """``AGENT_CREW_SCRIPT_TEST_MAX_LOAD``: 5-min load under which a full suite starts."""
    return float(_rerun_limit({}, "max_load", MAX_LOAD_ENV, DEFAULT_MAX_LOAD, integer=False))


def max_workers() -> int:
    """``AGENT_CREW_SCRIPT_TEST_WORKERS``: xdist worker cap for a full suite."""
    return int(_rerun_limit({}, "workers", WORKERS_ENV, DEFAULT_WORKERS, integer=True))


@dataclass
class HostLoadGate:
    """#1082: hold a full suite while the host is loaded, for a bounded time.

    The load is read every ``recheck_s`` for at most ``max_wait_s``; after that
    the suite runs anyway and the record says ``load_contended``. ``sleep``,
    ``clock`` and ``read_load`` are injectable so tests never really wait.
    ``on_wait_start`` fires once per wait; ``on_check`` on every reading.
    """
    max_load: float = DEFAULT_MAX_LOAD
    recheck_s: float = LOAD_RECHECK_S
    max_wait_s: float = LOAD_MAX_WAIT_S
    read_load: Optional[Callable[[], Optional[float]]] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    on_wait_start: Optional[Callable[[dict], None]] = None
    on_check: Optional[Callable[[dict], None]] = None
    port: Optional[int] = None

    @classmethod
    def from_env(cls, **kwargs) -> "HostLoadGate":
        return cls(max_load=max_load(), **kwargs)

    def admit(self) -> dict:
        """Block until the suite may start; return the admission evidence."""
        read = self.read_load or read_load_5m
        started = self.clock()
        readings: list = []
        while True:
            load = read()
            waited = self.clock() - started
            readings.append({"load_5m": load, "waited_s": round(waited, 3)})
            if self.on_check:
                self.on_check(readings[-1])
            if load is None or load < self.max_load:
                return {"max_load": self.max_load, "readings": readings,
                        "waited_s": round(waited, 3), "load_contended": False,
                        **({"load_unavailable": True} if load is None else {})}
            if waited >= self.max_wait_s:
                return {"max_load": self.max_load, "readings": readings,
                        "waited_s": round(waited, 3), "load_contended": True}
            if len(readings) == 1 and self.on_wait_start:
                self.on_wait_start({"load_5m": load, "max_load": self.max_load,
                                    "recheck_s": self.recheck_s,
                                    "max_wait_s": self.max_wait_s})
            self.sleep(min(self.recheck_s, self.max_wait_s - waited))


_MAXPROCESSES = "--maxprocesses"
_SHELL_EXPANSION = re.compile(r"[$`]")
# Anything that would make an appended word mean something other than one more
# pytest argument: operators, redirects, subshells, comments, continuations.
_SHELL_SYNTAX = re.compile(r"[;&|<>()#\n\\]")


def _direct_pytest(command: str, words: list[str]) -> bool:
    """Whether ``command`` is one plain pytest argv, safe to extend at its end."""
    return (bool(words) and not _SHELL_SYNTAX.search(command)
            and _pytest_launcher(words, 0) is not None and "--" not in words
            and sum(_pytest_launcher(words, i) is not None for i in range(len(words))) == 1)


def _maxprocesses_values(words: list[str]) -> list[str]:
    """Every ``--maxprocesses`` value in ``words``, in order (argparse: last wins)."""
    values = []
    for index, word in enumerate(words):
        if word.startswith(_MAXPROCESSES + "="):
            values.append(word[len(_MAXPROCESSES) + 1:])
        elif word == _MAXPROCESSES:
            values.append(words[index + 1] if index + 1 < len(words) else "")
    return values


def _within(value: str, cap: int) -> bool:
    return bool(re.fullmatch(r"\d+", value)) and 0 < int(value) <= cap


def _pytest_launcher(words: list[str], index: int) -> Optional[str]:
    """``module`` for ``python -m pytest``, ``script`` for ``pytest``, else None."""
    name = os.path.basename(words[index])
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", name) and words[index + 1:index + 3] == [
            "-m", "pytest"]:
        return "module"
    if name in {"pytest", "py.test"} and words[index - 1:index] != ["-m"]:
        return "script"
    return None


def _pytest_interpreters(words: list[str], env: Mapping[str, str],
                         cwd: str) -> Optional[list[str]]:
    """The interpreter of every pytest launcher in ``words``; None if any is unknown."""
    interpreters = []
    for index, word in enumerate(words):
        launcher_kind = _pytest_launcher(words, index)
        if launcher_kind == "module":
            interpreter = word
        elif launcher_kind == "script":
            launcher = shutil.which(word, path=env.get("PATH", os.defpath)) if (
                os.sep not in word) else os.path.join(cwd, word)
            try:
                with open(launcher or "", "rb") as f:
                    shebang = f.readline().decode(errors="replace")[2:].split()
            except OSError:
                return None
            if shebang[:1] and os.path.basename(shebang[0]) == "env":
                shebang = shebang[1:]
            if not shebang or not re.fullmatch(r"python(?:\d+(?:\.\d+)?)?",
                                               os.path.basename(shebang[0])):
                return None                       # an sh wrapper or unknown launcher
            interpreter = shebang[0]
        else:
            continue
        resolved = (shutil.which(interpreter, path=env.get("PATH", os.defpath))
                    if os.sep not in interpreter else os.path.join(cwd, interpreter))
        if not resolved:
            return None
        interpreters.append(resolved)
    return interpreters or None


def _target_has_xdist(interpreter: str, env: Mapping[str, str], cwd: str) -> bool:
    """Whether the interpreter the command runs pytest with can import xdist."""
    try:
        return subprocess.run([interpreter, "-c", "import xdist"], cwd=cwd, env=dict(env),
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cap_xdist_workers(command: str, env: Mapping[str, str], cap: int, *,
                      cwd: str) -> tuple[str, dict[str, str], dict]:
    """Bound a full suite's xdist workers by ``cap``; returns (command, env, evidence).

    A direct pytest launch (one plain pytest argv, no shell syntax) gets
    ``--maxprocesses=<cap>`` appended as its last argument, which wins over any
    earlier value: ``injection`` is ``appended`` and the bound is enforced
    (scope decision on #1082). Every other command is best-effort through
    ``PYTEST_ADDOPTS`` as below.

    Such a command is never rewritten: it runs under ``/bin/sh -c``, so redirects,
    pipes, variables and chained commands keep their meaning (review of #1083).
    pytest reads ini ``addopts``, then ``PYTEST_ADDOPTS``, then the command's
    own arguments, and the last ``--maxprocesses`` wins; xdist then bounds any
    ``-n`` by it. The option is injected only where pytest accepts it: every
    pytest the command launches can load xdist, and no larger or unreadable
    value follows in the command. Otherwise the env is unchanged and
    ``injection`` is ``skipped`` with a ``reason``.

    Shell-local assignments, ini settings and launchers can still change what
    runs, so the cap is a request, never a claim: ``worker_cap_receipt``
    records it next to the workers the run explicitly reported, if any.
    """
    env = dict(env)
    evidence: dict = {"cap_requested": cap, "mechanism": f"PYTEST_ADDOPTS {_MAXPROCESSES}"}

    def skipped(reason: str) -> tuple[str, dict[str, str], dict]:
        return command, env, {**evidence, "injection": "skipped", "reason": reason}

    addopts = env.get("PYTEST_ADDOPTS", "")
    try:
        words, addopt_words = shlex.split(command), shlex.split(addopts)
    except ValueError:
        return skipped("unparseable")
    if any(_SHELL_EXPANSION.search(word) for word in words):
        return skipped("command_expands_at_runtime")
    if "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in env or "no:xdist" in words + addopt_words:
        return skipped("xdist_disabled")
    interpreters = _pytest_interpreters(words, env, cwd)
    if interpreters is None:
        return skipped("pytest_launcher_unresolved")
    if not all(_target_has_xdist(interpreter, env, cwd) for interpreter in interpreters):
        return skipped("xdist_unavailable")
    if _direct_pytest(command, words):
        return f"{command} {_MAXPROCESSES}={cap}", env, {
            "cap_requested": cap, "mechanism": f"argv {_MAXPROCESSES}",
            "injection": "appended"}
    if not all(_within(value, cap) for value in _maxprocesses_values(words)):
        return skipped("command_overrides_cap")
    addopt_values = _maxprocesses_values(addopt_words)
    if addopt_values and _within(addopt_values[-1], cap):
        return command, env, {**evidence, "injection": "already_configured",
                              "configured": int(addopt_values[-1])}
    env["PYTEST_ADDOPTS"] = " ".join([addopts, f"{_MAXPROCESSES}={cap}"]).strip()
    return command, env, {**evidence, "injection": "injected"}


_XDIST_WORKERS_RE = re.compile(
    r"^(?:created: \d+/(\d+) workers?|(\d+) workers? \[\d+ items?\])\s*$", re.M)


def observed_xdist_workers(output: str) -> Optional[int]:
    """The worker count of an explicit xdist line in ``output``, else None.

    A summary without such a line proves nothing: quiet flags, plugins and
    redirected invocations all hide it, so the count stays unknown.
    """
    workers = [int(a or b) for a, b in _XDIST_WORKERS_RE.findall(output)]
    return max(workers) if workers else None


def worker_cap_receipt(injection: Mapping, command: str, outcome: "RunOutcome") -> dict:
    """The requested cap and the observed workers; never a claim of enforcement.

    ``observed_workers`` is kept only when the command visibly launches exactly
    one pytest, so the output cannot belong to a different invocation.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    launches = sum(_pytest_launcher(words, index) is not None for index in range(len(words)))
    return {**injection,
            "observed_workers": outcome.observed_workers if launches == 1 else None}


CI_GATE_ENV = "AGENT_CREW_SCRIPT_TEST_CI_GATE"
# #1038: CI states that establish no job ran a step: no check at all, or every
# failure is an Actions job with no runner and no steps (billing block). Only
# these yield to a local pass. ``pending`` covers a runner still executing
# steps and ``error`` means CI could not be read, so neither proves the absence
# of a failing job; they stay INCOMPLETE. ``head_checks_state`` reports any
# genuinely failed run as red.
_CI_NOT_GATING_STATES = frozenset({"none", "infra_unavailable"})


def ci_gate_enabled() -> bool:
    """``AGENT_CREW_SCRIPT_TEST_CI_GATE``: unset/``1`` gates on red CI, ``0`` does not."""
    raw = os.environ.get(CI_GATE_ENV)
    if raw is None:
        return True
    value = raw.strip()
    if value in ("0", "1"):
        return value == "1"
    logger.warning("invalid %s=%r; using default 1", CI_GATE_ENV, raw)
    return True


@dataclass
class RunOutcome:
    exit_code: Optional[int]
    counts: dict = field(default_factory=dict)
    failing_ids: list = field(default_factory=list)
    timed_out: bool = False
    duration_s: float = 0.0
    output_tail: str = ""
    observed_workers: Optional[int] = None        # None: the run did not report it
    worker_cap: Optional[dict] = None

    @property
    def clean(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.failing_ids

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "counts": dict(self.counts),
                "failing_ids": list(self.failing_ids), "timed_out": self.timed_out,
                "duration_s": round(self.duration_s, 3),
                "observed_workers": self.observed_workers,
                **({"worker_cap": dict(self.worker_cap)} if self.worker_cap else {})}

    @classmethod
    def from_dict(cls, data: Mapping) -> "RunOutcome":
        return cls(exit_code=data.get("exit_code"), counts=dict(data.get("counts") or {}),
                   failing_ids=list(data.get("failing_ids") or []),
                   timed_out=bool(data.get("timed_out")),
                   duration_s=float(data.get("duration_s") or 0.0),
                   observed_workers=data.get("observed_workers"),
                   worker_cap=data.get("worker_cap"))


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
                      output_tail=(output or "")[-_OUTPUT_TAIL_CHARS:],
                      observed_workers=observed_xdist_workers(output or ""))


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


def _admit_full_suite(load_gate: Optional[HostLoadGate], record: Optional[dict]) -> bool:
    """Hold a full suite on host load (#1082) and keep the evidence in ``record``.

    Returns whether the suite was admitted ``load_contended``.
    """
    if load_gate is None:
        return False
    admission = load_gate.admit()
    if record is not None:
        record.setdefault("host_load", []).append(admission)
        if admission["load_contended"]:
            record["load_contended"] = True
    return admission["load_contended"]


@contextmanager
def _full_suite_slot(load_gate: Optional[HostLoadGate], record: Optional[dict]):
    timing = ({"clock": load_gate.clock, "sleep": load_gate.sleep,
               "on_check": load_gate.on_check, "port": load_gate.port}
              if load_gate is not None else {})
    with host_suite_slot(kind="script_test", **timing) as receipt:
        if record is not None:
            record.setdefault("host_suite", []).append(receipt)
            record["queue_wait_s"] = round(
                record.get("queue_wait_s", 0.0) + receipt["queue_wait_s"], 3)
            record["host_suite_contended"] = (
                record.get("host_suite_contended", False) or receipt["host_suite_contended"])
        yield


def base_outcome(repo: str, base_sha: str, command: str, timeout_s: float,
                 cache_dir: str, state: Optional[Mapping],
                 runner: Callable = run_command, *,
                 load_gate: Optional[HostLoadGate] = None,
                 record: Optional[dict] = None,
                 worker_cap: Optional[int] = None) -> tuple[RunOutcome, bool]:
    """The base outcome, cached per base sha, command and test environment.

    Under ``worker_cap`` a baseline is keyed by the workers it was observed to
    run with (or ``unknown``), never by the requested cap: a base seen with
    more workers than the cap is not reused under it. A base admitted
    ``load_contended`` is used for this run but not cached as a clean baseline.
    """
    identity = _test_environment_identity(state, timeout_s)

    def path_for(workers) -> str:
        if worker_cap is None:
            return _cache_path(cache_dir, base_sha, command, identity)
        return _cache_path(cache_dir, base_sha, command, f"{identity}-w{workers}")

    keys = [None] if worker_cap is None else [*range(worker_cap, 0, -1), "unknown"]
    for key in keys:
        try:
            with open(path_for(key)) as f:
                return RunOutcome.from_dict(json.load(f)), True
        except (OSError, ValueError):
            pass
    contended = _admit_full_suite(load_gate, record)
    with Checkout(repo, base_sha) as checkout:
        env, launched = test_environment(checkout, state), command
        if worker_cap is not None:
            launched, env, injection = cap_xdist_workers(command, env, worker_cap,
                                                         cwd=checkout)
        with _full_suite_slot(load_gate, record):
            outcome = runner(launched, checkout, timeout_s, env=env)
        if worker_cap is not None:
            outcome.worker_cap = worker_cap_receipt(injection, launched, outcome)
    path = path_for(None if worker_cap is None else
                    outcome.worker_cap["observed_workers"] or "unknown")
    if not outcome.timed_out and not contended:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(outcome.to_dict(), f)
        os.replace(tmp, path)
    return outcome, False


def run_script_test(repo: str, *, base_sha: str, head_sha: str, state: Optional[Mapping],
                    cache_dir: str, runner: Callable = run_command,
                    load_gate: Optional[HostLoadGate] = None) -> dict:
    """Run the stage and return its durable record (``outcome`` is the verdict).

    Full suites (head and uncached base) wait on ``load_gate`` and run with
    their xdist workers capped; isolated reruns use the plain environment.
    """
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
            cap = max_workers()
            suite_command, suite_env, injection = cap_xdist_workers(
                command, head_env, cap, cwd=checkout)
            if suite_command != command:
                record["suite_command"] = suite_command
            _admit_full_suite(load_gate, record)
            with _full_suite_slot(load_gate, record):
                head = runner(suite_command, checkout, timeout_s, env=suite_env)
            record["worker_cap"] = worker_cap_receipt(injection, suite_command, head)
            record["head"] = head.to_dict()
            record["counts"] = dict(head.counts)
            record["duration_s"] = head.duration_s
            if head.timed_out:
                record.update(outcome=INCOMPLETE, reason="script_test_timeout")
                return record
            if head.failing_ids:
                base, cached = base_outcome(repo, base_sha, command, timeout_s,
                                            cache_dir, state, runner,
                                            load_gate=load_gate, record=record,
                                            worker_cap=cap)
                record["base"] = {**base.to_dict(), "cached": cached}
                record["duration_s"] += base.duration_s
                if base.timed_out:
                    record.update(outcome=INCOMPLETE, reason="script_test_timeout")
                    return record
                if ((base.worker_cap or {}).get("observed_workers") or 0) > cap:
                    # Not comparable with a head bounded by the cap (#1082).
                    record.update(outcome=INCOMPLETE, reason="script_test_base_over_cap")
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
                   + (f"; ci_state={record.get('ci_state')} (not gated: "
                      f"{record.get('ci_not_gated')})" if record.get("ci_not_gated") else "")
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
                      runner: Callable = run_command,
                      load_gate: Optional[HostLoadGate] = None) -> TaskResult:
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
                                 cache_dir=cache_dir, runner=runner, load_gate=load_gate)
        record["base_branch"] = base_branch
        if record.get("outcome") == PASSED and head_has_workflows(repo, head_sha):
            from agent_crew.github import get_repo, head_checks_state
            gh_repo = ctx.get("repo") or get_repo(cwd=repo)
            if not gh_repo:
                raise RuntimeError("missing context.repo for CI checks lookup")
            ci_state, ci_detail = head_checks_state(gh_repo, head_sha)
            record.update(ci_state=ci_state, ci_detail=ci_detail)
            # #1038: the local suite verdict stays authoritative unless a CI
            # job really ran and failed; CI is then recorded as evidence only.
            if ci_state == "green":
                pass
            elif not ci_gate_enabled():
                record["ci_not_gated"] = "gate off"
            elif ci_state in _CI_NOT_GATING_STATES:
                record["ci_not_gated"] = "infra"
            elif ci_state == "red":
                record.update(outcome=FAILED, reason="ci_red")
            else:
                record.update(outcome=INCOMPLETE, reason=f"ci_{ci_state}")
    except Exception as exc:  # noqa: BLE001 — the stage reports, never crashes
        logger.exception("script test setup failed for %s", task.task_id)
        record = {"executor": SCRIPT_TESTER, "provider_calls": 0,
                  "outcome": SETUP_ERROR, "detail": str(exc)}
    return record_to_result(task.task_id, record, pr_number=pr_number)


def is_script_test_result(result: TaskResult) -> bool:
    return isinstance(result.error_info, dict) and "script_test" in result.error_info
