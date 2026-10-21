"""Bounded background retry for cascade enqueues that hit a transient sqlite lock (#816).

Measured 2026-10-21 00:23:07Z on 8105 (build e695aaa): under host IO load of
13-20 on 16 cores, ``POST /tasks/<fix>/result`` was accepted with 200, the
cascade logged ``auto_enqueue_review: unexpected error: database is locked``
once, and the #812 lineage stopped. Nothing retried the enqueue and nothing
told the coordinator. Of all the writes that lock lost that night (shadow
receipts, heartbeats, work-intent liveness) the lineage cascade is the only
one that silently stops work, so it is the one that gets a retry.

Shape:

* The request thread makes the inline attempt exactly as before. A lock there
  schedules a daemon thread; the HTTP result is never delayed by the retry.
* Each retry re-runs the WHOLE cascade function. The cascade is idempotent —
  deterministic successor ids, an existence check before the insert, and
  ``TaskAlreadyExistsError``/``DuplicateReviewError`` on the insert itself —
  so a retry after a partial success (row written, lock surfaced on a later
  write) finds the existing successor and creates nothing.
* The schedule is bounded: the inline attempt plus ``retry_delays()`` retries,
  by default four over ~60 s. Exhaustion emits ONE durable coordinator notice
  (``cascade_enqueue_failed``) that names the lineage and the missing step.
  A WARNING line alone is not enough — that is exactly what #816 had.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Callable, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Durable coordinator-notice kind for an exhausted cascade retry.
NOTICE_KIND = "cascade_enqueue_failed"

#: Retry delays after the inline attempt: 5 attempts over 60 s in total.
DEFAULT_DELAYS: tuple[float, ...] = (5.0, 10.0, 15.0, 30.0)

#: Comma-separated seconds between retries, e.g. ``"5,10,15,30"``.
DELAYS_ENV = "AGENT_CREW_CASCADE_LOCK_RETRY_DELAYS"

#: How many times the exhaustion notice itself is retried when the queue is
#: still locked at that point (the claim is a sqlite write too).
_NOTICE_ATTEMPTS = 3


def is_database_locked(exc: BaseException) -> bool:
    """Only sqlite's busy-lock is transient; every other error stays one-shot."""
    return (type(exc) is sqlite3.OperationalError
            and "database is locked" in str(exc).lower())


def retry_delays(raw: Optional[str] = None) -> tuple[float, ...]:
    """Parse the retry schedule; anything malformed falls back to the default."""
    value = os.getenv(DELAYS_ENV, "") if raw is None else raw
    value = (value or "").strip()
    if not value:
        return DEFAULT_DELAYS
    try:
        delays = tuple(float(part) for part in value.split(","))
    except ValueError:
        logger.warning("%s=%r is not a comma-separated list of seconds; using %r",
                       DELAYS_ENV, value, DEFAULT_DELAYS)
        return DEFAULT_DELAYS
    if not delays or any(d < 0 or d != d for d in delays):
        logger.warning("%s=%r has a negative or NaN delay; using %r",
                       DELAYS_ENV, value, DEFAULT_DELAYS)
        return DEFAULT_DELAYS
    return delays


class CascadeEnqueueLocked(RuntimeError):
    """A cascade step hit ``database is locked`` (#816).

    Raised by ``pipeline.auto_enqueue_{review,fix,test}`` only when the caller
    passed ``raise_on_lock=True``; every other caller keeps the historical
    "warn and return None" behaviour. ``stage`` is the successor that is now
    missing (``review``/``fix``/``test``) and ``parent_task_id`` the task whose
    result should have produced it.
    """

    def __init__(self, stage: str, parent_task_id: str, cause: BaseException):
        super().__init__(f"auto_enqueue_{stage} for {parent_task_id}: {cause}")
        self.stage = stage
        self.parent_task_id = parent_task_id


# ``notice_fn(parent_task_id, stage, attempts, last_error) -> bool`` — True
# once the durable notice path ran to completion (delivered OR already
# claimed), False when the queue itself refused the claim.
NoticeFn = Callable[[str, str, int, str], bool]


