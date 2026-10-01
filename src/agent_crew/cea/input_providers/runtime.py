"""P6 runtime-state provider — a read-only view of the ``runtime_stop`` row.

Opens the queue DB with ``mode=ro`` and reads the P6 columns step 1 added
(``state, epoch, reason, decision_id, updated_at``). It never reconciles
``pause.json`` or writes anything: ``TaskQueue.get_runtime_state`` owns that,
and a provider that wrote would be a second store (P6). Any failure — no file,
no row, an unknown state — is ``read_failed=True``, which the engine treats as
STOPPED (P7).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot


class QueueRuntimeStateProvider:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def current(self) -> RuntimeStateSnapshot:
        failed = RuntimeStateSnapshot(state=RuntimeState.STOPPED, epoch=0, read_failed=True)
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            return failed
        try:
            row = conn.execute("SELECT state, epoch, reason, decision_id, updated_at "
                               "FROM runtime_stop WHERE id = 1").fetchone()
        except sqlite3.Error:
            return failed
        finally:
            conn.close()
        if row is None:
            return failed
        try:
            state = RuntimeState(row[0])
        except ValueError:
            return failed
        return RuntimeStateSnapshot(state=state, epoch=int(row[1] or 0), reason=row[2],
                                    decision_id=row[3], updated_at=_num(row[4]))


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
