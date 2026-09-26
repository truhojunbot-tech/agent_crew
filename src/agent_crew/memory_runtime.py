"""ADR-001 durable, backend-neutral memory layer (disabled by default)."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
import re
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
        elif index == 0 and deepest_set > 0 and not is_unset(query_value):
            # A project record with no fleet must not cross into a named
            # fleet. Other omitted intermediate dimensions are ancestors.
            return False
    return True


class MemoryStorage(Protocol):
    def put(self, record: MemoryRecord) -> None: ...
    def retrieve(self, scope: MemoryScope, query: str = "", exact_key: str = "") -> list[MemoryRecord]: ...
    def audit(self, scope: MemoryScope, exact_key: str) -> list[MemoryRecord]: ...


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
            if record.layer == "authoritative" and record.value.get("kind") == "owner_statement":
                # Serialize the identity check with the insert across processes.
                db.execute("BEGIN IMMEDIATE")
                value = record.value
                proof = value.get("verification", {})
                expected_key = json.dumps(
                    [value.get("bot"), "plugin:telegram:telegram", str(value.get("chat_id")),
                     str(value.get("message_id"))], ensure_ascii=False, separators=(",", ":"))
                if (record.scope != MemoryScope(project=value.get("bot"))
                        or record.key != expected_key or value.get("channel") != "plugin:telegram:telegram"
                        or record.version != 1
                        or not value.get("source_ref") or not value.get("timestamp")
                        or not isinstance(value.get("text"), str)
                        or hashlib.sha256(value["text"].encode()).hexdigest() != value.get("text_sha256")
                        or proof.get("status") != "VERIFIED"
                        or proof.get("chat_id") != value.get("chat_id")
                        or proof.get("user_id") != value.get("chat_id")
                        or str(proof.get("message_id")) != str(value.get("message_id"))
                        or proof.get("text_sha256") != value.get("text_sha256")):
                    raise ValueError("owner statement identity or verification invalid")
                existing = db.execute(
                    "SELECT value FROM adr001_memory WHERE layer=? AND key=? AND scope=?",
                    (record.layer, record.key, _canonical_scope_json(record.scope)),
                ).fetchone()
                if existing:
                    if json.loads(existing[0]) != record.value:
                        raise ValueError("owner statement identity is immutable")
                    return
                target = record.value.get("supersedes")
                if target:
                    original = db.execute(
                        "SELECT value FROM adr001_memory WHERE layer='authoritative' AND key=? AND scope=?",
                        (target, _canonical_scope_json(record.scope)),
                    ).fetchone()
                    if not original or json.loads(original[0]).get("kind") != "owner_statement":
                        raise ValueError("superseded owner statement not found in bot scope")
            elif record.layer == "authoritative":
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT value FROM adr001_memory WHERE layer=? AND key=? AND scope=?",
                    (record.layer, record.key, _canonical_scope_json(record.scope)),
                ).fetchone()
                if existing and json.loads(existing[0]).get("kind") == "owner_statement":
                    raise ValueError("owner statement identity is immutable")
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
        superseded_keys = {record.value["supersedes"] for record in result
                           if record.layer == "authoritative" and record.value.get("kind") == "owner_statement"
                           and record.value.get("supersedes")}
        result = [record for record in result
                  if _scope_applies(record.scope, scope)
                  and not record.value.get("invalidated_at")
                  and not record.value.get("superseded_at")
                  and not record.value.get("superseded")
                  and not (record.layer == "authoritative" and record.key in superseded_keys)]
        terms = set(query.lower().replace('-', ' ').split())
        def rank(record):
            text = (record.key + ' ' + json.dumps(record.value)).lower().replace('-', ' ')
            relevance = len(terms.intersection(text.split()))
            specificity = _scope_specificity(record.scope)
            return (-relevance, -specificity, record.key)
        return sorted(result, key=rank)

    def audit(self, scope: MemoryScope, exact_key: str) -> list[MemoryRecord]:
        """Read an original by exact identity and its linked corrections, including history."""
        if not exact_key:
            raise ValueError("exact_key is required for historical audit")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT layer,key,value,scope,version FROM adr001_memory WHERE layer='authoritative'"
            ).fetchall()
        records = [MemoryRecord(row[0], row[1], json.loads(row[2]), _scope_from_json(row[3]), row[4])
                   for row in rows]
        records = [record for record in records if _scope_applies(record.scope, scope)]
        linked = {exact_key}
        while True:
            expanded = linked | {record.key for record in records if record.value.get("supersedes") in linked}
            if expanded == linked:
                break
            linked = expanded
        if not any(record.key == exact_key for record in records):
            return []
        return sorted((record for record in records if record.key in linked),
                      key=lambda record: (record.value.get("timestamp") or "", record.key))


def memory_enabled() -> bool:
    return os.getenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def ingest_blackboard_entry(storage: MemoryStorage, frontmatter: dict) -> MemoryRecord:
    """Store one Blackboard frontmatter entry as its original raw episode."""
    required = ("id", "from", "type", "link", "topic", "status", "result_link")
    if not all(key in frontmatter for key in required) or not frontmatter["id"]:
        raise ValueError("incomplete Blackboard frontmatter")
    link = str(frontmatter["link"])
    issue = re.search(r"/(?:issues|pull)/(\d+)\b|#(\d+)\b", link)
    record = MemoryRecord(
        "episodic", str(frontmatter["id"]), dict(frontmatter),
        MemoryScope(project=str(frontmatter["from"]), issue=next(
            (part for part in issue.groups() if part), "") if issue else ""),
    )
    storage.put(record)
    return record


class RuntimeMemoryProvider:
    """Shadow-only adapter from MemoryStorage to memory.MemoryProvider."""

    name = "memory_runtime"
    backend = "sqlite"

    def __init__(self, storage: MemoryStorage, *, fleet: str = ""):
        self.storage = storage
        self.fleet = fleet

    def retrieve(self, request):
        from .memory import MemoryItem, MemoryResult

        if not memory_enabled():
            return MemoryResult(provider=self.name, backend=self.backend, state="unavailable")
        if not request.project:
            return MemoryResult(provider=self.name, backend=self.backend, state="empty")
        scope = MemoryScope(fleet=self.fleet, project=request.project,
                            issue=request.issue, task_id=request.task_id,
                            context_generation=request.context_generation)
        records = self.storage.retrieve(scope, query=request.retrieval_query)
        allowed = set(request.memory_types) if request.memory_types else {"procedural", "episodic"}
        records = [record for record in records if record.layer in allowed
                   and record.layer in {"procedural", "episodic"}]
        items = tuple(MemoryItem(
            item_id=record.key,
            project=record.scope.project,
            memory_type=record.layer,
            source_ref=str(record.value.get("link") or record.value.get("source_ref") or record.key),
            excerpt=str(record.value.get("topic") or record.value.get("text") or ""),
            rank=index,
        ) for index, record in enumerate(records[:max(0, request.limit)], 1))
        return MemoryResult(provider=self.name, backend=self.backend,
                            state="results" if items else "empty", items=items)


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
