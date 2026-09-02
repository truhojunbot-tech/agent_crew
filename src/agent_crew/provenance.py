"""#248 — what build is this process actually running?

#247 found a deployment-control failure, not an algorithmic one: GitHub `main`
carried #238 while all four live dispatchers were still importing an older
editable checkout, so the merged fix had never executed in production. The
runaway agy conversation kept growing and the pre-fix behaviour was on course to
be attributed to a fix that never ran.

The failure was invisible because nothing the servers exposed said which build
they were on. This module makes that observable.

## The one rule that makes this useful

⛔A provenance report must describe the code this process LOADED, not the code
  sitting in the checkout when you ask. Those diverge exactly when it matters:
  someone pulls, the running process keeps executing the old bytes, and a naive
  `git rev-parse HEAD` at request time would report the new SHA and call a stale
  runtime current — reintroducing #247 through the very tool built to detect it.

So everything under `build()` is captured ONCE, at import, and frozen:

    commit / ref / dirty      the checkout HEAD when this process started
    code_fingerprint          hash of the .py files as they were on disk then
    started_at / pid          the process boundary a cohort can be cut on

and everything that can move is reported separately and compared:

    checkout_commit           HEAD right now
    checkout_fingerprint      the .py files right now
    source_changed_since_start / checkout_moved_since_start

A process whose `code_fingerprint` no longer matches the checkout is running
code that no longer exists on disk. That is precisely the state #247 was in, and
it is now a fact the server reports about itself.

## Non-goals

⛔Read-only, always. Nothing here pulls, restarts, or repairs anything: a
  safe-boundary deployment stays an operator action (#248 safety constraint).
  The most this module ever does is say "stale" loudly.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: Bound on the git calls made here. A provenance read must never hang a
#: health check — an unanswerable question is answered "unknown".
GIT_TIMEOUT_S = 5.0

#: Status values returned by `compare()`.
CURRENT = "current"
STALE = "stale"
UNKNOWN = "unknown"

_BUILD: Optional[dict] = None


def _git(args: list, cwd: str) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=GIT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — provenance never raises into a request
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def source_root() -> str:
    """Directory of the `agent_crew` package THIS process imported."""
    return os.path.dirname(os.path.abspath(__file__))


def fingerprint_source(root: str) -> tuple:
    """``(hex, file_count)`` over the package's ``.py`` files.

    Content-addressed rather than mtime-based: a `git pull` that restores a
    file to identical bytes is genuinely not a change, and a checkout with
    uncommitted edits is genuinely not its HEAD. The path is hashed with the
    content so a moved or deleted file registers.
    """
    h = hashlib.sha256()
    n = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                h.update(os.path.relpath(path, root).encode())
                h.update(b"\0")
                h.update(hashlib.sha256(data).digest())
                n += 1
    except Exception:  # noqa: BLE001
        return ("", 0)
    return (h.hexdigest(), n)


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("agent_crew")
        except PackageNotFoundError:
            return ""
    except Exception:  # noqa: BLE001
        return ""


def _process_start_time() -> float:
    """Process start as an epoch, from /proc where available.

    Import time is a good enough proxy elsewhere, but on Linux the real boot
    instant is available and is what an A/B cohort boundary should be cut on.
    """
    try:
        with open(f"/proc/{os.getpid()}/stat") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        starttime_ticks = int(fields[19])
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
        return time.time() - (uptime - starttime_ticks / hz)
    except Exception:  # noqa: BLE001
        return time.time()


def capture(root: str = "") -> dict:
    """Describe the build rooted at `root` (default: this package) right now.

    Split out from `build()` so the freezing and the measuring are separate
    concerns — and so a test can point it at a real throwaway checkout and then
    move that checkout underneath it, which is the only honest way to prove the
    freeze works.
    """
    root = root or source_root()
    repo = os.path.dirname(os.path.dirname(root))   # <repo>/src/agent_crew
    fp, n = fingerprint_source(root)
    return {
        "commit": _git(["rev-parse", "HEAD"], repo),
        "commit_short": _git(["rev-parse", "--short", "HEAD"], repo),
        "ref": _git(["rev-parse", "--abbrev-ref", "HEAD"], repo),
        "dirty": bool(_git(["status", "--porcelain"], repo)),
        "repo_root": repo,
        "source_root": root,
        "code_fingerprint": fp,
        "source_file_count": n,
        "package_version": _package_version(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "pid": os.getpid(),
        "started_at": _process_start_time(),
        "captured_at": time.time(),
        }


def build() -> dict:
    """The frozen, immutable description of what this process is running.

    Captured on first call — which the server makes at startup — and never
    recomputed. Later calls return the same dict even after the checkout moves,
    because that is the whole point.
    """
    global _BUILD
    if _BUILD is None:
        _BUILD = capture()
    return dict(_BUILD)


def snapshot(*, project: str = "", port: int = 0, role: str = "",
             build_info: Optional[dict] = None) -> dict:
    """Frozen build + what the checkout looks like RIGHT NOW + the comparison.

    The comparison fields are the operational payload: they answer "is this
    process still running the code that is on disk?" without shell access to
    the host, which is exactly what #247 needed and did not have.
    """
    b = dict(build_info) if build_info else build()
    live_fp, _ = fingerprint_source(b["source_root"])
    checkout_commit = _git(["rev-parse", "HEAD"], b["repo_root"])
    out = dict(b)
    out.update({
        "project": project,
        "port": port,
        "role": role,
        "checkout_commit": checkout_commit,
        "checkout_fingerprint": live_fp,
        # ⛔The two flags a stale runtime is detected by. `checkout_moved`
        #   means someone committed/pulled since this process started;
        #   `source_changed` means the FILES this process loaded are no longer
        #   the files on disk — the running code exists only in memory.
        "checkout_moved_since_start": bool(
            checkout_commit and b["commit"] and checkout_commit != b["commit"]),
        "source_changed_since_start": bool(
            live_fp and b["code_fingerprint"] and live_fp != b["code_fingerprint"]),
        "uptime_s": round(max(0.0, time.time() - b["started_at"]), 1),
    })
    return out


def compare(expected_ref: str, *, snap: Optional[dict] = None) -> dict:
    """Is the LOADED build at or after `expected_ref`?

    Returns `{"status": current|stale|unknown, "reason": str, ...}`.

    ⛔`unknown` is a distinct answer and is never collapsed into `current`. If
      the loaded commit is unreachable (force-pushed away), or git cannot be
      consulted, the honest report is that we cannot tell — #248 requires a
      stale build to fail validation or read as unknown, never to pass quietly.
    """
    snap = snap or snapshot()
    loaded = snap.get("commit") or ""
    repo = snap.get("repo_root") or ""
    if not expected_ref:
        return {"status": UNKNOWN, "reason": "no expected ref given",
                "loaded": loaded, "expected": expected_ref}
    if not loaded:
        return {"status": UNKNOWN, "reason": "loaded commit unknown (not a git checkout?)",
                "loaded": loaded, "expected": expected_ref}
    expected = _git(["rev-parse", expected_ref + "^{commit}"], repo)
    if not expected:
        return {"status": UNKNOWN, "reason": f"expected ref {expected_ref!r} not resolvable",
                "loaded": loaded, "expected": expected_ref}
    if not _git(["cat-file", "-e", loaded + "^{commit}"], repo) and _git(
            ["cat-file", "-t", loaded], repo) != "commit":
        return {"status": UNKNOWN,
                "reason": "loaded commit is not in the checkout any more",
                "loaded": loaded, "expected": expected}
    try:
        r = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", expected, loaded],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return {"status": UNKNOWN, "reason": "git comparison failed",
                "loaded": loaded, "expected": expected}
    if r.returncode == 0:
        status, reason = CURRENT, f"loaded build contains {expected_ref}"
    elif r.returncode == 1:
        status, reason = STALE, f"loaded build predates {expected_ref}"
    else:
        return {"status": UNKNOWN, "reason": "git comparison inconclusive",
                "loaded": loaded, "expected": expected}
    # A process whose source has changed under it is not "current" whatever
    # the SHAs say: the bytes it is executing are not the bytes on disk.
    if status == CURRENT and snap.get("source_changed_since_start"):
        return {"status": STALE, "loaded": loaded, "expected": expected,
                "reason": "source files changed since this process started — "
                          "it is running code that is no longer on disk; restart to adopt it"}
    return {"status": status, "reason": reason, "loaded": loaded, "expected": expected}


def summary_line(snap: dict, cmp: Optional[dict] = None) -> str:
    """One operator-readable line. Used by the CLI and the startup log."""
    bits = [
        f"{snap.get('project') or '(no project)'}",
        f"commit={snap.get('commit_short') or '?'}{'+dirty' if snap.get('dirty') else ''}",
        f"ref={snap.get('ref') or '?'}",
        f"pid={snap.get('pid')}",
        f"up={snap.get('uptime_s')}s",
    ]
    if snap.get("checkout_moved_since_start"):
        bits.append("CHECKOUT-MOVED")
    if snap.get("source_changed_since_start"):
        bits.append("SOURCE-CHANGED")
    if cmp:
        bits.append(f"vs-expected={cmp['status'].upper()}")
    return " ".join(bits)
