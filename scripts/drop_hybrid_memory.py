#!/usr/bin/env python3
"""Roll back the ADR-001 hybrid schema after every shared-DB writer is stopped.

PYTHONPATH=src python3 scripts/drop_hybrid_memory.py DB [--apply]
Dry-run is the default. Apply saves DB.hybrid-rollback.bak before modifying DB.
Requires SQLite 3.37+ for DROP COLUMN. Keep hybrid mode disabled afterward.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


_BASE_COLUMNS = {"layer", "key", "value", "scope", "version", "created"}
_HYBRID_COLUMNS = {"superseded_by", "invalidated_at"}
_TRIGGERS = (
    "adr001_no_legacy_flags_bi", "adr001_no_legacy_flags_bu",
    "adr001_fts_ai", "adr001_fts_au", "adr001_fts_ad",
    "adr001_fts_effectiveness_au",
)


def drop_hybrid_schema(path: Path, *, apply: bool = False) -> dict:
    path = Path(path)
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise RuntimeError("SQLite 3.37+ is required to drop hybrid memory columns")
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as source:
        columns = {row[1] for row in source.execute("PRAGMA table_info(adr001_memory)")}
        if columns != _BASE_COLUMNS | _HYBRID_COLUMNS:
            raise ValueError("expected exactly the eight-column hybrid adr001_memory table")
        invalidated = source.execute(
            "SELECT count(*) FROM adr001_memory WHERE invalidated_at IS NOT NULL").fetchone()[0]
        superseded = source.execute(
            "SELECT count(*) FROM adr001_memory WHERE superseded_by IS NOT NULL").fetchone()[0]
        result = {"invalidated": invalidated, "superseded": superseded,
                  "dry_run": not apply, "backup": None}
        if not apply:
            return result
        backup = path.with_name(path.name + ".hybrid-rollback.bak")
        if backup.exists():
            raise FileExistsError(backup)
        try:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        except Exception:
            backup.unlink(missing_ok=True)
            raise
    with closing(sqlite3.connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if {row[1] for row in db.execute("PRAGMA table_info(adr001_memory)")} != columns:
                raise RuntimeError("memory schema changed after backup")
            # Drop the guard before writing legacy JSON markers. The markers
            # preserve both exclusions when the old six-column reader returns.
            for trigger in _TRIGGERS:
                db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            db.execute("UPDATE adr001_memory SET value=json_set(value,'$.invalidated_at',invalidated_at) "
                       "WHERE invalidated_at IS NOT NULL")
            db.execute("UPDATE adr001_memory SET value=json_set(value,'$.superseded_at'," 
                       "COALESCE(invalidated_at,created)) WHERE superseded_by IS NOT NULL")
            db.execute("DROP TABLE IF EXISTS adr001_fts")
            db.execute("DROP TABLE IF EXISTS adr001_vec")
            db.execute("ALTER TABLE adr001_memory DROP COLUMN superseded_by")
            db.execute("ALTER TABLE adr001_memory DROP COLUMN invalidated_at")
            db.commit()
        except Exception:
            db.rollback()
            raise
    result.update(dry_run=False, backup=str(backup))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(drop_hybrid_schema(args.db, apply=args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
