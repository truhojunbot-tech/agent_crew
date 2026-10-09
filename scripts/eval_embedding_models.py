#!/usr/bin/env python3
"""Score ADR-001 ONNX embedders through A2 retrieval on a copied SQLite DB.

No runtime configuration or source memory database is written. The frozen eval
set is checked before model downloads and before any scoring begins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_crew.memory_hybrid import HybridMemoryStorage
from agent_crew.memory_runtime import MemoryScope

EVAL_SHA256 = "0bdb218d0c8e6ba3da8202c19201fee52a9605664ebe3ea4fb1d5de92de4ac61"
EVAL_PATH = Path.home() / "alfred/instances/Quota/eval/adr001_eval_set_20261009.json"
MEMORY_DB = Path.home() / ".agent_crew/memory/adr001_memory.db"
MODEL_IDS = (
    "intfloat/multilingual-e5-small",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "BAAI/bge-m3",
    "all-MiniLM-L6-v2",
    "lexical_only",
)


def load_eval_set(path: Path, expected_sha: str = EVAL_SHA256) -> dict:
    raw = Path(path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha:
        raise ValueError(f"eval-set sha256 mismatch: expected {expected_sha}, got {actual}")
    data = json.loads(raw)
    if len(data.get("owner_cases", ())) != 17 or len(data.get("task_cases", ())) != 24:
        # Small synthetic fixtures remain useful to callers/tests when they
        # explicitly supply their own hash; the production hash is frozen.
        if expected_sha == EVAL_SHA256:
            raise ValueError("frozen eval set must contain 17 owner and 24 task cases")
    return data


def copy_database(source: Path, destination: Path) -> None:
    """Use SQLite's online backup so a live WAL snapshot is internally sound."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("source and destination memory DB must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as copy:
            original.backup(copy)


def expected_keys(case: dict) -> list[str]:
    return [item["ref"] if isinstance(item, dict) else item
            for item in case["expected"]]


def score_hit(expected: list, response: dict) -> bool:
    """Case-level recall: any exact expected memory key in head or middle."""
    gold = {item["ref"] if isinstance(item, dict) else item for item in expected}
    return bool(gold.intersection(row["key"] for row in
                                  response["head"] + response["middle"]))


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(.95 * len(ordered)) - 1]


def choose_model(rows: list[dict]) -> dict | None:
    eligible = [r for r in rows if r.get("p95_retrieve_ms") is not None
                and r["p95_retrieve_ms"] <= 300
                and r["model_id"] not in {"all-MiniLM-L6-v2", "lexical_only"}]
    if not eligible:
        return None
    return max(eligible, key=lambda r: (
        r["owner_hits"] + r["task_hits"], r["task_hits"], r["owner_hits"],
        -r["p95_retrieve_ms"], -r["model_size_bytes"]))


