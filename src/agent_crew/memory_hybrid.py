"""A2: FTS5 and local-vector ranking over the one ADR-001 SQLite store."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from array import array
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from .memory_runtime import (
    MemoryRecord, MemoryScope, MemoryStorage, SQLiteMemoryStorage,
    _canonical_scope_json, _logical_revision_key, _scope_from_json,
    _retrieval_rank,
    procedural_row_servable, procedural_servable_sql,
)

logger = logging.getLogger(__name__)
LATENCY_BUDGET_SECONDS = 0.300
LAZY_EMBED_LIMIT = 32
LAZY_EMBED_BATCH = 2
RRF_K = 60
HEAD_PRINCIPLE_BYTES = 3600
HEAD_STANDING_BYTES = 2000
MIDDLE_CANDIDATE_LIMIT = 96


def _body_sql(alias: str) -> str:
    return (f"CASE WHEN {alias}.layer='authoritative' THEN "
            f"COALESCE(json_extract({alias}.value,'$.text'),{alias}.value) || "
            f"COALESCE(' ' || json_extract({alias}.value,'$.gloss_en'),'') "
            f"ELSE COALESCE(json_extract({alias}.value,'$.text') || ' ', '') || "
            f"{alias}.value END")


def _vector_text(layer: str, raw_value: str, fts_body: str) -> tuple[str, str | None]:
    """Keep the legacy body for ordinary rows; split out a declared gloss."""
    value = json.loads(raw_value)
    gloss = value.get("gloss_en") if isinstance(value, dict) else None
    gloss = gloss.strip() if isinstance(gloss, str) else None
    if not gloss:
        return fts_body, None
    primary = value.get("text") if isinstance(value.get("text"), str) else fts_body
    return primary, gloss


def _embed_kind(embedder: Callable, text: str, kind: str):
    method = getattr(embedder, "embed_kind", None)
    return method(text, kind) if method is not None else embedder(text)


def _content_sha(text: str, embedder: Callable | None, kind: str) -> str:
    prefix = str(getattr(embedder, f"{kind}_prefix", ""))
    return hashlib.sha256((prefix + text).encode()).hexdigest()


_FTS_AI_SQL = ("CREATE TRIGGER IF NOT EXISTS adr001_fts_ai AFTER INSERT ON "
               "adr001_memory BEGIN INSERT INTO adr001_fts(rowid,layer,key,body) "
               f"VALUES(new.rowid,new.layer,new.key,{_body_sql('new')}); END;")
_FTS_AU_SQL = ("CREATE TRIGGER IF NOT EXISTS adr001_fts_au AFTER UPDATE OF value "
               "ON adr001_memory BEGIN DELETE FROM adr001_fts WHERE rowid=old.rowid; "
               "INSERT INTO adr001_fts(rowid,layer,key,body) "
               f"VALUES(new.rowid,new.layer,new.key,{_body_sql('new')}); "
               "DELETE FROM adr001_vec WHERE rowid=new.rowid; "
               "DELETE FROM adr001_vec_gloss WHERE rowid=new.rowid; END;")
_FTS_DDL = "CREATE VIRTUAL TABLE adr001_fts USING fts5(layer,key,body, tokenize='trigram')"


def _trigger_definition_changed(db: sqlite3.Connection, name: str, expected: str) -> bool:
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                     (name,)).fetchone()
    normalized = lambda sql: " ".join(sql.strip().rstrip(";").replace(
        "IF NOT EXISTS ", "").split()).casefold()
    return row is None or normalized(row[0]) != normalized(expected)


def ensure_index_schema(db: sqlite3.Connection) -> None:
    """Install additive indexes and triggers, then backfill old rows idempotently."""
    db.create_function("adr001_logical_key", 1, _logical_revision_key,
                       deterministic=True)
    # Only HybridMemoryStorage calls this initializer. The default store keeps
    # its six-column schema until its producers have moved to named inserts.
    columns = {row[1] for row in db.execute("PRAGMA table_info(adr001_memory)")}
    if "superseded_by" not in columns:
        db.execute("ALTER TABLE adr001_memory ADD COLUMN superseded_by TEXT NULL")
    if "invalidated_at" not in columns:
        db.execute("ALTER TABLE adr001_memory ADD COLUMN invalidated_at REAL NULL")
    # Translate old JSON tombstones before forbidding new ones. A legacy
    # superseded flag has no pointer; retain its exclusion as invalidation.
    invalidated = db.execute("""UPDATE adr001_memory SET invalidated_at =
        CASE WHEN json_type(value,'$.invalidated_at') IN ('integer','real')
             THEN CAST(json_extract(value,'$.invalidated_at') AS REAL)
             WHEN json_type(value,'$.superseded_at') IN ('integer','real')
             THEN CAST(json_extract(value,'$.superseded_at') AS REAL)
             ELSE created END
        WHERE invalidated_at IS NULL AND (
          json_type(value,'$.invalidated_at') IS NOT NULL OR
          json_type(value,'$.superseded_at') IS NOT NULL OR
          json_type(value,'$.superseded')='true')""").rowcount
    legacy_links = db.execute("""UPDATE adr001_memory
        SET superseded_by=json_extract(value,'$.superseded')
        WHERE superseded_by IS NULL AND json_type(value,'$.superseded')='text'
          AND json_extract(value,'$.superseded')<>''""").rowcount
    # Materialize the sparse superseding rows once. The old correlated lookup
    # extracted JSON from every row for every candidate at each startup.
    db.execute("""CREATE TEMP TABLE adr001_supersedes_candidates AS
        SELECT layer,scope,json_extract(value,'$.supersedes') AS supersedes,
               key,created,rowid AS source_rowid FROM adr001_memory
        WHERE json_extract(value,'$.supersedes') IS NOT NULL""")
    try:
        db.execute("""CREATE INDEX adr001_supersedes_candidates_lookup
            ON adr001_supersedes_candidates
            (layer,scope,supersedes,created DESC,source_rowid DESC)""")
        linked = db.execute("""UPDATE adr001_memory AS old SET superseded_by = (
            SELECT newer.key FROM adr001_supersedes_candidates AS newer
            WHERE newer.layer=old.layer AND newer.scope=old.scope
              AND newer.supersedes=adr001_logical_key(old.key)
            ORDER BY newer.created DESC,newer.source_rowid DESC LIMIT 1)
            WHERE old.superseded_by IS NULL AND EXISTS (
              SELECT 1 FROM adr001_supersedes_candidates AS newer
              WHERE newer.layer=old.layer AND newer.scope=old.scope
                AND newer.supersedes=adr001_logical_key(old.key))""").rowcount
    finally:
        db.execute("DROP TABLE adr001_supersedes_candidates")
    logger.info("memory hybrid legacy migration invalidated=%d superseded=%d",
                invalidated, legacy_links + linked)
    fts_row = db.execute("SELECT sql FROM sqlite_master WHERE name='adr001_fts'").fetchone()
    fresh = fts_row is None
    fts_changed = not fresh and "tokenize='trigram'" not in fts_row[0].lower()
    triggers_changed = any(_trigger_definition_changed(db, name, ddl) for name, ddl in (
        ("adr001_fts_ai", _FTS_AI_SQL), ("adr001_fts_au", _FTS_AU_SQL)))
    if fresh:
        db.execute(_FTS_DDL)
    db.execute("CREATE TABLE IF NOT EXISTS adr001_vec "
               "(rowid INTEGER PRIMARY KEY, model_id TEXT, content_sha TEXT, vec BLOB)")
    db.execute("CREATE TABLE IF NOT EXISTS adr001_vec_gloss "
               "(rowid INTEGER PRIMARY KEY, model_id TEXT, content_sha TEXT, vec BLOB)")
    db.executescript("""
      CREATE TRIGGER IF NOT EXISTS adr001_no_legacy_flags_bi
        BEFORE INSERT ON adr001_memory WHEN
          json_type(new.value,'$.superseded_at') IS NOT NULL OR
          json_type(new.value,'$.superseded') IS NOT NULL OR
          json_type(new.value,'$.invalidated_at') IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'legacy memory flags forbidden'); END;
      CREATE TRIGGER IF NOT EXISTS adr001_no_legacy_flags_bu
        BEFORE UPDATE OF value ON adr001_memory WHEN
          json_type(new.value,'$.superseded_at') IS NOT NULL OR
          json_type(new.value,'$.superseded') IS NOT NULL OR
          json_type(new.value,'$.invalidated_at') IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'legacy memory flags forbidden'); END;
      CREATE TRIGGER IF NOT EXISTS adr001_fts_ad AFTER DELETE ON adr001_memory BEGIN
        DELETE FROM adr001_fts WHERE rowid=old.rowid;
        DELETE FROM adr001_vec WHERE rowid=old.rowid;
        DELETE FROM adr001_vec_gloss WHERE rowid=old.rowid;
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_fts_effectiveness_au
        AFTER UPDATE OF superseded_by,invalidated_at ON adr001_memory BEGIN
        DELETE FROM adr001_vec WHERE rowid=new.rowid;
        DELETE FROM adr001_vec_gloss WHERE rowid=new.rowid;
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_vec_gloss_ad
        AFTER DELETE ON adr001_memory BEGIN
        DELETE FROM adr001_vec_gloss WHERE rowid=old.rowid;
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_vec_gloss_effectiveness_au
        AFTER UPDATE OF superseded_by,invalidated_at ON adr001_memory BEGIN
        DELETE FROM adr001_vec_gloss WHERE rowid=new.rowid;
      END;
    """)
    if fts_changed:
        # The script above ends the legacy-column transaction. Replacing the
        # table and its triggers in one write transaction keeps concurrent
        # writers from observing an unindexed row.
        db.execute("BEGIN IMMEDIATE")
        for name in ("adr001_fts_ai", "adr001_fts_au", "adr001_fts_ad"):
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        db.execute("DROP TABLE adr001_fts")
        db.execute(_FTS_DDL)
        db.execute("""CREATE TRIGGER adr001_fts_ad AFTER DELETE ON adr001_memory BEGIN
            DELETE FROM adr001_fts WHERE rowid=old.rowid;
            DELETE FROM adr001_vec WHERE rowid=old.rowid;
        END;""")
    count = db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0]
    indexed = db.execute("SELECT count(*) FROM adr001_fts").fetchone()[0]
    if fresh or fts_changed or triggers_changed or count != indexed:
        db.execute("DELETE FROM adr001_fts")
        db.execute("INSERT INTO adr001_fts(rowid,layer,key,body) "
                   "SELECT m.rowid,m.layer,m.key," + _body_sql("m") + " FROM adr001_memory m")
    if fts_changed or triggers_changed:
        # executescript commits before it runs. Keep the old triggers until
        # the rebuild starts, then swap them in that same transaction. A
        # rollback leaves both the old body and old trigger definition, so
        # the next initializer retries the rebuild.
        db.execute("DROP TRIGGER IF EXISTS adr001_fts_ai")
        db.execute("DROP TRIGGER IF EXISTS adr001_fts_au")
        db.execute(_FTS_AI_SQL)
        db.execute(_FTS_AU_SQL)


def _effective_sql(alias: str = "m") -> str:
    # Every hybrid reader (head, candidates, render-time rejoin) goes through
    # this clause as a coarse prefilter, then `_servable` decides (#716).
    return (f"{alias}.superseded_by IS NULL "
            f"AND {alias}.invalidated_at IS NULL "
            f"AND {procedural_servable_sql(alias)}")


def _scope_sql(scope: MemoryScope) -> tuple[str, list]:
    """A fleet ancestor and a project's fleet-less rows both apply (R27)."""
    project = ("(COALESCE(json_extract(m.scope,'$.project'),'')=? OR "
               "(COALESCE(json_extract(m.scope,'$.project'),'')='' AND "
               "COALESCE(json_extract(m.scope,'$.fleet'),'')<>''))")
    clauses = [project,
               "(COALESCE(json_extract(m.scope,'$.fleet'),'')='' OR "
               "json_extract(m.scope,'$.fleet')=?)"]
    params = [scope.project, scope.fleet]
    for name in ("worktree", "issue", "task_id", "context_generation", "provider_session"):
        value = getattr(scope, name)
        clauses.append(f"(json_extract(m.scope,'$.{name}') IS NULL OR "
                       f"json_extract(m.scope,'$.{name}')='' OR "
                       f"json_extract(m.scope,'$.{name}')=?)")
        params.append(value)
    return " AND ".join(clauses), params


