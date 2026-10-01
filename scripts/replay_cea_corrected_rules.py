#!/usr/bin/env python3
"""Replay #410 lineage handling and #412 enforcement against a read-only audit copy.

Usage: PYTHONPATH=src python3 scripts/replay_cea_corrected_rules.py
  --snapshot-dir /tmp/cea-shadow-audit-20260926-r2
  --audit docs/sev0/cea-shadow-block-precision-2026-09-26.md

The backup contains receipt decisions but not historical provider inputs. A
released ALREADY_COMPLETED decision therefore has an unknown subsequent verdict.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3

from agent_crew.cea.callsites import enforcing
from agent_crew.cea.engine import ENFORCE, EngineConfig, RELEASABLE_CONSUMED_TASK_STATUSES
from agent_crew.cea.store import task_status


PRIOR_RECEIPT = re.compile(r"\breceipt ([0-9a-f-]{36})\b")
SAMPLE = re.compile(
    r"^\| `([^`]+)` / `([0-9a-f]{8})` \|.*\*\*(CORRECT_BLOCK|FALSE_BLOCK|UNKNOWN)\*\*"
)
CONFIG = EngineConfig(mode=ENFORCE, enforce_codes=frozenset({"RUNTIME_STATE_FORBIDS"}))


def samples_from_audit(path: Path) -> dict[tuple[str, str], str]:
    text = path.read_text().split("## Sample evidence", 1)[1]
    samples = {}
    for line in text.splitlines():
        match = SAMPLE.match(line)
        if match:
            key = (match[1], match[2])
            if key in samples:
                raise ValueError(f"duplicate sample {key}")
            samples[key] = match[3]
    return samples


def replay(snapshot_dir: Path, audit: Path) -> dict:
    samples = samples_from_audit(audit)
    rows = []
    for path in sorted(snapshot_dir.glob("*.db")):
        # immutable ensures this script never changes even a backup's WAL files.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='authorization_receipts'").fetchone():
                continue
            decisions = conn.execute(
                "SELECT receipt_id, task_id, decision, reason FROM authorization_receipts "
                "WHERE seq=0 ORDER BY receipt_id").fetchall()
            for decision in decisions:
                reason = json.loads(decision["reason"])
                code = reason["code"]
                record = {
                    "project": path.stem,
                    "receipt_id": decision["receipt_id"],
                    "task_id": decision["task_id"],
                    "before": f"{decision['decision']}:{code}",
                    "after": f"{decision['decision']}:{code}",
                    "prior_task_status": None,
                }
                if code == "ALREADY_COMPLETED":
                    match = PRIOR_RECEIPT.search(reason.get("text", ""))
                    if not match:
                        raise ValueError(f"missing lineage receipt in {decision['receipt_id']}")
                    prior = conn.execute(
                        "SELECT task_id FROM authorization_receipts "
                        "WHERE receipt_id=? AND seq=0", (match[1],)).fetchone()
                    prior_status = task_status(conn, prior["task_id"]) if prior else None
                    record["prior_task_status"] = prior_status
                    if prior_status in RELEASABLE_CONSUMED_TASK_STATUSES:
                        record["after"] = "RE_EVALUATE:UNKNOWN"
                record["stops_on_recorded_code"] = (
                    record["after"] != "RE_EVALUATE:UNKNOWN"
                    and decision["decision"] != "ALLOW"
                    and enforcing(CONFIG, reason_code=code)
                )
                key = (path.stem, decision["receipt_id"][:8])
                record["sample_label"] = samples.get(key)
                rows.append(record)
        finally:
            conn.close()

    matched = sum(row["sample_label"] is not None for row in rows)
    if matched != len(samples):
        raise ValueError(f"matched {matched} of {len(samples)} audit samples")
    return {
        "snapshot_dir": str(snapshot_dir),
        "decisions": len(rows),
        "sample_count": matched,
        "before": dict(sorted(Counter(r["before"] for r in rows).items())),
        "after": dict(sorted(Counter(r["after"] for r in rows).items())),
        "alfred_before": dict(sorted(Counter(r["before"] for r in rows if r["project"] == "alfred").items())),
        "alfred_after": dict(sorted(Counter(r["after"] for r in rows if r["project"] == "alfred").items())),
        "known_stops": [r["receipt_id"] for r in rows if r["stops_on_recorded_code"]],
        "false_blocks_still_stop": [r["receipt_id"] for r in rows
                                    if r["sample_label"] == "FALSE_BLOCK" and r["stops_on_recorded_code"]],
        "supported_blocks_proceed": [r["receipt_id"] for r in rows
                                     if r["sample_label"] == "CORRECT_BLOCK"
                                     and not r["stops_on_recorded_code"]],
        "changed_already_completed": [r for r in rows if r["before"] == "BLOCK:ALREADY_COMPLETED"
                                      and r["after"] == "RE_EVALUATE:UNKNOWN"],
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.snapshot_dir, args.audit), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