class EvalOnnxEmbedder:
    """CPU ONNX mean pooling with A2's callable/model_id contract."""

    def __init__(self, model_path: Path, tokenizer_path: Path, model_id: str,
                 query_texts: set[str]):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.model_id = model_id
        self.query_texts = query_texts
        self.timings_ms: list[float] = []

    def __call__(self, text: str):
        import numpy as np

        started = time.perf_counter()
        if self.model_id == "intfloat/multilingual-e5-small":
            text = ("query: " if text in self.query_texts else "passage: ") + text
        encoded = self.tokenizer.encode(text)
        ids = np.asarray([encoded.ids[:256]], dtype=np.int64)
        mask = np.ones_like(ids)
        segments = np.zeros_like(ids)
        feeds = {meta.name: (mask if "mask" in meta.name else
                             segments if "token_type" in meta.name else ids)
                 for meta in self.session.get_inputs()}
        output = self.session.run(None, feeds)[0]
        if output.ndim == 3:
            vector = (output * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
        elif output.ndim == 2:
            vector = output
        else:
            raise ValueError(f"unexpected ONNX output shape {output.shape}")
        vector = vector[0].astype(np.float32)
        self.timings_ms.append((time.perf_counter() - started) * 1000)
        return vector


def download_model(model_id: str, cache_dir: Path, query_texts: set[str]):
    if model_id == "lexical_only":
        return None, 0
    if model_id == "all-MiniLM-L6-v2":
        directory = Path.home() / ".cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx"
        model, tokenizer = directory / "model.onnx", directory / "tokenizer.json"
    else:
        from huggingface_hub import hf_hub_download

        names = ["onnx/model.onnx", "tokenizer.json"]
        if model_id == "BAAI/bge-m3":
            names.append("onnx/model.onnx_data")
        paths = [Path(hf_hub_download(model_id, name, cache_dir=str(cache_dir)))
                 for name in names]
        model, tokenizer = paths[0], paths[1]
    if not model.is_file() or not tokenizer.is_file():
        raise FileNotFoundError(f"ONNX model/tokenizer unavailable for {model_id}")
    size = model.stat().st_size + tokenizer.stat().st_size
    external = model.parent / "model.onnx_data"
    if external.is_file():
        size += external.stat().st_size
    return EvalOnnxEmbedder(model, tokenizer, model_id, query_texts), size


def count_available_keys(source: Path, cases: list[dict]) -> int:
    """Report corpus coverage separately from retrieval recall."""
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
        keys = {row[0] for row in db.execute("SELECT key FROM adr001_memory")}
    return sum(bool(keys.intersection(expected_keys(case))) for case in cases)


def score_model(source: Path, workdir: Path, model_id: str, data: dict,
                cache_dir: Path, *, k: int, byte_budget: int) -> dict:
    db_path = workdir / (model_id.replace("/", "_") + ".db")
    copy_database(source, db_path)
    cases = data["owner_cases"] + data["task_cases"]
    embedder, size = download_model(model_id, cache_dir,
                                    {case["query"] for case in cases})
    storage = HybridMemoryStorage(str(db_path), embedder=embedder)
    hits = {"owner_cases": 0, "task_cases": 0}
    latencies: list[float] = []
    modes: dict[str, int] = {}
    for group in ("owner_cases", "task_cases"):
        for case in data[group]:
            response = storage.retrieve_ranked(
                MemoryScope(project=case["project"]), case["query"],
                "eval", k, byte_budget)
            hits[group] += int(score_hit(case["expected"], response))
            latencies.append(response["latency_ms"])
            modes[response["mode"]] = modes.get(response["mode"], 0) + 1
    return {"model_id": model_id, "owner_hits": hits["owner_cases"],
            "task_hits": hits["task_cases"], "p95_embed_ms":
            p95(embedder.timings_ms) if embedder else None,
            "p95_retrieve_ms": p95(latencies), "model_size_bytes": size,
            "retrieval_modes": modes, "fallback_count": storage.fallback_count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=MEMORY_DB)
    parser.add_argument("--eval-set", type=Path, default=EVAL_PATH)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path.home() / ".cache/huggingface/hub")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=MODEL_IDS, default=MODEL_IDS)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--byte-budget", type=int, default=65536)
    args = parser.parse_args()
    data = load_eval_set(args.eval_set)
    if args.k <= 0 or args.byte_budget <= 0:
        parser.error("--k and --byte-budget must be positive")
    with tempfile.TemporaryDirectory(prefix="adr001-embed-eval-") as tmp:
        # Freeze one online snapshot before scoring so every model sees the
        # same corpus even if the live source receives new memory rows.
        snapshot = Path(tmp) / "source-snapshot.db"
        copy_database(args.db, snapshot)
        coverage = {group: count_available_keys(snapshot, data[group])
                    for group in ("owner_cases", "task_cases")}
        rows = [score_model(snapshot, Path(tmp), model, data, args.cache_dir,
                            k=args.k, byte_budget=args.byte_budget)
                for model in args.models]
    report = {"eval_sha256": EVAL_SHA256, "source_db": str(args.db),
              "k": args.k, "byte_budget": args.byte_budget,
              "corpus_available_cases": coverage, "rows": rows,
              "chosen_model_id": (choose_model(rows) or {}).get("model_id")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
