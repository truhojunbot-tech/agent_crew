"""ADR-001 durable, backend-neutral memory layer (disabled by default)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import re
from contextlib import closing
from dataclasses import dataclass, asdict
from typing import Optional, Protocol

LAYERS = frozenset({"authoritative", "checkpoint", "procedural", "episodic",
                    "decision", "failure_pattern"})
SHADOW_RETENTION_ROWS_PER_PROJECT = 900
SHADOW_PRUNE_BATCH_ROWS = 50
SHADOW_RETRIEVAL_MAX_ROWS = 50
SHADOW_MEMORY_DEFAULT_TIMEOUT_SECONDS = 0.05


def shadow_sqlite_timeout_seconds(*, capture: bool = False) -> float:
    """Use the configured wait for retrieval; bound synchronous capture waits."""
    try:
        configured = float(os.getenv("AGENT_CREW_SHADOW_MEMORY_TIMEOUT_SECONDS",
                                     str(SHADOW_MEMORY_DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        configured = SHADOW_MEMORY_DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(configured) or configured <= 0:
        configured = SHADOW_MEMORY_DEFAULT_TIMEOUT_SECONDS
    return min(SHADOW_MEMORY_DEFAULT_TIMEOUT_SECONDS, configured) if capture else configured


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


def _retrieval_rank(record: MemoryRecord, terms: set[str]) -> tuple[int, int, str]:
    """Shared relevance and scope order for both storage retrieval paths."""
    text = (record.key + ' ' + json.dumps(record.value)).lower().replace('-', ' ')
    relevance = len(terms.intersection(text.split()))
    return (-relevance, -_scope_specificity(record.scope), record.key)


def _scope_applies(record_scope: MemoryScope, query_scope: MemoryScope,
                   *, strict: bool = True) -> bool:
    """Return whether a stored scope applies to a query under ADR-001 order."""
    if not strict:
        # Shadow comparisons can use evidence from a prior task when the
        # request omits a dimension. A named fleet remains an exact boundary.
        if record_scope.project != query_scope.project:
            return False
        if record_scope.fleet != query_scope.fleet:
            return False
        for name in ("worktree", "issue", "task_id", "context_generation",
                     "provider_session"):
            wanted = getattr(query_scope, name)
            actual = getattr(record_scope, name)
            unset = ("", None, 0) if name == "context_generation" else ("", None)
            if wanted not in unset and actual not in (*unset, wanted):
                return False
        return True
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


class SQLiteMemoryStorage:
    """Local-first POC; callers depend only on :class:`MemoryStorage`."""
    def __init__(self, path: str):
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS adr001_memory (layer TEXT,key TEXT,value TEXT,scope TEXT,version INTEGER,created REAL, PRIMARY KEY(layer,key,scope))")
            self._migrate_legacy_generation_zero(db)
            db.commit()

    @classmethod
    def existing(cls, path: str) -> "SQLiteMemoryStorage":
        """Use a provisioned shadow DB without running schema migration on POST."""
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        instance = cls.__new__(cls)
        instance.path = path
        return instance

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
            if record.layer == "authoritative" and record.key.startswith("owner:"):
                # Owner source records are immutable even when a caller supplies
                # a higher version. A correction has its own message identity.
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute(
                    "SELECT value FROM adr001_memory WHERE layer=? AND key=? AND scope=?",
                    (record.layer, record.key, _canonical_scope_json(record.scope)),
                ).fetchone()
                if previous:
                    if json.loads(previous[0]) != record.value:
                        raise ValueError("owner statement is immutable")
                    return
            db.execute("""INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)
                ON CONFLICT(layer,key,scope) DO UPDATE SET value=excluded.value,version=excluded.version,created=excluded.created
                WHERE excluded.version > adr001_memory.version""",
                (record.layer, record.key, json.dumps(record.value), _canonical_scope_json(record.scope), record.version, time.time())); db.commit()

    def put_many_shadow(self, records: list[MemoryRecord], *, retire_keys: tuple[str, ...] = (),
                        max_rows_per_project: int = SHADOW_RETENTION_ROWS_PER_PROJECT) -> None:
        """Batch derived evidence in one bounded transaction, pruning old captures."""
        if not records:
            return
        project = records[0].scope.project
        if not project or any(r.scope.project != project or r.layer not in {
                "episodic", "decision", "failure_pattern"} for r in records):
            raise ValueError("shadow batch requires one project and derived layers")
        with closing(sqlite3.connect(self.path, timeout=shadow_sqlite_timeout_seconds(capture=True))) as db:
            db.execute("BEGIN IMMEDIATE")
            for record in records:
                scope = _canonical_scope_json(record.scope)
                old_version = db.execute(
                    "SELECT MAX(version) FROM adr001_memory WHERE layer=? AND key=? "
                    "AND json_extract(scope,'$.project')=?",
                    (record.layer, record.key, project)).fetchone()[0] or 0
                # A later result may know more scope fields than the first
                # capture. These task/episode keys identify one observation;
                # keep its newest scope without leaving an older duplicate.
                db.execute("DELETE FROM adr001_memory WHERE layer=? AND key=? "
                           "AND json_extract(scope,'$.project')=? AND scope<>?",
                           (record.layer, record.key, project, scope))
                prior = db.execute("SELECT value,version FROM adr001_memory WHERE layer=? AND key=? AND scope=?",
                                   (record.layer, record.key, scope)).fetchone()
                if prior and json.loads(prior[0]) == record.value:
                    continue
                version = old_version + 1
                db.execute("""INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)
                    ON CONFLICT(layer,key,scope) DO UPDATE SET value=excluded.value,
                    version=excluded.version,created=excluded.created""",
                    (record.layer, record.key, json.dumps(record.value), scope,
                     version, time.time()))
            for key in retire_keys:
                # Preserve the failed observation for audit. Both retrieval
                # paths already exclude values with a superseded_at timestamp;
                # the ordinary retention prune still ages this row out.
                db.execute("UPDATE adr001_memory SET value=json_set(value,'$.superseded_at',?) "
                           "WHERE layer='failure_pattern' AND key=? "
                           "AND json_extract(scope,'$.project')=? "
                           "AND json_extract(value,'$.superseded_at') IS NULL",
                           (time.time(), key, project))
            db.execute("""DELETE FROM adr001_memory WHERE rowid IN (
                SELECT rowid FROM adr001_memory
                WHERE layer IN ('episodic','decision','failure_pattern')
                  AND json_extract(scope, '$.project')=?
                  AND (key GLOB 'task:*' OR key GLOB 'episode:*')
                ORDER BY created DESC,rowid DESC LIMIT ? OFFSET ?)""",
                (project, SHADOW_PRUNE_BATCH_ROWS, max_rows_per_project))
            db.commit()
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
        result = [record for record in result
                  if _scope_applies(record.scope, scope)
                  and not record.value.get("invalidated_at")
                  and not record.value.get("superseded_at")
                  and not record.value.get("superseded")]
        terms = set(query.lower().replace('-', ' ').split())
        return sorted(result, key=lambda record: _retrieval_rank(record, terms))

    def retrieve_shadow(self, scope: MemoryScope, layers: set[str], limit: int,
                        query: str = ""
                        ) -> tuple[list[MemoryRecord], int]:
        """Bound parsing to recent candidates and count project-less drops."""
        if not scope.project or not layers or limit <= 0:
            return [], 0
        clauses, params = [], []
        for name, value in asdict(scope).items():
            if name == "project":
                continue
            # Shadow evidence from a prior task can still inform a later task
            # when the request has no value for that dimension. The live pack
            # continues to use the strict scope matcher in retrieve().
            if name != "fleet" and value in ("", None):
                continue
            path = f"$.{name}"
            if name == "fleet":
                clauses.append("COALESCE(json_extract(scope, ?),'') = ?")
                params.extend((path, value))
            elif name == "context_generation":
                clauses.append("(json_extract(scope, ?) IS NULL OR json_extract(scope, ?) = '' "
                               "OR json_extract(scope, ?) = 0 OR json_extract(scope, ?) = ?)")
                params.extend((path, path, path, path, value))
            else:
                clauses.append("(json_extract(scope, ?) IS NULL OR json_extract(scope, ?) = '' "
                               "OR json_extract(scope, ?) = ?)")
                params.extend((path, path, path, value))
        base = ("layer IN (" + ",".join("?" for _ in layers) + ") AND "
                + " AND ".join(clauses)
                + " AND json_extract(value,'$.invalidated_at') IS NULL"
                + " AND json_extract(value,'$.superseded_at') IS NULL"
                + " AND COALESCE(json_extract(value,'$.superseded'),0)=0")
        args = [*sorted(layers), *params]
        with closing(sqlite3.connect(self.path, timeout=shadow_sqlite_timeout_seconds())) as db:
            dropped = db.execute("SELECT count(*) FROM adr001_memory WHERE " + base +
                " AND COALESCE(json_extract(scope,'$.project'),'')=''", args).fetchone()[0]
            rows = db.execute("SELECT layer,key,value,scope,version FROM adr001_memory WHERE "
                + base + " AND json_extract(scope,'$.project')=? "
                "ORDER BY created DESC,rowid DESC LIMIT ?",
                [*args, scope.project, (SHADOW_RETRIEVAL_MAX_ROWS if query.strip()
                                        else min(limit, SHADOW_RETRIEVAL_MAX_ROWS))]).fetchall()
        records = [MemoryRecord(layer, key, json.loads(value), _scope_from_json(record_scope),
                                version) for layer, key, value, record_scope, version in rows]
        scoped = [r for r in records if _scope_applies(r.scope, scope, strict=False)]
        if query.strip():
            terms = set(query.lower().replace('-', ' ').split())
            scoped.sort(key=lambda record: _retrieval_rank(record, terms))
        return scoped[:min(limit, SHADOW_RETRIEVAL_MAX_ROWS)], dropped


def owner_statement_key(project: str, channel: str, chat_id: str,
                        message_id: str) -> str:
    """Exact owner identity: target bot, verified channel, chat and message."""
    parts = (project, channel, chat_id, message_id)
    if any(not isinstance(part, str) or not part or ":" in part for part in parts):
        raise ValueError("incomplete owner statement identity")
    return "owner:" + ":".join(parts)


def capture_owner_statement(storage: MemoryStorage, *, project: str,
                            target_project: str, proof: dict, text: str,
                            source_ref: str, supersedes: Optional[list] = None,
                            observed_at: str = ""
                            ) -> MemoryRecord:
    """Write an independently verified message to the existing authoritative layer.

    The caller must obtain ``proof`` from owner_channel.verify_telegram on the
    exact source text. No inferred bot, sender, correction or authority is accepted.
    """
    if not project or project != target_project or not source_ref:
        raise ValueError("owner source target or provenance missing/mismatched")
    if not isinstance(proof, dict) or proof.get("status") != "VERIFIED":
        raise ValueError("owner message not verified")
    chat, sender, mid = (str(proof.get(k) or "") for k in
                         ("chat_id", "user_id", "message_id"))
    if not chat or chat != sender or not mid or not proof.get("ts"):
        raise ValueError("owner chat/message identity not verified")
    if not isinstance(text, str) or not text or proof.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest():
        raise ValueError("owner text hash mismatch")
    key = owner_statement_key(project, "telegram", chat, mid)
    links = supersedes or []
    if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
        raise ValueError("invalid supersedes links")
    scope = MemoryScope(project=project)
    for link in links:
        prior = [r for r in storage.retrieve(scope, exact_key=link)
                 if r.layer == "authoritative" and r.scope.project == project
                 and r.value.get("kind") == "owner_statement"]
        if not prior or link == key:
            raise ValueError("superseded owner message is absent or outside target bot")
    record = MemoryRecord("authoritative", key, {
        "kind": "owner_statement", "text": text, "text_sha256": proof["text_sha256"],
        "message_id": mid, "chat_id": chat, "channel": "telegram",
        "timestamp": observed_at or str(proof["ts"]),
        "channel_ts": str(proof["ts"]), "source_ref": source_ref,
        "verification_status": "VERIFIED", "supersedes": links,
    }, scope)
    storage.put(record)
    return record


def effective_owner_statements(storage: MemoryStorage, project: str) -> list[MemoryRecord]:
    """PR #57 correction resolution on records from one scoped memory store."""
    records = _scoped_owner_records(storage, project)
    superseded = {link for record in records
                  for link in record.value.get("supersedes", [])}
    return [r for r in records if r.key not in superseded]


