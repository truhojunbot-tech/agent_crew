#!/usr/bin/env python3
"""One-shot import of Blackboard JSONL and procedural episodes into shadow memory.

PYTHONPATH=src python3 scripts/import_shadow_memory.py DB --blackboard-jsonl FILE
PYTHONPATH=src python3 scripts/import_shadow_memory.py DB --episodes-jsonl FILE --project agent_crew
The input file contains one JSON object per line. No live-read flag is changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from agent_crew.memory_capture import capture_blackboard_result, capture_episode
from agent_crew.memory_runtime import SQLiteMemoryStorage


def import_file(db: Path, source: Path, *, kind: str, project: str = "",
                repo: str = "") -> dict:
    if not db.is_file():
        raise FileNotFoundError(db)
    storage = SQLiteMemoryStorage(str(db))
    counts = Counter()
    with source.open(encoding="utf-8") as lines:
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if kind == "blackboard":
                    capture_blackboard_result(storage, data)
                else:
                    capture_episode(storage, data, project=project or data.get("project", ""),
                                    repo=repo or data.get("repo", ""))
                counts["stored"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                counts["rejected"] += 1
                print(json.dumps({"event": "shadow_memory_import_rejected", "line": number,
                                  "reason": str(exc)}))
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--blackboard-jsonl", type=Path)
    inputs.add_argument("--episodes-jsonl", type=Path)
    parser.add_argument("--project", default="")
    parser.add_argument("--repo", default="")
    args = parser.parse_args()
    kind = "blackboard" if args.blackboard_jsonl else "episodes"
    source = args.blackboard_jsonl or args.episodes_jsonl
    print(json.dumps(import_file(args.db, source, kind=kind,
                                 project=args.project, repo=args.repo), sort_keys=True))


if __name__ == "__main__":
    main()
