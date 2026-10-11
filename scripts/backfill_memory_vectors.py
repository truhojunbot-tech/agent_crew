"""Resume ADR-001 vector backfill against an explicitly configured endpoint.

Set AGENT_CREW_MEMORY_EMBED_ENDPOINT to a dedicated serving endpoint and
AGENT_CREW_MEMORY_MODEL_ID to the selected model. No endpoint is assumed.
Each vector is committed separately, so interrupting and rerunning is safe.
"""
from __future__ import annotations

import argparse
import json

from agent_crew.memory_hybrid import HybridMemoryStorage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="ADR-001 SQLite database path")
    parser.add_argument("--limit", type=int, default=None,
                        help="maximum vectors to write this run")
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args()
    store = HybridMemoryStorage(args.db)
    if store.write_embedder is None:
        parser.error("set AGENT_CREW_MEMORY_EMBED_ENDPOINT and AGENT_CREW_MEMORY_MODEL_ID")
    written = store.backfill_vectors(limit=args.limit, batch_size=args.batch_size)
    print(json.dumps({"model_id": store.model_id, "written": written}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