def owner_statement_history(storage: MemoryStorage, project: str,
                            exact_key: str) -> list[MemoryRecord]:
    """Read the immutable original and linked corrections for audit only."""
    records = _scoped_owner_records(storage, project)
    originals = [r for r in records if r.key == exact_key]
    if not originals:
        return []
    seen = {exact_key}
    while True:
        added = {r.key for r in records if r.key not in seen
                 and any(link in seen for link in r.value.get("supersedes", []))}
        if not added:
            break
        seen.update(added)
    return originals + [r for r in records if r.key in seen and r.key != exact_key]


def _scoped_owner_records(storage: MemoryStorage, project: str) -> list[MemoryRecord]:
    if not project:
        raise ValueError("owner project is required")
    from .memory import same_memory_project
    records = [r for r in storage.retrieve(MemoryScope(project=project))
               if r.layer == "authoritative" and r.value.get("kind") == "owner_statement"
               and same_memory_project(project, r.scope.project)]
    for record in records:
        value = record.value
        expected = owner_statement_key(project, "telegram", str(value.get("chat_id") or ""),
                                       str(value.get("message_id") or ""))
        if (record.key != expected or value.get("verification_status") != "VERIFIED"
                or value.get("text_sha256") != hashlib.sha256(
                    str(value.get("text") or "").encode()).hexdigest()):
            raise ValueError("invalid authoritative owner record")
    keys = {record.key for record in records}
    for record in records:
        links = record.value.get("supersedes", [])
        if (not isinstance(links, list)
                or any(not isinstance(link, str) or link not in keys for link in links)):
            raise ValueError("invalid owner statement supersedes link")
    return records


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
        from .memory import MemoryItem, MemoryResult, same_memory_project

        # This adapter is only used by the shadow dispatch seam. Its flag is
        # AGENT_CREW_SHADOW_MEMORY_ENABLED; the live-read flag belongs solely
        # to reconstruct_context and must not suppress a shadow comparison.
        if not request.project:
            return MemoryResult(provider=self.name, backend=self.backend, state="empty")
        scope = MemoryScope(fleet=self.fleet, project=request.project,
                            issue=request.issue, task_id=request.task_id,
                            context_generation=request.context_generation)
        allowed = set(request.memory_types) if request.memory_types else {
            "procedural", "episodic", "decision", "failure_pattern"}
        allowed &= {"procedural", "episodic", "decision", "failure_pattern"}
        if isinstance(self.storage, SQLiteMemoryStorage):
            scoped, dropped = self.storage.retrieve_shadow(
                scope, allowed, request.limit, query=request.retrieval_query)
        else:
            records = [record for record in self.storage.retrieve(scope,
                query=request.retrieval_query) if record.layer in allowed]
            scoped = [r for r in records if same_memory_project(request.project, r.scope.project)]
            dropped = len(records) - len(scoped)
        items = tuple(MemoryItem(
            item_id=record.key,
            project=record.scope.project,
            memory_type=record.layer,
            source_ref=str(record.value.get("link") or record.value.get("source_ref") or record.key),
            excerpt=str(record.value.get("topic") or record.value.get("text") or ""),
            rank=index,
        ) for index, record in enumerate(scoped[:max(0, request.limit)], 1))
        return MemoryResult(provider=self.name, backend=self.backend,
                            state="results" if items else "empty", items=items,
                            dropped_cross_project=dropped)


def reconstruct_context(storage: MemoryStorage, role: str, task_id: str, scope: MemoryScope) -> dict:
    """Quality-first pack: authoritative refs are always included; no budget trimming."""
    if not memory_enabled(): return {"enabled": False, "records": []}
    live_layers = {"authoritative", "checkpoint", "procedural"}
    records = [r for r in storage.retrieve(scope, query=f"{role} {task_id}")
               + storage.retrieve(scope, exact_key=task_id) if r.layer in live_layers]
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
