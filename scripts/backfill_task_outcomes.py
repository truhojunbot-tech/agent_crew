#!/usr/bin/env python3
"""Backfill ADR-001 task outcomes and audit frozen task-reference coverage.

Pass copies of crew databases when evaluating a live corpus. The command
never writes a task DB or an eval set; only --memory-db is writable.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from agent_crew.memory_capture import backfill_task_outcomes
from agent_crew.memory_runtime import SQLiteMemoryStorage


def count_available_refs(memory_db: str, eval_set: str) -> tuple[int, int, list[tuple[str, str]]]:
    cases = json.loads(Path(eval_set).read_text(encoding="utf-8"))["task_cases"]
    with sqlite3.connect(f"{Path(memory_db).resolve().as_uri()}?mode=ro", uri=True) as db:
        rows = [(key, json.loads(value)) for key, value in db.execute(
            "SELECT key,value FROM adr001_memory")]
    missing = []
    hits = 0
    for case in cases:
        found = False
        for expected in case["expected"]:
            ref = expected["ref"]
            kind = expected["source_kind"]
            if kind == "issue_or_pr":
                project = (re.search(r"(?:github\.com/[^/]+/([^/]+)/(?:issues|pull)/|^([\w-]+) )", ref)
                           or None)
                project = (project.group(1) or project.group(2)) if project else case["project"]
                issue = re.search(r"(?:issue(?:s)?|PR|pull)/?\s*#?(\d+)", ref, re.I)
                if issue:
                    number = int(issue.group(1))
                    field = "pr_number" if re.search(r"\bPR\b|/pull/", ref, re.I) else "issue"
                    found = any(value.get("kind") == "task_outcome"
                                and value.get("project") == project
                                and str(value.get(field) or "") == str(number)
                                for _, value in rows)
            elif kind == "lineage_finding":
                task_ids = re.findall(r"\b(?:review|impl|fix-review)-[\w-]+", ref)
                found = any(value.get("kind") == "task_outcome"
                            and value.get("task_id") in task_ids
                            and value.get("project") == case["project"]
                            for _, value in rows)
            else:
                # Non-task sources must exist by their own exact identity.
                stored_key = ref.removeprefix("adr001:")
                found = any(key == stored_key or value.get("source_ref") == ref
                            for key, value in rows)
            if found:
                break
        if found:
            hits += 1
        else:
            missing.append((case["case_id"], "; ".join(e["ref"] for e in case["expected"])))
    return hits, len(cases), missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-db", required=True, help="writable ADR-001 DB (use a copy)")
    parser.add_argument("--tasks-db", action="append", default=[],
                        help="crew tasks DB to read; repeat for each project (use copies)")
    parser.add_argument("--eval-set", required=True, help="frozen eval JSON (read only)")
    args = parser.parse_args()
    storage = SQLiteMemoryStorage.existing(args.memory_db)
    changed = backfill_task_outcomes(storage, args.tasks_db) if args.tasks_db else 0
    hits, total, missing = count_available_refs(args.memory_db, args.eval_set)
    print(f"changed={changed} expected refs available={hits}/{total}")
    for case_id, refs in missing:
        print(f"missing {case_id}: {refs}")


if __name__ == "__main__":
    main()