def _row_record(row) -> dict:
    rowid, layer, key, value, scope, version, created = row
    body = json.loads(value)
    return {"rowid": rowid, "layer": layer, "key": _logical_revision_key(key), "value": body,
            "scope": asdict(_scope_from_json(scope)), "version": version,
            "created": created}


def _servable(rows: list[dict]) -> list[dict]:
    """Python authority over the coarse SQL lifecycle prefilter (#716)."""
    return [row for row in rows
            if procedural_row_servable(row["layer"], row["value"], key=row["key"])]


def _bytes(record: dict) -> int:
    return len(json.dumps(record, sort_keys=True, ensure_ascii=False).encode())


class OnnxMiniLMEmbedder:
    """Optional CPU embedder configured by model and tokenizer paths."""

    def __init__(self, model_path: str, tokenizer_path: str, model_id: str,
                 *, query_prefix: str = "", passage_prefix: str = ""):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_id = model_id
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(tokenizer_path)

    def __call__(self, text: str):
        return self.embed_kind(text, "passage")

    def embed_kind(self, text: str, kind: str):
        import numpy as np

        encoded = self.tokenizer.encode(getattr(self, f"{kind}_prefix") + text)
        ids = np.asarray([encoded.ids[:256]], dtype=np.int64)
        mask = np.asarray([getattr(encoded, "attention_mask", [1] * len(encoded.ids))[:256]],
                          dtype=np.int64)
        segments = np.zeros_like(ids)
        feeds = {}
        for input_meta in self.session.get_inputs():
            feeds[input_meta.name] = (mask if "mask" in input_meta.name else
                                      segments if "token_type" in input_meta.name else ids)
        tokens = self.session.run(None, feeds)[0][0]
        pooled = (tokens * mask[0, :, None]).sum(axis=0) / max(1, int(mask.sum()))
        norm = np.linalg.norm(pooled)
        return (pooled / norm if norm else pooled).astype(np.float32)


