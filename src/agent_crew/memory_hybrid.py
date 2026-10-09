"""A2: FTS5 and local-vector ranking over the one ADR-001 SQLite store."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
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
RRF_K = 60
HEAD_PRINCIPLE_BYTES = 3600
HEAD_STANDING_BYTES = 2000


def _body_sql(alias: str) -> str:
    return (f"CASE WHEN {alias}.layer='authoritative' THEN "
            f"COALESCE(json_extract({alias}.value,'$.text'),{alias}.value) "
            f"ELSE {alias}.value END")


def ensure_index_schema(db: sqlite3.Connection) -> None:
    """Install additive indexes and triggers, then backfill old rows idempotently."""
    names = {row[1] for row in db.execute("PRAGMA table_info(adr001_memory)")}
    for name in ("superseded_by", "invalidated_at"):
        if name not in names:
            db.execute(f"ALTER TABLE adr001_memory ADD COLUMN {name} TEXT")
    fresh = db.execute("SELECT 1 FROM sqlite_master WHERE name='adr001_fts'").fetchone() is None
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS adr001_fts USING fts5(layer,key,body)")
    db.execute("CREATE TABLE IF NOT EXISTS adr001_vec "
               "(rowid INTEGER PRIMARY KEY, model_id TEXT, content_sha TEXT, vec BLOB)")
    db.executescript("""
      CREATE TRIGGER IF NOT EXISTS adr001_fts_ai AFTER INSERT ON adr001_memory BEGIN
        INSERT INTO adr001_fts(rowid,layer,key,body)
          VALUES(new.rowid,new.layer,new.key,
            CASE WHEN new.layer='authoritative' THEN
              COALESCE(json_extract(new.value,'$.text'),new.value)
            ELSE new.value END);
      END;
      CREATE TRIGGER IF NOT EXISTS adr001_fts_au
        AFTER UPDATE OF value,superseded_by,invalidated_at ON adr001_memory BEGIN
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
    """)
    count = db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0]
    indexed = db.execute("SELECT count(*) FROM adr001_fts").fetchone()[0]
    if fresh or count != indexed:
        db.execute("DELETE FROM adr001_fts")
        db.execute("INSERT INTO adr001_fts(rowid,layer,key,body) "
                   "SELECT m.rowid,m.layer,m.key," + _body_sql("m") + " FROM adr001_memory m")


def _effective_sql(alias: str = "m") -> str:
    return (f"{alias}.superseded_by IS NULL AND {alias}.invalidated_at IS NULL "
            f"AND json_extract({alias}.value,'$.superseded_at') IS NULL "
            f"AND json_extract({alias}.value,'$.invalidated_at') IS NULL "
            f"AND COALESCE(json_extract({alias}.value,'$.superseded'),0)=0")


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
        self.last_retrieval_mode = "lexical_only"

    def backfill(self) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            ensure_index_schema(db)
            db.commit()

    def missing_vector_count(self) -> int:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT count(*) FROM adr001_memory m LEFT JOIN adr001_vec v "
                              "ON v.rowid=m.rowid WHERE v.rowid IS NULL OR v.vec IS NULL").fetchone()[0]

    def _candidates(self, db: sqlite3.Connection, scope: MemoryScope) -> list[dict]:
        clause, params = _scope_sql(scope)
        rows = db.execute("SELECT m.rowid,m.layer,m.key,m.value,m.scope,m.version,m.created "
                          "FROM adr001_memory m WHERE " + clause + " AND " + _effective_sql(),
                          params).fetchall()
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

    def _lazy_embed(self, candidates: list[dict]) -> int:
        if self.embedder is None:
            return len(candidates)
        import numpy as np

        by_id = {r["rowid"]: r for r in candidates}
        with closing(sqlite3.connect(self.path, timeout=.1)) as db:
            rows = db.execute("SELECT m.rowid,f.body,v.model_id,v.content_sha FROM adr001_memory m "
                              "JOIN adr001_fts f ON f.rowid=m.rowid LEFT JOIN adr001_vec v "
                              "ON v.rowid=m.rowid WHERE m.rowid IN (" +
                              ",".join("?" for _ in by_id) + ") ORDER BY m.created,m.rowid",
                              list(by_id)).fetchall() if by_id else []
            missing = [(rowid, body, hashlib.sha256(body.encode()).hexdigest())
                       for rowid, body, model, digest in rows
                       if model != self.model_id or digest != hashlib.sha256(body.encode()).hexdigest()]
            for rowid, body, digest in missing[:LAZY_EMBED_LIMIT]:
                try:
                    vector = np.asarray(self.embedder(body), dtype=np.float32).reshape(-1)
                    if not vector.size or not np.isfinite(vector).all():
                        continue
                    db.execute("INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) "
                               "VALUES(?,?,?,?) ON CONFLICT(rowid) DO UPDATE SET "
                               "model_id=excluded.model_id,content_sha=excluded.content_sha,vec=excluded.vec",
                               (rowid, self.model_id, digest, vector.tobytes()))
                except Exception as exc:
                    logger.warning("memory embed failed rowid=%s: %s", rowid, exc)
            db.commit()
        return max(0, len(missing) - LAZY_EMBED_LIMIT)

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
            import numpy as np
            q = np.asarray(self.embedder(query), dtype=np.float32).reshape(-1)
            qnorm = float(np.linalg.norm(q))
            for rowid, model, digest, blob in vectors:
                body = bodies.get(rowid, "")
                if model != self.model_id or digest != hashlib.sha256(body.encode()).hexdigest():
                    continue
                vector = np.frombuffer(blob, dtype=np.float32)
                if vector.size != q.size:
                    continue
                denom = qnorm * float(np.linalg.norm(vector))
                if denom:
                    semantic.append((rowid, float(np.dot(q, vector) / denom)))
        semantic.sort(key=lambda item: (-item[1], item[0]))
        vector_rank = {rowid: rank for rank, (rowid, _) in enumerate(semantic, 1)}
        lexical = {r["rowid"]: rank for rank, r in enumerate(candidates, 1)}
        candidates.sort(key=lambda row: (
            -(1 / (RRF_K + bm25[row["rowid"]]) if row["rowid"] in bm25 else 0)
            - (1 / (RRF_K + vector_rank[row["rowid"]]) if row["rowid"] in vector_rank else 0),
            lexical[row["rowid"]]))
        return candidates, bool(vector_rank)

    def retrieve_ranked(self, scope: MemoryScope, query: str, role: str,
                        k: int, byte_budget: int) -> dict:
        started = time.perf_counter()
        head = self.render_head(scope.project)
        try:
            with closing(sqlite3.connect(self.path)) as db:
                candidates = self._candidates(db, scope)
            head_ids = {r["rowid"] for r in head["records"]}
            middle = [r for r in candidates if r["rowid"] not in head_ids]
            pending = self._lazy_embed(middle)
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
                fresh = {r["rowid"] for r in self._candidates(db, scope)}
            selected = [r for r in selected if r["rowid"] in fresh]
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed > LATENCY_BUDGET_SECONDS * 1000:
                raise TimeoutError("retrieval exceeded 300 ms")
            mode = "hybrid" if vector_used else "lexical_only"
        except Exception as exc:
            logger.warning("memory retrieval fallback: %s: %s", type(exc).__name__, exc)
            self.fallback_count += 1
            terms = set(query.lower().replace("-", " ").split())
            with closing(sqlite3.connect(self.path)) as db:
                selected = [r for r in self._candidates(db, scope)
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
            pending = self.missing_vector_count()
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
