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
from array import array
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from .memory_runtime import (
    MemoryRecord, MemoryScope, MemoryStorage, SQLiteMemoryStorage,
    _scope_from_json, _retrieval_rank,
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
            f"COALESCE(json_extract({alias}.value,'$.text'),{alias}.value) "
            f"ELSE {alias}.value END")


def ensure_index_schema(db: sqlite3.Connection) -> None:
    """Install additive indexes and triggers, then backfill old rows idempotently."""
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
    linked = db.execute("""UPDATE adr001_memory AS old SET superseded_by = (
        SELECT newer.key FROM adr001_memory AS newer
        WHERE newer.layer=old.layer AND newer.scope=old.scope
          AND json_extract(newer.value,'$.supersedes')=old.key
        ORDER BY newer.created DESC,newer.rowid DESC LIMIT 1)
        WHERE old.superseded_by IS NULL AND EXISTS (
          SELECT 1 FROM adr001_memory AS newer
          WHERE newer.layer=old.layer AND newer.scope=old.scope
            AND json_extract(newer.value,'$.supersedes')=old.key)""").rowcount
    logger.info("memory hybrid legacy migration invalidated=%d superseded=%d",
                invalidated, legacy_links + linked)
    fresh = db.execute("SELECT 1 FROM sqlite_master WHERE name='adr001_fts'").fetchone() is None
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS adr001_fts USING fts5(layer,key,body)")
    db.execute("CREATE TABLE IF NOT EXISTS adr001_vec "
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
      CREATE TRIGGER IF NOT EXISTS adr001_fts_ai AFTER INSERT ON adr001_memory BEGIN
        INSERT INTO adr001_fts(rowid,layer,key,body)
          VALUES(new.rowid,new.layer,new.key,
            CASE WHEN new.layer='authoritative' THEN
              COALESCE(json_extract(new.value,'$.text'),new.value)
            ELSE new.value END);
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_fts_au
        AFTER UPDATE OF value ON adr001_memory BEGIN
        DELETE FROM adr001_fts WHERE rowid=old.rowid;
        INSERT INTO adr001_fts(rowid,layer,key,body)
          VALUES(new.rowid,new.layer,new.key,
            CASE WHEN new.layer='authoritative' THEN
              COALESCE(json_extract(new.value,'$.text'),new.value)
            ELSE new.value END);
        DELETE FROM adr001_vec WHERE rowid=new.rowid;
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_fts_ad AFTER DELETE ON adr001_memory BEGIN
        DELETE FROM adr001_fts WHERE rowid=old.rowid;
        DELETE FROM adr001_vec WHERE rowid=old.rowid;
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_fts_effectiveness_au
        AFTER UPDATE OF superseded_by,invalidated_at ON adr001_memory BEGIN
        DELETE FROM adr001_vec WHERE rowid=new.rowid;
      END;
    """)
    count = db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0]
    indexed = db.execute("SELECT count(*) FROM adr001_fts").fetchone()[0]
    if fresh or count != indexed:
        db.execute("DELETE FROM adr001_fts")
        db.execute("INSERT INTO adr001_fts(rowid,layer,key,body) "
                   "SELECT m.rowid,m.layer,m.key," + _body_sql("m") + " FROM adr001_memory m")


def _effective_sql(alias: str = "m") -> str:
    return (f"{alias}.superseded_by IS NULL "
            f"AND {alias}.invalidated_at IS NULL")


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
    return {"rowid": rowid, "layer": layer, "key": key, "value": body,
            "scope": asdict(_scope_from_json(scope)), "version": version,
            "created": created}


def _bytes(record: dict) -> int:
    return len(json.dumps(record, sort_keys=True, ensure_ascii=False).encode())


class OnnxMiniLMEmbedder:
    """Optional CPU embedder configured by model and tokenizer paths."""

    def __init__(self, model_path: str, tokenizer_path: str, model_id: str):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_id = model_id
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(tokenizer_path)

    def __call__(self, text: str):
        import numpy as np

        encoded = self.tokenizer.encode(text)
        ids = np.asarray([encoded.ids[:256]], dtype=np.int64)
        mask = np.ones_like(ids)
        segments = np.zeros_like(ids)
        feeds = {}
        for input_meta in self.session.get_inputs():
            feeds[input_meta.name] = (mask if "mask" in input_meta.name else
                                      segments if "token_type" in input_meta.name else ids)
        tokens = self.session.run(None, feeds)[0][0]
        return tokens.mean(axis=0).astype(np.float32)


def configured_embedder():
    model = os.getenv("AGENT_CREW_MEMORY_ONNX_PATH", "").strip()
    tokenizer = os.getenv("AGENT_CREW_MEMORY_TOKENIZER_PATH", "").strip()
    if not model or not tokenizer:
        return None
    try:
        return OnnxMiniLMEmbedder(
            model, tokenizer, os.getenv("AGENT_CREW_MEMORY_MODEL_ID", "all-MiniLM-L6-v2"))
    except Exception as exc:
        logger.warning("memory local embedder unavailable: %s: %s", type(exc).__name__, exc)
        return None


class HybridMemoryStorage(SQLiteMemoryStorage, MemoryStorage):
    """Scope in SQLite first; rank only those rowids with FTS5 and cosine."""

    def __init__(self, path: str, *, embedder: Optional[Callable] = None):
        super().__init__(path)
        self.backfill()
        self.embedder = embedder if embedder is not None else configured_embedder()
        self.model_id = str(getattr(self.embedder, "model_id", "")) if self.embedder else ""
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
        with closing(sqlite3.connect(self.path)) as db:
            ensure_index_schema(db)
            db.commit()

    def missing_vector_count(self) -> int:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT count(*) FROM adr001_memory m LEFT JOIN adr001_vec v "
                              "ON v.rowid=m.rowid WHERE v.rowid IS NULL OR v.vec IS NULL").fetchone()[0]

    def _candidates(self, db: sqlite3.Connection, scope: MemoryScope,
                    query: str = "") -> list[dict]:
        clause, params = _scope_sql(scope)
        prefix = ("SELECT m.rowid,m.layer,m.key,m.value,m.scope,m.version,m.created "
                  "FROM adr001_memory m ")
        terms = re.findall(r"[\w]+", query)[:12]
        rows = []
        if terms:
            expression = " OR ".join('"' + term + '"' for term in terms)
            rows = db.execute(
                prefix + "JOIN adr001_fts ON adr001_fts.rowid=m.rowid WHERE " +
                clause + " AND " + _effective_sql() +
                " AND adr001_fts MATCH ? ORDER BY bm25(adr001_fts) "
                "LIMIT ?", [*params, expression, MIDDLE_CANDIDATE_LIMIT],
            ).fetchall()
        if len(rows) < MIDDLE_CANDIDATE_LIMIT:
            seen = {row[0] for row in rows}
            recent = db.execute(prefix + "WHERE " + clause + " AND " + _effective_sql() +
                                " ORDER BY m.created DESC,m.rowid DESC LIMIT ?",
                                [*params, MIDDLE_CANDIDATE_LIMIT]).fetchall()
            rows.extend(row for row in recent if row[0] not in seen)
            rows = rows[:MIDDLE_CANDIDATE_LIMIT]
        result = [_row_record(row) for row in rows]
        decisions = {hashlib.sha256(json.dumps(row["value"], sort_keys=True).encode()).hexdigest()
                     for row in result if row["layer"] == "decision"}
        return [row for row in result if row["layer"] != "episodic" or
                hashlib.sha256(json.dumps(row["value"], sort_keys=True).encode()).hexdigest()
                not in decisions]

    def render_head(self, project: str) -> dict:
        """Deterministic SQL view used by both dispatch and the HTTP hook."""
        clause, params = _scope_sql(MemoryScope(project=project))
        prefix = ("SELECT m.rowid,m.layer,m.key,m.value,m.scope,m.version,m.created "
                  "FROM adr001_memory m WHERE " + clause + " AND " + _effective_sql())
        with closing(sqlite3.connect(self.path)) as db:
            principles = [_row_record(row) for row in db.execute(
                prefix + " AND m.layer='procedural' AND "
                "(m.key LIKE 'owner_principle:%' OR json_extract(m.value,'$.kind')='owner_principle') "
                "ORDER BY m.key", params)]
            standing = [_row_record(row) for row in db.execute(
                prefix + " AND json_extract(m.value,'$.kind')='standing_decision' "
                "ORDER BY json_extract(m.value,'$.verb'),json_extract(m.value,'$.subject'),"
                "json_extract(m.value,'$.source_message_id')", params)]
        head, overflow, seen = [], [], set()
        for group, cap in ((principles, HEAD_PRINCIPLE_BYTES),
                           (standing, HEAD_STANDING_BYTES)):
            used = 0
            for record in group:
                if record["rowid"] in seen:
                    continue
                seen.add(record["rowid"])
                size = _bytes(record)
                if used + size <= cap:
                    head.append(record); used += size
                else:
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
            rows = db.execute("SELECT m.rowid,f.body,v.model_id,v.content_sha FROM adr001_memory m "
                              "JOIN adr001_fts f ON f.rowid=m.rowid LEFT JOIN adr001_vec v "
                              "ON v.rowid=m.rowid WHERE m.rowid IN (" +
                              ",".join("?" for _ in by_id) + ") ORDER BY m.created DESC,m.rowid DESC",
                              list(by_id)).fetchall() if by_id else []
            missing = [(rowid, body, hashlib.sha256(body.encode()).hexdigest())
                       for rowid, body, model, digest in rows
                       if model != self.model_id or digest != hashlib.sha256(body.encode()).hexdigest()]
        # Compute outside a write transaction; other producers share this DB.
        attempted = 0
        for rowid, body, digest in missing[:min(LAZY_EMBED_LIMIT, LAZY_EMBED_BATCH)]:
            if time.perf_counter() >= deadline:
                break
            attempted += 1
            try:
                vector = array("f", self.embedder(body))
                if not vector or not all(math.isfinite(v) for v in vector):
                    continue
                with closing(sqlite3.connect(self.path, timeout=.1)) as db:
                    db.execute("INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) "
                               "VALUES(?,?,?,?) ON CONFLICT(rowid) DO UPDATE SET "
                               "model_id=excluded.model_id,content_sha=excluded.content_sha,vec=excluded.vec",
                               (rowid, self.model_id, digest, vector.tobytes()))
                    db.commit()
            except Exception as exc:
                logger.warning("memory embed failed rowid=%s: %s", rowid, exc)
        return max(0, len(missing) - attempted)

    def _rank_middle(self, candidates: list[dict], query: str) -> tuple[list[dict], bool]:
        import re

        ids = {r["rowid"] for r in candidates}
        if not ids:
            return [], False
        terms = re.findall(r"[\w]+", query)
        bm25 = {}
        with closing(sqlite3.connect(self.path)) as db:
            if terms:
                expression = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
                for rowid, score in db.execute(
                        "SELECT rowid,bm25(adr001_fts,0.2,10,1) FROM adr001_fts "
                        "WHERE adr001_fts MATCH ? ORDER BY 2", (expression,)):
                    if rowid in ids:
                        bm25[rowid] = len(bm25) + 1
            vectors = db.execute("SELECT rowid,model_id,content_sha,vec FROM adr001_vec WHERE "
                                 "rowid IN (" + ",".join("?" for _ in ids) + ")", list(ids)).fetchall()
            bodies = {rowid: body for rowid, body in db.execute(
                "SELECT rowid,body FROM adr001_fts WHERE rowid IN (" +
                ",".join("?" for _ in ids) + ")", list(ids))}
        semantic = []
        if self.embedder and vectors:
            q = array("f", self.embedder(query))
            qnorm = math.sqrt(sum(v * v for v in q))
            for rowid, model, digest, blob in vectors:
                body = bodies.get(rowid, "")
                if model != self.model_id or digest != hashlib.sha256(body.encode()).hexdigest():
                    continue
                vector = array("f")
                vector.frombytes(blob)
                if len(vector) != len(q):
                    continue
                denom = qnorm * math.sqrt(sum(v * v for v in vector))
                if denom:
                    semantic.append((rowid, sum(a * b for a, b in zip(q, vector)) / denom))
        semantic.sort(key=lambda item: (-item[1], item[0]))
        vector_rank = {rowid: rank for rank, (rowid, _) in enumerate(semantic, 1)}
        lexical = {r["rowid"]: rank for rank, r in enumerate(candidates, 1)}
        candidates.sort(key=lambda row: (
            -(1 / (RRF_K + bm25[row["rowid"]]) if row["rowid"] in bm25 else 0)
            - (1 / (RRF_K + vector_rank[row["rowid"]]) if row["rowid"] in vector_rank else 0),
            lexical[row["rowid"]]))
        return candidates, bool(vector_rank)

    def retrieve_ranked(self, scope: MemoryScope, query: str, role: str,
                        k: int, byte_budget: int, *,
                        fallback_marker: Optional[threading.Event] = None,
                        response_state: Optional[dict] = None) -> dict:
        started = time.perf_counter()
        deadline = started + LATENCY_BUDGET_SECONDS
        head = self.render_head(scope.project)
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
            selected = [r for r in selected if r["rowid"] in fresh]
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
        self.last_retrieval_mode = mode
        return {"head": head["records"], "middle": selected, "mode": mode,
                "pending_vectors": pending, "latency_ms": elapsed,
                "trimmed_bytes": trimmed, "standing_trimmed": head["standing_trimmed"],
                "model_id": self.model_id, "head_hash": head["head_hash"]}


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
