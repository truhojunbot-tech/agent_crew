"""Verified text submission into tmux panes (#888).

Every place that types text into an agent pane goes through ``submit``: take a
per-pane lock, deliver the text, press Enter, then read the pane back to
confirm the input actually left the input line, retrying Enter a bounded number
of times. ``send-keys`` returning 0 only proves tmux accepted keystrokes; it
says nothing about whether the TUI submitted them (#74: a dropped first Enter
left a task parked in the composer).

The pattern is borrowed from alfred's ``tools/pane_submit.sh`` and re-implemented
here: agent_crew must not depend on an alfred path.

Not routed through here, by design: ``C-c`` (no text to verify) and the gemini
``'2' Enter`` permission answer (a menu selection, not an input line).
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Callable, Generator

logger = logging.getLogger(__name__)

# Per-attempt wait after Enter. Widening backoff lets transient post-task UI
# states (footer redraws, cache writes) settle before the next Enter (#74).
RETRY_DELAYS = (0.3, 0.5, 1.0, 2.0)
PASTE_SETTLE = 0.5
_LOCK_ATTEMPTS = 50
_LOCK_POLL = 0.1
_PROBE_LEN = 40

# Prompt markers that open an agent CLI's input line: claude ❯ / >, codex ›.
_PROMPT_MARKS = ("❯", "›")
_BORDER_CHARS = "─━═"

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _lock_dir() -> str:
    return os.environ.get("AGENT_CREW_PANE_LOCK_DIR") or os.path.join(
        tempfile.gettempdir(), "agent_crew-pane-locks")


@contextmanager
def _locked(key: str) -> Generator[bool, None, None]:
    """Hold an in-process and a cross-process (flock) lock for ``key``.

    Yields False if the lock could not be taken within the bounded wait; the
    caller must then not inject anything.
    """
    with _thread_locks_guard:
        tlock = _thread_locks.setdefault(key, threading.Lock())
    with tlock:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        fh = None
        try:
            os.makedirs(_lock_dir(), exist_ok=True)
            fh = open(os.path.join(_lock_dir(), f"{safe}.lock"), "w")
        except OSError:
            # No lock dir is not a reason to stop delivering tasks; the
            # in-process lock still serialises this server's own writers.
            logger.warning("pane_submit: cannot open lock file for %s", key)
        got = fh is None
        if fh is not None:
            for _ in range(_LOCK_ATTEMPTS):
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except BlockingIOError:
                    time.sleep(_LOCK_POLL)
        try:
            yield got
        finally:
            if fh is not None:
                fh.close()


def squash(text: str) -> str:
    """Drop all whitespace and box-drawing so hard wraps cannot split a probe."""
    return "".join(c for c in "".join(text.split()) if not 0x2500 <= ord(c) <= 0x257F)


def has_prompt(screen: str) -> bool:
    return any(m in screen for m in _PROMPT_MARKS)


def input_region(screen: str) -> str:
    """The input line(s) of an agent CLI: after the last prompt marker, up to a
    horizontal border. Without a visible marker, the last few lines."""
    pos = max(screen.rfind(m) for m in _PROMPT_MARKS)
    if pos < 0:
        lines = [ln for ln in screen.splitlines() if ln.strip()]
        return "\n".join(lines[-3:])
    out = []
    for line in screen[pos + 1:].splitlines():
        visible = line.strip()
        if visible and any(c in _BORDER_CHARS for c in visible) and all(
                c.isspace() or 0x2500 <= ord(c) <= 0x257F for c in line):
            break
        out.append(line)
    return "\n".join(out)


def text_pending(text: str) -> Callable[[str], bool]:
    """Predicate: ``text`` (or a collapsed paste placeholder) still sits in the
    agent CLI's input line."""
    compact = squash(text)
    probes = {p for p in (compact[:_PROBE_LEN], compact[-_PROBE_LEN:]) if p}

    def pending(screen: str) -> bool:
        box = squash(input_region(screen))
        return "[Pastedtext" in box or any(p in box for p in probes)
    return pending


def shell_line_pending(text: str) -> Callable[[str], bool]:
    """Predicate for a command typed at a shell prompt: still pending while the
    screen ends with the command, i.e. nothing ran after it."""
    tail = squash(text)[-_PROBE_LEN:]

    def pending(screen: str) -> bool:
        lines = [ln for ln in screen.splitlines() if ln.strip()]
        return bool(tail) and squash("\n".join(lines[-3:])).endswith(tail)
    return pending


def capture(target: str) -> str | None:
    try:
        r = subprocess.run(["tmux", "capture-pane", "-p", "-t", target],
                           capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return None
    rc = getattr(r, "returncode", 0)
    if isinstance(rc, int) and rc != 0:
        return None  # cannot read the pane back: unverified, not submitted
    out = getattr(r, "stdout", "")
    return out if isinstance(out, str) else ""


def submit(target: str, text: str, *, pending: Callable[[str], bool] | None = None,
           paste: bool = True, settle: float = PASTE_SETTLE,
           delays: tuple[float, ...] = RETRY_DELAYS, label: str = "") -> str:
    """Deliver ``text`` to ``target`` and press Enter until the input is gone.

    Returns the observed outcome:
    - ``submitted``: a read-back showed the input line no longer holds ``text``;
    - ``unsubmitted``: still pending after ``len(delays)`` Enter attempts;
    - ``send_failed``: tmux refused the text, or the pane lock was busy.

    ``paste`` uses a bracketed paste (load-buffer + paste-buffer -p), which
    delivers multi-line text atomically; otherwise ``send-keys -l``.
    """
    if pending is None:
        pending = text_pending(text)
    label = label or target
    with _locked(target) as got:
        if not got:
            logger.warning("pane_submit: lock busy for %s (%s); nothing sent", target, label)
            return "send_failed"
        if paste:
            r1 = subprocess.run(["tmux", "load-buffer", "-"], input=text,
                                text=True, capture_output=True)
            r2 = subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-t", target],
                                capture_output=True)
            failed = getattr(r1, "returncode", 0) != 0 or getattr(r2, "returncode", 0) != 0
        else:
            r = subprocess.run(["tmux", "send-keys", "-l", "-t", target, text],
                               capture_output=True, text=True)
            failed = getattr(r, "returncode", 0) != 0
        if failed:
            logger.warning("pane_submit: tmux refused text for %s (%s)", target, label)
            return "send_failed"
        time.sleep(settle)
        for attempt, wait_after in enumerate(delays, start=1):
            subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True)
            time.sleep(wait_after)
            screen = capture(target)
            if screen is not None and not pending(screen):
                logger.info("pane_submit: %s submitted to %s (attempt %d/%d)",
                            label, target, attempt, len(delays))
                return "submitted"
            logger.warning("pane_submit: %s still pending on %s (attempt %d/%d)",
                           label, target, attempt, len(delays))
    logger.error("pane_submit: %s NOT submitted to %s after %d Enter attempts",
                 label, target, len(delays))
    return "unsubmitted"
