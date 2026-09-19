"""ADR-001 durable, backend-neutral memory layer (disabled by default)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, asdict
from typing import Optional, Protocol

LAYERS = frozenset({"authoritative", "checkpoint", "procedural", "episodic"})


@dataclass(frozen=True)
class MemoryScope:
    fleet: str = ""; project: str = ""; worktree: str = ""; issue: str = ""
    task_id: str = ""; context_generation: Optional[int] = None; provider_session: str = ""


@dataclass(frozen=True)
class MemoryRecord:
    layer: str; key: str; value: dict; scope: MemoryScope; version: int = 1


def _scope_from_json(scope: str) -> MemoryScope:
    fields = json.loads(scope)
    # ADR-001 previously encoded the unset generation as 0.  It was never a
    # concrete generation, so normalize it before matching or ranking records.
    if fields.get("context_generation") == 0:
        fields["context_generation"] = None
    return MemoryScope(**fields)


def _canonical_scope_json(scope: MemoryScope) -> str:
    fields = asdict(scope)
    # Generation 0 represented "unset" before ADR-001 made the field
    # optional.  Canonicalize it on write so it cannot create a second key.
    if fields["context_generation"] == 0:
        fields["context_generation"] = None
    return json.dumps(fields, sort_keys=True)


def _scope_specificity(scope: MemoryScope) -> int:
    return sum(value not in ("", None) for value in asdict(scope).values())


def _scope_applies(record_scope: MemoryScope, query_scope: MemoryScope) -> bool:
    """Return whether a stored scope applies to a query under ADR-001 order."""
    record_fields = list(asdict(record_scope).items())
    query_fields = dict(asdict(query_scope))
    # Generation zero was the legacy representation of unset.  Preserve that
    # meaning even for callers that still construct a scope with zero.
    if query_fields["context_generation"] == 0:
        query_fields["context_generation"] = None

    def is_unset(value: object) -> bool:
        return value in ("", None)

    deepest_set = max(
        (index for index, (_, value) in enumerate(record_fields) if not is_unset(value)),
        default=-1,
    )
    for index, (name, record_value) in enumerate(record_fields):
        query_value = query_fields[name]
        if not is_unset(record_value):
            if record_value != query_value:
                return False
        elif index <= deepest_set and not is_unset(query_value):
            # An unset ancestor of the record's deepest set dimension is an
            # exact-unset requirement, not a wildcard into another branch.
            return False
    return True


class MemoryStorage(Protocol):
    def put(self, record: MemoryRecord) -> None: ...
    def retrieve(self, scope: MemoryScope, query: str = "", exact_key: str = "") -> list[MemoryRecord]: ...


class SQLiteMemoryStorage:
    """Local-first POC; callers depend only on :class:`MemoryStorage`."""
    def __init__(self, path: str):
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS adr001_memory (layer TEXT,key TEXT,value TEXT,scope TEXT,version INTEGER,created REAL, PRIMARY KEY(layer,key,scope))")
            self._migrate_legacy_generation_zero(db)
            db.commit()

    @staticmethod
    def _migrate_legacy_generation_zero(db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT layer,key,value,scope,version,created FROM adr001_memory WHERE json_extract(scope, '$.context_generation') = 0").fetchall()
        for layer, key, value, legacy_scope, version, created in rows:
            scope = _canonical_scope_json(_scope_from_json(legacy_scope))
            existing = db.execute(
                "SELECT version FROM adr001_memory WHERE layer=? AND key=? AND scope=?",
                (layer, key, scope),
            ).fetchone()
            if existing is None:
                db.execute(
                    "UPDATE adr001_memory SET scope=? WHERE layer=? AND key=? AND scope=?",
                    (scope, layer, key, legacy_scope),
                )
            else:
                if version > existing[0]:
                    db.execute(
                        "UPDATE adr001_memory SET value=?,version=?,created=? WHERE layer=? AND key=? AND scope=?",
                        (value, version, created, layer, key, scope),
                    )
                db.execute(
                    "DELETE FROM adr001_memory WHERE layer=? AND key=? AND scope=?",
                    (layer, key, legacy_scope),
                )

    def put(self, record: MemoryRecord) -> None:
        if record.layer not in LAYERS: raise ValueError("unknown memory layer")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)
                ON CONFLICT(layer,key,scope) DO UPDATE SET value=excluded.value,version=excluded.version,created=excluded.created
                WHERE excluded.version > adr001_memory.version""",
                (record.layer, record.key, json.dumps(record.value), _canonical_scope_json(record.scope), record.version, time.time())); db.commit()
    def retrieve(self, scope: MemoryScope, query: str = "", exact_key: str = "") -> list[MemoryRecord]:
        fields = asdict(scope)
        clauses, params = [], []
        for name, value in fields.items():
            # A stored empty field is an ancestor; a nonempty one must agree.
            path = f"$.{name}"
            if name == "context_generation":
                clauses.append("(json_extract(scope, ?) IS NULL OR json_extract(scope, ?) = '' OR json_extract(scope, ?) = 0 OR json_extract(scope, ?) = ?)")
                params.extend((path, path, path, path, value))
            else:
                clauses.append("(json_extract(scope, ?) IS NULL OR json_extract(scope, ?) = '' OR json_extract(scope, ?) = ?)")
                params.extend((path, path, path, value))
        if exact_key:
            clauses.append("key=?"); params.append(exact_key)
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT layer,key,value,scope,version FROM adr001_memory WHERE " + " AND ".join(clauses), params).fetchall()
        result = [
            MemoryRecord(r[0], r[1], json.loads(r[2]), _scope_from_json(r[3]), r[4])
            for r in rows
        ]
        result = [record for record in result if _scope_applies(record.scope, scope)]
        terms = set(query.lower().replace('-', ' ').split())
        def rank(record):
            text = (record.key + ' ' + json.dumps(record.value)).lower().replace('-', ' ')
            relevance = len(terms.intersection(text.split()))
            specificity = _scope_specificity(record.scope)
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
        identity = (record.layer, record.key)
        # Records arrive relevance-first.  Scope specificity takes precedence;
        # retaining the existing value on ties preserves relevance rank.
        if identity not in unique or _scope_specificity(record.scope) > _scope_specificity(unique[identity].scope):
            unique[identity] = record
    return {"enabled": True, "role": role, "task_id": task_id,
            "records": [asdict(r) for r in unique.values()]}