class CascadeLockRetrier:
    """Run a cascade attempt inline and retry locked ones in the background."""

    def __init__(self, *, notice_fn: NoticeFn,
                 delays: Optional[Sequence[float]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 paused_error: tuple[type[BaseException], ...] = ()):
        self._notice_fn = notice_fn
        self._delays = tuple(delays if delays is not None else retry_delays())
        self._sleep = sleep
        self._paused_error = paused_error
        self._threads: list[threading.Thread] = []
        self._guard = threading.Lock()

    @property
    def delays(self) -> tuple[float, ...]:
        return self._delays

    def run(self, stage: str, parent_task_id: str, attempt: Callable[[], T],
            after: Optional[Callable[[Optional[T]], None]] = None) -> Optional[T]:
        """Inline attempt; on a lock, hand the same ``attempt`` to a retry thread.

        ``after(value)`` runs after every SUCCESSFUL attempt (inline or
        retried) with the cascade's return value — the HTTP wrappers use it
        for the tmux push. Returns the inline value, or ``None`` when the
        inline attempt was locked and a retry is now pending.
        """
        try:
            value = attempt()
        except CascadeEnqueueLocked as exc:
            logger.warning(
                "%s: inline attempt locked (%s); retrying in the background "
                "%d more time(s) over %.0f s", exc, "database is locked",
                len(self._delays), sum(self._delays))
            self._spawn(stage, parent_task_id, attempt, after)
            return None
        if after is not None:
            after(value)
        return value

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Join every pending retry thread (tests). True when none remain."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._guard:
                self._threads = [t for t in self._threads if t.is_alive()]
                pending = list(self._threads)
            if not pending:
                return True
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                return False
            pending[0].join(remaining)

    # ── internals ────────────────────────────────────────────────────────

    def _spawn(self, stage, parent_task_id, attempt, after) -> None:
        thread = threading.Thread(
            target=self._retry_loop, args=(stage, parent_task_id, attempt, after),
            daemon=True, name=f"agent-crew-cascade-retry-{stage}-{parent_task_id}")
        with self._guard:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _retry_loop(self, stage, parent_task_id, attempt, after) -> None:
        attempts = 1
        last_error = "database is locked"
        for delay in self._delays:
            self._sleep(delay)
            attempts += 1
            try:
                value = attempt()
            except CascadeEnqueueLocked as exc:
                last_error = str(exc)
                logger.warning("cascade retry %d/%d for %s/%s still locked",
                               attempts, len(self._delays) + 1, stage, parent_task_id)
                continue
            except self._paused_error:
                # STOP/pause is an explicit decision, not a lost step: the
                # pipeline already reopened the parent outbox for replay.
                logger.info("cascade retry for %s/%s stopped: queue paused",
                            stage, parent_task_id)
                return
            except Exception:  # noqa: BLE001 — a retry must never take the server down
                logger.exception("cascade retry %d for %s/%s raised; giving up",
                                 attempts, stage, parent_task_id)
                self._notify(parent_task_id, stage, attempts, "unexpected error")
                return
            logger.info("cascade retry %d/%d for %s/%s succeeded (%r)",
                        attempts, len(self._delays) + 1, stage, parent_task_id, value)
            if after is not None:
                try:
                    after(value)
                except Exception:  # noqa: BLE001
                    logger.exception("cascade retry after-hook failed for %s/%s",
                                     stage, parent_task_id)
            return
        logger.error("cascade enqueue for %s/%s failed after %d attempts: %s",
                     stage, parent_task_id, attempts, last_error)
        self._notify(parent_task_id, stage, attempts, last_error)

    def _notify(self, parent_task_id, stage, attempts, last_error) -> None:
        """The notice claim is a sqlite write too; give it a few chances."""
        for index in range(_NOTICE_ATTEMPTS):
            try:
                if self._notice_fn(parent_task_id, stage, attempts, last_error):
                    return
            except Exception:  # noqa: BLE001
                logger.exception("cascade_enqueue_failed notice raised for %s/%s",
                                 stage, parent_task_id)
            if index + 1 < _NOTICE_ATTEMPTS:
                self._sleep(self._delays[min(index, len(self._delays) - 1)]
                            if self._delays else 0.0)
        logger.error("cascade_enqueue_failed notice could not be recorded for %s/%s "
                     "— the %s successor is missing and nobody was told",
                     stage, parent_task_id, stage)
