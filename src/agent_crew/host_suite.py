"""One advisory full-suite slot shared by every dispatcher on this host (#1082)."""

from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import time

logger = logging.getLogger(__name__)


@contextmanager
def host_suite_slot(*, kind, port=None, clock=time.monotonic, sleep=time.sleep,
                    on_check=None):
    """Hold the slot until the suite exits, or launch contended after one hour.

    Never unlink the file: all contenders must flock the same inode. Only the
    owner writes metadata; a deadline launch must not overwrite its holder.
    ``holder`` is the last owner observed while waiting, not stale idle metadata.
    """
    path = Path(os.environ.get("AGENT_CREW_HOST_SUITE_LOCK",
                               "~/.agent_crew/host_full_suite.lock")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    started = clock()
    holder = None
    acquired = False
    with path.open("a+") as handle:
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    handle.seek(0)
                    try:
                        holder = json.load(handle)
                    except (ValueError, OSError):
                        holder = None  # owner may be between truncate and flush
                waited = max(0.0, clock() - started)
                receipt = {"queue_wait_s": round(waited, 3), "holder": holder,
                           "host_suite_contended": not acquired}
                if on_check and not acquired:
                    on_check(receipt)
                if acquired or waited >= 3600.0:
                    break
                logger.info("host suite slot busy: %s", receipt)
                sleep(min(120.0, 3600.0 - waited))
            if acquired:
                handle.seek(0)
                handle.truncate()
                json.dump({"pid": os.getpid(),
                           "port": port if port is not None else os.getenv("AGENT_CREW_PORT"),
                           "kind": kind}, handle)
                handle.flush()
            yield receipt
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)
