"""Test-stage policy: what the tester runs, and one run at a time (#272).

Two problems, one stage, and they compound each other.

**Duplication.** The tester prompt said "run the full test suite" for every
project unconditionally. CI already runs the full suite on the PR, so the
tester's run was a second copy of the same work on the developer's own host.
alpha_engine#5541 measured the consequence: load 30.29 on 16 cores, 1 GB free,
and a dev job queued behind it for 45+ minutes. The fix cannot be "skip tests
on alpha_engine" — the scope is a property of each project, so it is
configuration, and the default has to be safe for a project nobody configured.

**Concurrency.** The same incident saw two `make test` runs start 69 s apart in
one worktree. The dispatcher already refuses to run two tasks against a
worktree at once, but that guard is a `set` in one process's memory: it does
not survive a dispatcher restart while a `start_new_session=True` child keeps
running, and it cannot see a second dispatcher at all. A lock has to live
outside the process to close either gap.

⛔The lock is keyed on the worktree but kept OUT of it, under
  ``$AGENT_CREW_BASE/locks/``. Any process on the host resolves the same path
  for the same worktree, including one belonging to a different project or
  state directory, and no lock file lands in the repo being tested.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

#: Where the config may live, most authoritative first. The operator override
#: outranks the in-repo file on purpose: turning the scope down is an urgent
#: response to a loaded host, and it must not require a commit to the repo
#: whose tests are the problem.
ENV_SCOPE = "AGENT_CREW_TEST_SCOPE"
STATE_SCOPE_FILE = "test_scope.json"
REPO_SCOPE_FILE = os.path.join(".agent_crew", "test_scope.json")

#: The whole point of #272: full-suite is opt-IN. A project that wants the old
#: behaviour says so; a project that says nothing gets targeted tests, which is
#: the cheap thing that still catches the regression the diff introduced.
DEFAULT_SCOPE: dict = {
    "full_suite": False,
    "targeted": [],
    "guards": [],
    "full": [],
    "source": "default",
    "source_kind": "builtin",
}

#: Categorical provenance, safe to put in telemetry. `source` carries the
#: literal path or env name for humans reading a log; a filesystem path is not
#: something to publish into an economics stream (#278), so cohorts join on
#: this instead.
SOURCE_KINDS = ("builtin", "env", "operator", "repo")

#: Treatment names for the telemetry contract. Deliberately NOT booleans — a
#: missing field must read as "unknown", and `full_suite=False` on a row that
#: predates #278 would be a treatment claim nobody made (#278 criterion 5).
SCOPE_TARGETED = "targeted"
SCOPE_FULL = "full_suite"


def _base() -> str:
    return os.environ.get("AGENT_CREW_BASE", os.path.expanduser("~/.agent_crew"))


def _coerce(raw, source: str, source_kind: str = "builtin") -> dict:
    """A parsed config into the shape the renderer expects.

    Tolerant on purpose: a malformed scope must not break dispatch, and a
    string where a list belongs is the most likely hand-edit mistake.
    """
    scope = dict(DEFAULT_SCOPE)
    if not isinstance(raw, dict):
        logger.warning(f"test scope from {source} is not an object; ignoring it")
        return scope
    for key in ("targeted", "guards", "full"):
        value = raw.get(key)
        if isinstance(value, str):
            value = [value]
        scope[key] = [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []
    scope["full_suite"] = bool(raw.get("full_suite", False))
    scope["source"] = source
    scope["source_kind"] = source_kind if source_kind in SOURCE_KINDS else "builtin"
    return scope


def _read(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # ⛔Loud, but not fatal. A broken scope file silently falling back to
        #   the default would be indistinguishable from "no file", and the
        #   operator who wrote it would never learn it is not in effect.
        logger.warning(f"test scope at {path} is unreadable ({e}); ignoring it")
        return None


def load_scope(worktree: str = "", project: str = "", *, base: str | None = None) -> dict:
    """The test scope for this worktree/project, by precedence.

    ``AGENT_CREW_TEST_SCOPE`` (inline JSON or a path) > the operator's
    ``<base>/<project>/test_scope.json`` > the repo's own
    ``.agent_crew/test_scope.json`` > :data:`DEFAULT_SCOPE`.
    """
    raw_env = (os.environ.get(ENV_SCOPE) or "").strip()
    if raw_env:
        if raw_env.startswith("{"):
            try:
                return _coerce(json.loads(raw_env), f"env:{ENV_SCOPE}", "env")
            except ValueError as e:
                logger.warning(f"{ENV_SCOPE} is not valid JSON ({e}); ignoring it")
        else:
            data = _read(os.path.expanduser(raw_env))
            if data is not None:
                return _coerce(data, f"env:{raw_env}", "env")

    if project:
        path = os.path.join(base or _base(), project, STATE_SCOPE_FILE)
        data = _read(path)
        if data is not None:
            return _coerce(data, path, "operator")

    if worktree:
        path = os.path.join(worktree, REPO_SCOPE_FILE)
        data = _read(path)
        if data is not None:
            return _coerce(data, path, "repo")

    return dict(DEFAULT_SCOPE)


def effective_scope(scope: dict) -> str:
    """``"targeted"`` or ``"full_suite"`` — the treatment actually applied.

    "Effective" is the whole point of #278. A malformed override falls back to
    the built-in default, and what belongs in the economics stream is the
    scope that was USED, never the one that was requested-but-invalid: a cohort
    built on intent rather than effect measures nothing.
    """
    return SCOPE_FULL if scope.get("full_suite") else SCOPE_TARGETED


def scope_fingerprint(scope: dict) -> str:
    """Stable 16-hex digest of the effective configuration (#278).

    ⛔The commands themselves are NOT telemetry. They can carry absolute
      worktree paths and project-internal names, and #278 asks for a stable
      version or hash rather than the raw config for exactly that reason. A
      digest still answers the question a rollout cohort needs — "are these two
      tasks running the same policy?" — without publishing anything.

    Covers only the fields that change behaviour. `source`/`source_kind` are
    excluded on purpose: the same policy reached through the operator file and
    through the repo file is the same treatment, and folding provenance in
    would split one cohort in two.
    """
    material = json.dumps(
        {
            "full_suite": bool(scope.get("full_suite")),
            "targeted": list(scope.get("targeted") or []),
            "guards": list(scope.get("guards") or []),
            "full": list(scope.get("full") or []),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _bullets(commands: list) -> str:
    return "\n".join(f"  {c}" for c in commands)


def render_scope(scope: dict) -> str:
    """The tester prompt's scope block for a resolved config."""
    if scope.get("full_suite"):
        full = scope.get("full") or []
        body = (f"This project opts INTO the full suite (`full_suite: true` in "
                f"{scope.get('source')}), so run it:\n\n```bash\n{_bullets(full)}\n```\n"
                if full else
                "This project opts INTO the full suite (`full_suite: true` in "
                f"{scope.get('source')}), so run the project's full test command.\n")
        return ("### Test scope: FULL (opted in)\n\n" + body +
                "\nRun it once. Do not repeat it per finding.\n")

    lines = ["### Test scope: TARGETED — do NOT run the full suite\n",
             "CI runs the full suite on this PR. Running it again here is a second",
             "copy of the same work on a shared host: alpha_engine#5541 measured",
             "load 30.29 on 16 cores with 1 GB free and a dev job queued 45+ minutes",
             "behind it (#272). Your job is the part CI cannot do — checking that",
             "the diff is covered — not re-running what CI already ran.\n",
             "1. Get the diff: `git diff --name-only origin/main...HEAD`.",
             "2. Run the tests that cover those files."]
    if scope["targeted"]:
        lines += ["   This project specifies how (`{paths}` is the changed files):\n",
                  "```bash", _bullets(scope["targeted"]), "```"]
    else:
        lines += ["   No `targeted` commands are configured, so derive them: run the",
                  "   test files that exercise the changed modules, not the whole tree."]
    if scope["guards"]:
        lines += ["\n3. Run the cross-cutting guards, always, whatever the diff touched:\n",
                  "```bash", _bullets(scope["guards"]), "```"]
    else:
        lines += ["\n3. Run the project's lint/typecheck once — that is cheap and",
                  "   catches what targeted tests structurally cannot."]
    lines += [
        "",
        "⛔Say what you ran. Your `summary` must name the scope you used and the",
        "  commands, so a reader can tell a targeted pass from a full one. If you",
        "  concluded the diff needs the full suite, run it and say why — that is a",
        "  judgement you are allowed to make, but not one you may make silently.",
        f"\n(scope source: {scope.get('source')})\n",
    ]
    return "\n".join(lines)


# ── the lock ──────────────────────────────────────────────────────────


def lock_path(worktree: str, *, base: str | None = None) -> str:
    """Host-wide lock file for a worktree, outside the worktree itself.

    Keyed on the real path so two processes that reached the same directory by
    different routes (symlink, relative path) still collide, with a readable
    prefix so `ls` in the lock directory is diagnosable.
    """
    real = os.path.realpath(worktree)
    digest = hashlib.sha256(real.encode()).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", real).strip("-")[-60:]
    return os.path.join(base or _base(), "locks", f"test-stage-{slug}-{digest}.lock")


@contextlib.contextmanager
def test_stage_lock(worktree: str, *, base: str | None = None):
    """Hold the worktree's test-stage lock, or yield ``False`` immediately.

    Non-blocking by design. The caller is a dispatcher tick that already knows
    how to put a task back and retry in two seconds; parking a role slot on a
    blocking acquire would trade a concurrency bug for a stall.

    ⛔Yields the boolean rather than raising, so "someone else is testing" and
      "the lock is broken" stay distinguishable — an unwritable lock directory
      yields True and lets the dispatch proceed. Refusing to test because a
      lock file could not be created would turn a hygiene mechanism into an
      outage.
    """
    path = lock_path(worktree, base=base)
    handle = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+")
    except OSError as e:
        logger.warning(f"test_stage_lock: cannot open {path} ({e}); proceeding unlocked")
        yield True
        return
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            logger.info(
                f"test_stage_lock: {worktree} is already running a test stage "
                f"(lock {path} held) — not starting a second one (#272)")
            yield False
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n{worktree}\n")
            handle.flush()
        except OSError:
            pass          # the lock is the flock, not the file's contents
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            handle.close()
