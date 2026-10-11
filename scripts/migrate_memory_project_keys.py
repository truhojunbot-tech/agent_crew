#!/usr/bin/env python3
"""One-shot authoritative project-key migration. Never run from the server.

PYTHONPATH=src python3 scripts/migrate_memory_project_keys.py DB [--apply]
Dry-run is the default. Apply creates DB.bak with SQLite's backup API first.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from agent_crew.memory_capture import canonical_project


def plan(db: sqlite3.Connection) -> tuple[list[tuple], list[tuple], dict]:
    rows = db.execute(
        "SELECT layer,key,value,scope,version,created FROM adr001_memory "
        "WHERE layer='authoritative' ORDER BY key,scope").fetchall()
    before, after, planned = Counter(), Counter(), []
    changed = 0
    for layer, key, value_json, scope_json, version, created in rows:
        value, scope = json.loads(value_json), json.loads(scope_json)
        old = str(scope.get("project") or "")
        repo = str(value.get("repo") or value.get("source_ref") or value.get("link") or "")
        project = canonical_project(old, repo=repo)
        before[old] += 1
        after[project] += 1
        new_key = key.replace(f"owner:{old}:", f"owner:{project}:", 1)
        scope["project"] = project
        for field in ("project", "target_project"):
            if value.get(field) == old:
                value[field] = project
        if isinstance(value.get("supersedes"), list):
            value["supersedes"] = [link.replace(f"owner:{old}:", f"owner:{project}:", 1)
                                   for link in value["supersedes"]]
        if new_key != key or value != json.loads(value_json) or scope != json.loads(scope_json):
            changed += 1
        planned.append((layer, new_key, json.dumps(value), json.dumps(scope, sort_keys=True),
                        version, created))
    identities = [(row[0], row[1], row[3]) for row in planned]
    if len(identities) != len(set(identities)):
        raise ValueError("migration would collide with an existing authoritative key")
    return rows, planned, {"before": dict(sorted(before.items())),
                           "after": dict(sorted(after.items())),
                           "changed": changed}


def migrate(path: Path, *, apply: bool = False) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as source:
        old, new, counts = plan(source)
        if not apply:
            return {**counts, "dry_run": True, "backup": None}
        backup = path.with_name(path.name + ".bak")
        if backup.exists():
            raise FileExistsError(backup)
        try:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        except Exception:
            backup.unlink(missing_ok=True)
            raise
    committed = False
    try:
        with closing(sqlite3.connect(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Recheck under the write lock; never apply a stale dry-run plan.
                current, planned, latest = plan(db)
                if current != old or latest != counts:
                    raise RuntimeError("memory DB changed after backup; migration refused")
                # Owner put() rightly refuses mutation. This one-shot identity
                # rewrite preserves verified text/text_sha256 and only changes
                # project scopes, owner keys, and supersedes links atomically.
                db.execute("DELETE FROM adr001_memory WHERE layer='authoritative'")
                db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                               "VALUES (?,?,?,?,?,?)", planned)
                db.commit()
                committed = True
            except Exception:
                db.rollback()
                raise
    except Exception:
        # This invocation created the backup. A refused transaction left the
        # source untouched, so remove only our backup to permit a safe retry.
        if not committed:
            backup.unlink(missing_ok=True)
        raise
    return {**counts, "dry_run": False, "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=args.apply), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
