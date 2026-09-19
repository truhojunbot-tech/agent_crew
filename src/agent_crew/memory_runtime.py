"""ADR-001 durable, backend-neutral memory layer (disabled by default)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
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
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS adr001_memory (layer TEXT,key TEXT,value TEXT,scope TEXT,version INTEGER,created REAL, PRIMARY KEY(layer,key,scope))"); db.commit()
    def put(self, record: MemoryRecord) -> None:
        if record.layer not in LAYERS: raise ValueError("unknown memory layer")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)
                ON CONFLICT(layer,key,scope) DO UPDATE SET value=excluded.value,version=excluded.version,created=excluded.created
                WHERE excluded.version > adr001_memory.version""",
                (record.layer, record.key, json.dumps(record.value), json.dumps(asdict(record.scope), sort_keys=True), record.version, time.time())); db.commit()
    def retrieve(self, scope: MemoryScope, query: str = "", exact_key: str = "") -> list[MemoryRecord]:
        fields = asdict(scope)
        clauses, params = [], []
        for name, value in fields.items():
            # A stored empty field is an ancestor; a nonempty one must agree.
            clauses.append("(json_extract(scope, ?) IS NULL OR json_extract(scope, ?) = '' OR json_extract(scope, ?) = ?)")
            path = f"$.{name}"; params.extend((path, path, path, value))
        if exact_key:
            clauses.append("key=?"); params.append(exact_key)
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT layer,key,value,scope,version FROM adr001_memory WHERE " + " AND ".join(clauses), params).fetchall()
        result = [MemoryRecord(r[0], r[1], json.loads(r[2]), MemoryScope(**json.loads(r[3])), r[4]) for r in rows]
        terms = set(query.lower().replace('-', ' ').split())
        def rank(record):
            text = (record.key + ' ' + json.dumps(record.value)).lower().replace('-', ' ')
            relevance = len(terms.intersection(text.split()))
            specificity = sum(bool(value) for value in asdict(record.scope).values())
            return (-relevance, -specificity, record.key)
        return sorted(result, key=rank)


def memory_enabled() -> bool:
    return os.getenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def reconstruct_context(storage: MemoryStorage, role: str, task_id: str, scope: MemoryScope) -> dict:
    """Quality-first pack: authoritative refs are always included; no budget trimming."""
    if not memory_enabled(): return {"enabled": False, "records": []}
    records = storage.retrieve(scope, query=f"{role} {task_id}") + storage.retrieve(scope, exact_key=task_id)
    # Truth and checkpoint state are mandatory quality inputs, independent of
    # lexical relevance or any future economics budget.
    records += [r for r in storage.retrieve(scope) if r.layer in {"authoritative", "checkpoint"}]
    unique = {}
    for record in records:
        # retrieve() is specificity-descending; first is the nearest truth.
        unique.setdefault((record.layer, record.key), record)
    return {"enabled": True, "role": role, "task_id": task_id,
            "records": [asdict(r) for r in unique.values()]}