def configured_embedder():
    model = os.getenv("AGENT_CREW_MEMORY_ONNX_PATH", "").strip()
    tokenizer = os.getenv("AGENT_CREW_MEMORY_TOKENIZER_PATH", "").strip()
    if not model or not tokenizer:
        return None
    try:
        return OnnxMiniLMEmbedder(
            model, tokenizer, os.getenv("AGENT_CREW_MEMORY_MODEL_ID", "all-MiniLM-L6-v2"),
            query_prefix=os.getenv("AGENT_CREW_MEMORY_QUERY_PREFIX", ""),
            passage_prefix=os.getenv("AGENT_CREW_MEMORY_PASSAGE_PREFIX", ""))
    except Exception as exc:
        logger.warning("memory local embedder unavailable: %s: %s", type(exc).__name__, exc)
        return None


class EndpointEmbedder:
    """Embed through an explicitly configured serving endpoint."""

    def __init__(self, endpoint: str, model_id: str, timeout_seconds: float = 30.0,
                 *, query_prefix: str = "", passage_prefix: str = ""):
        self.endpoint = endpoint
        self.model_id = model_id
        self.timeout_seconds = timeout_seconds
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix

    def __call__(self, text: str):
        return self.embed_kind(text, "passage")

    def embed_kind(self, text: str, kind: str):
        payload = json.dumps({"model": self.model_id,
                              "input": getattr(self, f"{kind}_prefix") + text}).encode()
        request = urllib.request.Request(self.endpoint, data=payload,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.load(response)
        vectors = result.get("embeddings")
        return vectors[0] if vectors else result["embedding"]


def configured_write_embedder():
    endpoint = os.getenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT", "").strip()
    model_id = os.getenv("AGENT_CREW_MEMORY_MODEL_ID", "").strip()
    if not endpoint or not model_id:
        return None
    if not endpoint.startswith(("http://", "https://")):
        logger.warning("memory embed endpoint must be an HTTP URL")
        return None
    return EndpointEmbedder(endpoint, model_id,
                            query_prefix=os.getenv("AGENT_CREW_MEMORY_QUERY_PREFIX", ""),
                            passage_prefix=os.getenv("AGENT_CREW_MEMORY_PASSAGE_PREFIX", ""))


class HybridMemoryStorage(SQLiteMemoryStorage, MemoryStorage):
    """Scope in SQLite first; rank only those rowids with FTS5 and cosine."""

    def __init__(self, path: str, *, embedder: Optional[Callable] = None):
        super().__init__(path, busy_timeout_seconds=30.0)
        self.backfill()
        self.write_embedder = embedder if embedder is not None else configured_write_embedder()
        # The endpoint can wait 30 seconds; only local embedders may run in
        # the 300 ms retrieval path. Explicit test embedders serve both paths.
        self.embedder = embedder if embedder is not None else configured_embedder()
        self.model_id = str(getattr(self.write_embedder or self.embedder,
                                    "model_id", ""))
        self.retrieval_model_id = str(getattr(self.embedder, "model_id", ""))
        self.last_backfill_stats = {"failed": 0, "remaining": 0}
        self.fallback_count = 0
        self._fallback_lock = threading.Lock()
        self.last_retrieval_mode = "lexical_only"

    def _record_fallback(self, marker: Optional[threading.Event] = None) -> None:
        with self._fallback_lock:
            if marker is not None and marker.is_set():
                return
            if marker is not None:
                marker.set()
            self.fallback_count += 1

    def backfill(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=30.0)) as db:
            ensure_index_schema(db)
            db.commit()
        self._revision_columns = True

    def put(self, record: MemoryRecord) -> None:
        super().put(record)
        if self.write_embedder is not None:
            self._embed_written_record(record)

    def put_many_shadow(self, records: list[MemoryRecord], **kwargs) -> None:
        super().put_many_shadow(records, **kwargs)
        if self.write_embedder is not None:
            for record in records:
                if not self._embed_written_record(record):
                    break

    def _embed_written_record(self, record: MemoryRecord) -> bool:
        try:
            with closing(sqlite3.connect(self.path, timeout=30.0)) as db:
                row = db.execute("SELECT m.rowid,m.layer,m.value,f.body "
                                 "FROM adr001_memory m "
                                 "JOIN adr001_fts f ON f.rowid=m.rowid "
                                 "WHERE "
                                 "m.layer=? AND m.key=? AND m.scope=? AND " + _effective_sql(),
                                 (record.layer, record.key,
                                  _canonical_scope_json(record.scope))).fetchone()
            if row:
                rowid, layer, raw_value, body = row
                primary, gloss = _vector_text(layer, raw_value, body)
                return self._embed_row(rowid, primary, gloss)
            return True
        except Exception as exc:
            logger.warning("memory write embed failed key=%s: %s", record.key, exc)
            return False

    def _embed_row(self, rowid: int, body: str, gloss: str | None = None) -> bool:
        try:
            targets = [("adr001_vec", body)]
            if gloss:
                targets.append(("adr001_vec_gloss", gloss))
            with closing(sqlite3.connect(self.path, timeout=30.0)) as db:
                existing = {table: db.execute(
                    f"SELECT model_id,content_sha,vec FROM {table} WHERE rowid=?", (rowid,)).fetchone()
                    for table, _ in targets}
            stale = [(table, text) for table, text in targets
                     if not existing[table] or existing[table][0] != self.model_id
                     or existing[table][1] != _content_sha(text, self.write_embedder, "passage")
                     or existing[table][2] is None]
            if not stale:
                return True
            computed = []
            for table, text in stale:
                vector = array("f", _embed_kind(self.write_embedder, text, "passage"))
                if not vector or not all(math.isfinite(value) for value in vector):
                    raise ValueError("empty or non-finite embedding")
                computed.append((table, text, vector))
            with closing(sqlite3.connect(self.path, timeout=30.0)) as db:
                # Another writer may have changed or retired this row while the
                # endpoint was computing its vector.
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT m.layer,m.value,f.body FROM adr001_memory m "
                                     "JOIN adr001_fts f ON f.rowid=m.rowid "
                                     "WHERE m.rowid=? AND " + _effective_sql(), (rowid,)).fetchone()
                if not current or _vector_text(*current) != (body, gloss):
                    return False
                for table, text, vector in computed:
                    db.execute(f"INSERT INTO {table}(rowid,model_id,content_sha,vec) "
                               "VALUES(?,?,?,?) ON CONFLICT(rowid) DO UPDATE SET "
                               "model_id=excluded.model_id,content_sha=excluded.content_sha,vec=excluded.vec",
                               (rowid, self.model_id,
                                _content_sha(text, self.write_embedder, "passage"), vector.tobytes()))
                db.commit()
            return True
        except Exception as exc:
            logger.warning("memory embed failed rowid=%s: %s", rowid, exc)
            return False

    def backfill_vectors(self, *, limit: Optional[int] = None, batch_size: int = 128) -> int:
        """Fill effective stale vectors; committed rows survive interrupted runs."""
        if self.write_embedder is None:
            return 0
        if batch_size < 1 or (limit is not None and limit < 0):
            raise ValueError("batch_size must be positive and limit nonnegative")
        cursor = 0
        written = 0
        failed = consecutive_failures = 0
        while limit is None or written < limit:
            with closing(sqlite3.connect(self.path, timeout=30.0)) as db:
                rows = db.execute("SELECT m.rowid,m.layer,m.value,f.body "
                                  "FROM adr001_memory m JOIN adr001_fts f ON f.rowid=m.rowid "
                                  "WHERE m.rowid>? AND " + _effective_sql() +
                                  " ORDER BY m.rowid LIMIT ?", (cursor, batch_size)).fetchall()
            if not rows:
                break
            for rowid, layer, raw_value, fts_body in rows:
                cursor = rowid
                body, gloss = _vector_text(layer, raw_value, fts_body)
                if self._row_vectors_current(rowid, body, gloss):
                    continue
                if self._embed_row(rowid, body, gloss):
                    written += 1
                    consecutive_failures = 0
                else:
                    failed += 1
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        break
                if limit is not None and written >= limit:
                    break
            if consecutive_failures >= 3:
                break
        self.last_backfill_stats = {
            "failed": failed,
            "remaining": self.remaining_vector_count(),
        }
        return written

    def remaining_vector_count(self) -> int:
        """Count missing or stale primary and gloss vectors for effective rows."""
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT m.layer,m.value,f.body,"
                              "v.model_id,v.content_sha,v.vec,"
                              "g.model_id,g.content_sha,g.vec "
                              "FROM adr001_memory m JOIN adr001_fts f ON f.rowid=m.rowid "
                              "LEFT JOIN adr001_vec v ON v.rowid=m.rowid "
                              "LEFT JOIN adr001_vec_gloss g ON g.rowid=m.rowid WHERE "
                              + _effective_sql()).fetchall()
        count = 0
        for layer, value, fts_body, model, digest, vector, gloss_model, gloss_digest, gloss_vector in rows:
            body, gloss = _vector_text(layer, value, fts_body)
            count += int(vector is None or model != self.model_id or
                         digest != _content_sha(body, self.write_embedder, "passage"))
            if gloss is not None:
                count += int(gloss_vector is None or gloss_model != self.model_id or
                             gloss_digest != _content_sha(gloss, self.write_embedder, "passage"))
        return count

    def _row_vector_missing_count(self, rowid: int, body: str, gloss: str | None) -> int:
        with closing(sqlite3.connect(self.path)) as db:
            missing = 0
            for table, text in (("adr001_vec", body), ("adr001_vec_gloss", gloss)):
                if text is None:
                    continue
                row = db.execute(f"SELECT model_id,content_sha,vec FROM {table} WHERE rowid=?",
                                 (rowid,)).fetchone()
                if not row or row[0] != self.model_id or row[1] != _content_sha(
                        text, self.write_embedder, "passage") or row[2] is None:
                    missing += 1
            return missing

    def _row_vectors_current(self, rowid: int, body: str, gloss: str | None) -> bool:
        return self._row_vector_missing_count(rowid, body, gloss) == 0

    def missing_vector_count(self) -> int:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT count(*) FROM adr001_memory m LEFT JOIN adr001_vec v "
                              "ON v.rowid=m.rowid WHERE v.rowid IS NULL OR v.vec IS NULL").fetchone()[0]

    def effective_rows(self, rows: list[dict], scope: MemoryScope) -> list[dict]:
        """Rejoin selected rowids immediately before rendering a response."""
        if not rows:
            return []
        clause, params = _scope_sql(scope)
        ids = [row["rowid"] for row in rows]
        with closing(sqlite3.connect(self.path, timeout=.05)) as db:
            effective = {row[0] for row in db.execute(
                "SELECT m.rowid FROM adr001_memory m WHERE m.rowid IN (" +
                ",".join("?" for _ in ids) + ") AND " + clause +
                " AND " + _effective_sql(), [*ids, *params])}
        return _servable([row for row in rows if row["rowid"] in effective])

    def _candidates(self, db: sqlite3.Connection, scope: MemoryScope,
                    query: str = "") -> list[dict]:
        clause, params = _scope_sql(scope)
        prefix = ("SELECT m.rowid,m.layer,m.key,m.value,m.scope,m.version,m.created "
                  "FROM adr001_memory m ")
        terms = re.findall(r"[\w]+", query)[:12]
        rows = []
        if terms:
            searchable = [term for term in terms if len(term) >= 3]
            short_terms = [term for term in terms if len(term) < 3]
            if short_terms:
                # Trigram MATCH cannot see short terms. Include key and layer,
                # as the former unicode61 index did, and fold English case.
                haystack = "lower(adr001_fts.layer || ' ' || adr001_fts.key || ' ' || adr001_fts.body)"
                short_match = " OR ".join(
                    f"instr({haystack}, lower(?)) > 0" for _ in short_terms)
                rows = db.execute(
                    prefix + "JOIN adr001_fts ON adr001_fts.rowid=m.rowid WHERE " +
                    clause + " AND " + _effective_sql() + " AND (" + short_match + ") "
                    "ORDER BY m.created DESC,m.rowid DESC LIMIT ?",
                    [*params, *short_terms, MIDDLE_CANDIDATE_LIMIT],
                ).fetchall()
            if searchable:
                expression = " OR ".join('"' + term + '"' for term in searchable)
                long_rows = db.execute(
                    prefix + "JOIN adr001_fts ON adr001_fts.rowid=m.rowid WHERE " +
                    clause + " AND " + _effective_sql() +
                    " AND adr001_fts MATCH ? ORDER BY bm25(adr001_fts) "
                    "LIMIT ?", [*params, expression, MIDDLE_CANDIDATE_LIMIT],
                ).fetchall()
                if rows:
                    # Both lexical lanes get room before recent-row padding.
                    short_rows, rows, seen = rows, [], set()
                    for index in range(MIDDLE_CANDIDATE_LIMIT):
                        for lane in (short_rows, long_rows):
                            if index < len(lane) and lane[index][0] not in seen:
                                rows.append(lane[index]); seen.add(lane[index][0])
                                if len(rows) == MIDDLE_CANDIDATE_LIMIT:
                                    break
                        if len(rows) == MIDDLE_CANDIDATE_LIMIT:
                            break
                else:
                    rows = long_rows
        if len(rows) < MIDDLE_CANDIDATE_LIMIT:
            seen = {row[0] for row in rows}
            recent = db.execute(prefix + "WHERE " + clause + " AND " + _effective_sql() +
                                " ORDER BY m.created DESC,m.rowid DESC LIMIT ?",
                                [*params, MIDDLE_CANDIDATE_LIMIT]).fetchall()
            rows.extend(row for row in recent if row[0] not in seen)
            rows = rows[:MIDDLE_CANDIDATE_LIMIT]
        result = _servable([_row_record(row) for row in rows])
        decisions = {hashlib.sha256(json.dumps(row["value"], sort_keys=True).encode()).hexdigest()
                     for row in result if row["layer"] == "decision"}
        return [row for row in result if row["layer"] != "episodic" or
                hashlib.sha256(json.dumps(row["value"], sort_keys=True).encode()).hexdigest()
                not in decisions]

    def render_head(self, project: str, fleet: str = "") -> dict:
        """Deterministic SQL view used by both dispatch and the HTTP hook."""
        clause, params = _scope_sql(MemoryScope(project=project, fleet=fleet))
        prefix = ("SELECT m.rowid,m.layer,m.key,m.value,m.scope,m.version,m.created "
                  "FROM adr001_memory m WHERE " + clause + " AND " + _effective_sql())
        with closing(sqlite3.connect(self.path)) as db:
            principles = _servable([_row_record(row) for row in db.execute(
                prefix + " AND m.layer='procedural' AND "
                "(m.key LIKE 'owner_principle:%' OR json_extract(m.value,'$.kind')='owner_principle') "
                "ORDER BY m.key", params)])
            standing = [_row_record(row) for row in db.execute(
                prefix + " AND json_extract(m.value,'$.kind')='standing_decision' "
                "ORDER BY json_extract(m.value,'$.verb'),json_extract(m.value,'$.subject'),"
                "json_extract(m.value,'$.source_message_id')", params)]
        head, overflow, seen = [], [], set()
        for group, cap, force_overflow in ((principles, HEAD_PRINCIPLE_BYTES, False),
                                           (standing, HEAD_STANDING_BYTES, True)):
            used = 0
            for record in group:
                if record["rowid"] in seen:
                    continue
                seen.add(record["rowid"])
                size = _bytes(record)
                if used + size <= cap:
                    head.append(record); used += size
                elif force_overflow:
                    overflow.append(record)
        rendered = json.dumps(head, sort_keys=True, ensure_ascii=False).encode()
        return {"records": head, "standing_overflow": overflow,
                "standing_trimmed": len([r for r in overflow if r in standing]),
                "head_hash": hashlib.sha256(rendered).hexdigest()}

    def _lazy_embed(self, candidates: list[dict], deadline: float) -> int:
        if self.embedder is None:
            return len(candidates)

        by_id = {r["rowid"]: r for r in candidates}
        with closing(sqlite3.connect(self.path, timeout=.1)) as db:
            rows = db.execute("SELECT m.rowid,m.layer,m.value,f.body,v.model_id,v.content_sha "
                              "FROM adr001_memory m "
                              "JOIN adr001_fts f ON f.rowid=m.rowid LEFT JOIN adr001_vec v "
                              "ON v.rowid=m.rowid WHERE m.rowid IN (" +
                              ",".join("?" for _ in by_id) + ") ORDER BY m.created DESC,m.rowid DESC",
                              list(by_id)).fetchall() if by_id else []
            missing = [(rowid, *_vector_text(layer, value, body))
                       for rowid, layer, value, body, model, digest in rows
                       if model != self.retrieval_model_id or
                       digest != _content_sha(_vector_text(layer, value, body)[0],
                                              self.embedder, "passage")]
        # Compute outside a write transaction; other producers share this DB.
        attempted = 0
        for rowid, body, gloss in missing[:min(LAZY_EMBED_LIMIT, LAZY_EMBED_BATCH)]:
            if time.perf_counter() >= deadline:
                break
            attempted += 1
            try:
                vector = array("f", _embed_kind(self.embedder, body, "passage"))
                if not vector or not all(math.isfinite(v) for v in vector):
                    continue
                with closing(sqlite3.connect(self.path, timeout=.1)) as db:
                    db.execute("INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) "
                               "VALUES(?,?,?,?) ON CONFLICT(rowid) DO UPDATE SET "
                               "model_id=excluded.model_id,content_sha=excluded.content_sha,vec=excluded.vec",
                               (rowid, self.retrieval_model_id,
                                _content_sha(body, self.embedder, "passage"), vector.tobytes()))
                    db.commit()
            except Exception as exc:
                logger.warning("memory embed failed rowid=%s: %s", rowid, exc)
        return max(0, len(missing) - attempted)

    def _rank_middle(self, candidates: list[dict], query: str) -> tuple[list[dict], bool]:
        import re

        ids = {r["rowid"] for r in candidates}
        if not ids:
            return [], False
        terms = re.findall(r"[\w]+", query)[:12]
        bm25 = {}
        short_rank = {}
        with closing(sqlite3.connect(self.path)) as db:
            searchable = [term for term in terms if len(term) >= 3]
            if searchable:
                expression = " OR ".join('"' + term.replace('"', '') + '"' for term in searchable)
                for rowid, score in db.execute(
                        "SELECT rowid,bm25(adr001_fts,0.2,10,1) FROM adr001_fts "
                        "WHERE adr001_fts MATCH ? ORDER BY 2", (expression,)):
                    if rowid in ids:
                        bm25[rowid] = len(bm25) + 1
            short_terms = [term for term in terms if len(term) < 3]
            if short_terms:
                haystack = "lower(layer || ' ' || key || ' ' || body)"
                short_match = " OR ".join(
                    f"instr({haystack}, lower(?)) > 0" for _ in short_terms)
                short_ids = {row[0] for row in db.execute(
                    "SELECT rowid FROM adr001_fts WHERE rowid IN (" +
                    ",".join("?" for _ in ids) + ") AND (" + short_match + ")",
                    [*ids, *short_terms])}
                short_rank = {row["rowid"]: rank for rank, row in enumerate(
                    (row for row in candidates if row["rowid"] in short_ids), 1)}
            vectors = db.execute("SELECT rowid,model_id,content_sha,vec FROM adr001_vec WHERE "
                                 "rowid IN (" + ",".join("?" for _ in ids) + ")", list(ids)).fetchall()
            gloss_vectors = db.execute("SELECT rowid,model_id,content_sha,vec "
                                       "FROM adr001_vec_gloss WHERE rowid IN (" +
                                       ",".join("?" for _ in ids) + ")", list(ids)).fetchall()
            bodies = {rowid: _vector_text(layer, value, body) for rowid, layer, value, body
                      in db.execute("SELECT m.rowid,m.layer,m.value,f.body FROM adr001_memory m "
                                    "JOIN adr001_fts f ON f.rowid=m.rowid WHERE m.rowid IN (" +
                                    ",".join("?" for _ in ids) + ")", list(ids))}
        semantic = []
        if self.embedder and (vectors or gloss_vectors):
            q = array("f", _embed_kind(self.embedder, query, "query"))
            qnorm = math.sqrt(sum(v * v for v in q))
            scores = {}
            for kind, rows in (("primary", vectors), ("gloss", gloss_vectors)):
                for rowid, model, digest, blob in rows:
                    texts = bodies.get(rowid)
                    if texts is None:
                        continue
                    body = texts[0] if kind == "primary" else texts[1]
                    if body is None or model != self.retrieval_model_id or digest != _content_sha(
                            body, self.embedder, "passage"):
                        continue
                    vector = array("f")
                    vector.frombytes(blob)
                    if len(vector) != len(q):
                        continue
                    denom = qnorm * math.sqrt(sum(v * v for v in vector))
                    if denom:
                        score = sum(a * b for a, b in zip(q, vector)) / denom
                        scores[rowid] = max(score, scores.get(rowid, -math.inf))
            semantic = list(scores.items())
        semantic.sort(key=lambda item: (-item[1], item[0]))
        vector_rank = {rowid: rank for rank, (rowid, _) in enumerate(semantic, 1)}
        lexical = {r["rowid"]: rank for rank, r in enumerate(candidates, 1)}
        candidates.sort(key=lambda row: (
            -(1 / (RRF_K + bm25[row["rowid"]]) if row["rowid"] in bm25 else 0)
            - (1 / (RRF_K + short_rank[row["rowid"]])
               if row["rowid"] in short_rank else 0)
            - (1 / (RRF_K + vector_rank[row["rowid"]]) if row["rowid"] in vector_rank else 0),
            lexical[row["rowid"]]))
        return candidates, bool(vector_rank)

    def retrieve_ranked(self, scope: MemoryScope, query: str, role: str,
                        k: int, byte_budget: int, *,
                        fallback_marker: Optional[threading.Event] = None,
                        response_state: Optional[dict] = None) -> dict:
        started = time.perf_counter()
        deadline = started + LATENCY_BUDGET_SECONDS
        head = self.render_head(scope.project, scope.fleet)
        if response_state is not None:
            response_state["head"] = head
        candidates = []
        pending = 0
        try:
            if time.perf_counter() >= deadline:
                raise TimeoutError("head exceeded retrieval budget")
            with closing(sqlite3.connect(self.path)) as db:
                candidates = self._candidates(db, scope, query)
            if time.perf_counter() >= deadline:
                raise TimeoutError("candidate lookup exceeded retrieval budget")
            head_ids = {r["rowid"] for r in head["records"]}
            middle = [r for r in candidates if r["rowid"] not in head_ids]
            pending = self._lazy_embed(middle, deadline)
            if response_state is not None:
                response_state["pending"] = pending
            if time.perf_counter() >= deadline:
                raise TimeoutError("embedding exceeded retrieval budget")
            middle, vector_used = self._rank_middle(middle, query)
            overflow_ids = {r["rowid"] for r in head["standing_overflow"]}
            middle = head["standing_overflow"] + [r for r in middle
                                                    if r["rowid"] not in overflow_ids]
            selected, used, trimmed = [], 0, 0
            emitted_keys = {r["key"] for r in head["records"]}
            for row in middle:
                if len(selected) >= max(0, k): break
                if row["key"] in emitted_keys:
                    continue
                size = _bytes(row)
                if used + size <= max(0, byte_budget):
                    selected.append(row); used += size; emitted_keys.add(row["key"])
                else:
                    trimmed += size
            # Refresh effectiveness at the point of rendering, after ranking.
            with closing(sqlite3.connect(self.path)) as db:
                clause, params = _scope_sql(scope)
                ids = [r["rowid"] for r in selected]
                fresh = {row[0] for row in db.execute(
                    "SELECT m.rowid FROM adr001_memory m WHERE m.rowid IN (" +
                    ",".join("?" for _ in ids) + ") AND " + clause +
                    " AND " + _effective_sql(), [*ids, *params],
                )} if ids else set()
            selected = _servable([r for r in selected if r["rowid"] in fresh])
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed > LATENCY_BUDGET_SECONDS * 1000:
                raise TimeoutError("retrieval exceeded 300 ms")
            mode = "hybrid" if vector_used else "lexical_only"
        except Exception as exc:
            logger.warning("memory retrieval fallback: %s: %s", type(exc).__name__, exc)
            self._record_fallback(fallback_marker)
            terms = set(query.lower().replace("-", " ").split())
            selected = [r for r in candidates
                        if r["rowid"] not in {h["rowid"] for h in head["records"]}]
            selected.sort(key=lambda r: _retrieval_rank(
                MemoryRecord(r["layer"], r["key"], r["value"], MemoryScope(**r["scope"])), terms))
            bounded, used = [], 0
            for row in selected:
                if len(bounded) >= max(0, k):
                    break
                size = _bytes(row)
                if used + size <= max(0, byte_budget):
                    bounded.append(row)
                    used += size
            selected = bounded
            trimmed = 0
            mode = "fallback"
            elapsed = (time.perf_counter() - started) * 1000
        # Rejoin every rendered row after ranking, including head rows. A
        # second writer may have superseded one while this call was ranking.
        try:
            head["records"] = self.effective_rows(head["records"], scope)
            selected = self.effective_rows(selected, scope)
        except sqlite3.Error as exc:
            logger.warning("memory render-time effectiveness check failed: %s", exc)
            self._record_fallback(fallback_marker)
            head["records"] = []
            selected = []
            mode = "fallback"
        rendered_head = json.dumps(head["records"], sort_keys=True, ensure_ascii=False).encode()
        head["head_hash"] = hashlib.sha256(rendered_head).hexdigest()
        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > LATENCY_BUDGET_SECONDS * 1000 and mode != "fallback":
            self._record_fallback(fallback_marker)
            mode = "fallback"
            selected = []
        self.last_retrieval_mode = mode
        return {"head": head["records"], "middle": selected, "mode": mode,
                "pending_vectors": pending, "latency_ms": elapsed,
                "trimmed_bytes": trimmed, "standing_trimmed": head["standing_trimmed"],
                "model_id": self.model_id, "head_hash": head["head_hash"],
                "head_bytes": len(rendered_head), "superseded_served": 0}


def memory_storage_from_env(path: str, *, embedder=None) -> MemoryStorage:
    if os.getenv("AGENT_CREW_MEMORY_BACKEND", "").strip().lower() == "hybrid":
        return HybridMemoryStorage(path, embedder=embedder)
    return SQLiteMemoryStorage(path)


def backup_daily(path: str, *, keep: int = 7) -> Optional[str]:
    """One SQLite online backup per UTC day; retain the newest seven."""
    source = Path(path)
    if not source.is_file():
        return None
    directory = Path(os.getenv("AGENT_CREW_MEMORY_BACKUP_DIR", str(source.parent / "backups")))
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (source.name + "." + time.strftime("%Y%m%d", time.gmtime()) + ".backup")
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with closing(sqlite3.connect(path)) as original, closing(sqlite3.connect(temporary)) as copy:
            original.backup(copy)
        os.replace(temporary, destination)
    for old in sorted(directory.glob(source.name + ".*.backup"), reverse=True)[keep:]:
        old.unlink()
    return str(destination)
