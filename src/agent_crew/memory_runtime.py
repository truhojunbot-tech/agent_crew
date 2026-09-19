"""ADR-001 durable, backend-neutral memory layer (disabled by default)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Protocol

LAYERS = frozenset({"authoritative", "checkpoint", "procedural", "episodic"})


@dataclass(frozen=True)
class MemoryScope:
    fleet: str = ""; project: str = ""; worktree: str = ""; issue: str = ""
    task_id: str = ""; context_generation: int = 0; provider_session: str = ""


@dataclass(frozen=True)
class MemoryRecord:
    layer: str; key: str; value: dict; scope: MemoryScope; version: int = 1


class MemoryStorage(Protocol):
    def put(self, record: MemoryRecord) -> None: ...
    def retrieve(self, scope: MemoryScope, query: str = "", exact_key: str = "") -> list[MemoryRecord]: ...


class SQLiteMemoryStorage:
    """Local-first POC; callers depend only on :class:`MemoryStorage`."""
    def __init__(self, path: str):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS adr001_memory (layer TEXT,key TEXT,value TEXT,scope TEXT,version INTEGER,created REAL, PRIMARY KEY(layer,key,scope))")
    def put(self, record: MemoryRecord) -> None:
        if record.layer not in LAYERS: raise ValueError("unknown memory layer")
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO adr001_memory VALUES (?,?,?,?,?,?)", (record.layer, record.key, json.dumps(record.value), json.dumps(asdict(record.scope), sort_keys=True), record.version, time.time()))
    def retrieve(self, scope: MemoryScope, query: str = "", exact_key: str = "") -> list[MemoryRecord]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT layer,key,value,scope,version FROM adr001_memory WHERE scope=?", (json.dumps(asdict(scope), sort_keys=True),)).fetchall()
        result = [MemoryRecord(r[0], r[1], json.loads(r[2]), MemoryScope(**json.loads(r[3])), r[4]) for r in rows]
        if exact_key: return [r for r in result if r.key == exact_key]
        q = query.lower()
        return [r for r in result if not q or q in r.key.lower() or q in json.dumps(r.value).lower()]


def memory_enabled() -> bool:
    return os.getenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def reconstruct_context(storage: MemoryStorage, role: str, task_id: str, scope: MemoryScope) -> dict:
    """Quality-first pack: authoritative refs are always included; no budget trimming."""
    if not memory_enabled(): return {"enabled": False, "records": []}
    records = storage.retrieve(scope, query=role) + storage.retrieve(scope, exact_key=task_id)
    unique = {(r.layer, r.key): r for r in records}
    return {"enabled": True, "role": role, "task_id": task_id,
            "records": [asdict(r) for r in unique.values()]}
