#!/usr/bin/env python3
"""Reproducible, isolated QMD R5 export and retrieval evaluation.

The source database is opened read-only and copied with SQLite's online backup.
All QMD commands receive a private INDEX_PATH and QMD_CONFIG_DIR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import math
import re
import resource
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

from eval_embedding_models import EVAL_SHA256, load_eval_set, resolve_expected_keys


def backup(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        raise ValueError("source and target must differ")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80] or "empty"


def export_rows(db_path: Path, root: Path) -> tuple[dict, dict]:
    """Write exactly one document per effective row; preserve keys in metadata."""
    manifest = {}
    projects = set()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT rowid,* FROM adr001_memory WHERE superseded_by IS NULL AND invalidated_at IS NULL ORDER BY rowid")
        for row in rows:
            try:
                scope = json.loads(row["scope"] or "{}")
            except (TypeError, ValueError):
                scope = {}
            project = str(scope.get("project") or "")
            fleet = str(scope.get("fleet") or "")
            group = "project" if project else "fleet" if fleet else "global"
            name = safe_part(project or fleet or "global")
            folder = root / group / name
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{row['rowid']:08d}.md"
            try:
                value = json.loads(row["value"])
            except (TypeError, ValueError):
                value = row["value"]
            body = value.get("text", "") if isinstance(value, dict) else str(value)
            if isinstance(value, dict):
                body += "\n" + json.dumps({k: v for k, v in value.items() if k != "text"}, ensure_ascii=False)
            metadata = {"key": row["key"], "layer": row["layer"], "scope": scope}
            path.write_text("---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in metadata.items()) + "\n---\n" + body + "\n")
            manifest[str(path)] = {"key": row["key"], "project": project, "fleet": fleet,
                                   "korean": bool(re.search(r"[\uac00-\ud7a3]", body))}
            if project:
                projects.add(project)
    link_fleet_views(root, projects)
    return manifest, {"projects": sorted(projects), "effective_rows": len(manifest)}


def link_fleet_views(root: Path, projects: set[str] | list[str]) -> None:
    """Expose fleet rows inside each project collection without copying bodies."""
    fleet_dir = root / "fleet" / "fleet"
    if not fleet_dir.exists():
        return
    for project in projects:
        target_dir = root / "project" / safe_part(project)
        for source in fleet_dir.glob("*.md"):
            target = target_dir / f"fleet_{source.name}"
            target.unlink(missing_ok=True)
            os.link(source, target)


def qmd_env(work: Path) -> dict:
    env = os.environ.copy()
    env["INDEX_PATH"] = str(work / "index.sqlite")
    env["QMD_CONFIG_DIR"] = str(work / "config")
    return env


def qmd(args: list[str], env: dict, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["qmd", *args], text=True, capture_output=True, env=env, check=check)


def create_collection(work: Path, documents: Path, env: dict) -> None:
    for group in ("project", "fleet", "global"):
        group_dir = documents / group
        if not group_dir.exists():
            continue
        for folder in sorted(group_dir.iterdir()):
            name = f"adr001_{group}_{folder.name}"
            qmd(["collection", "add", str(folder), "--name", name, "--mask", "**/*.md"], env)
    qmd(["update"], env)


def result_paths(stdout: str) -> list[str]:
    data = json.loads(stdout)
    if isinstance(data, dict):
        data = data.get("results", data.get("files", []))
    return [str(row.get("file") or row.get("path") or row.get("filepath") or "") for row in data]


def vector_query(query: str) -> str:
    """QMD vec rejects standalone lexical negations; remove those tokens."""
    without_exclusions = re.sub(r"(?<!\w)-{1,2}\w+", "", query)
    return re.sub(r"\s+", " ", without_exclusions.replace("-", " ")).strip()


def virtual_manifest(manifest: dict) -> dict:
    lookup = {}
    projects = {row["project"] for row in manifest.values() if row["project"]}
    for absolute, row in manifest.items():
        parts = Path(absolute).parts
        group, scope = parts[-3:-1]
        name = f"adr001_{group}_{scope}"
        lookup[f"qmd://{name}/{Path(absolute).name}"] = row
        if row["fleet"] == "fleet":
            for project in projects:
                lookup[f"qmd://adr001_project_{safe_part(project)}/fleet_{Path(absolute).name}"] = row
    return lookup


def checkpoint_samples(work: Path, arm: str) -> tuple[list[dict], Path]:
    path = work / f"samples_{arm}.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return samples, path


def append_sample(path: Path, sample: dict) -> None:
    with path.open("a") as output:
        output.write(json.dumps(sample, ensure_ascii=False) + "\n")


def eval_cases(data: dict, db_path: Path, manifest: dict) -> list[dict]:
    cases = []
    rows_by_key = {}
    for row in manifest.values():
        rows_by_key.setdefault(row["key"], []).append(row)
    for kind, records in (("owner", data["owner_cases"]), ("task", data["task_cases"])):
        for case in records:
            project = case.get("project", "")
            expected = {key for key in resolve_expected_keys(db_path, case)
                        if any(row["project"] == project or row["fleet"] == "fleet" or
                               (not row["project"] and not row["fleet"])
                               for row in rows_by_key.get(key, []))}
            korean = any(row["korean"] for key in expected for row in rows_by_key[key])
            cases.append({"kind": kind, "id": case.get("id", case.get("case_id")),
                          "query": re.sub(r"\s+", " ", case["query"]).strip(),
                          "project": project,
                          "expected": expected, "korean_target": korean})
    return cases


def run_cli(cases: list[dict], manifest: dict, env: dict, repetitions: int = 5,
            selected_arms: set[str] | None = None, checkpoint_dir: Path | None = None) -> dict:
    arms = {"search": ["search"], "vsearch": ["vsearch"],
            "query_rerank": ["query"], "query_no_rerank": ["query", "--no-rerank"]}
    output = {}
    virtual = virtual_manifest(manifest)
    available_projects = {row["project"] for row in manifest.values() if row["project"]}
    for name, command in arms.items():
        if selected_arms is not None and name not in selected_arms:
            continue
        samples, checkpoint = checkpoint_samples(checkpoint_dir, name) if checkpoint_dir else ([], None)
        done = {(s["rep"], s["id"]) for s in samples}
        for rep in range(repetitions):
            for case in cases:
                if (rep, case["id"]) in done:
                    continue
                started = time.perf_counter()
                collections = ([f"adr001_project_{safe_part(case['project'])}"]
                               if case["project"] in available_projects else ["adr001_fleet_fleet"])
                query = vector_query(case["query"]) if name == "vsearch" else case["query"]
                result = qmd([command[0], query, *command[1:], "--json", "-n", "10",
                              *[item for collection in collections for item in ("-c", collection)]], env, check=False)
                elapsed = (time.perf_counter() - started) * 1000
                if result.returncode:
                    raise RuntimeError(f"{name} failed ({result.returncode}): {result.stderr[-2000:]}")
                paths = result_paths(result.stdout)
                scoped = [virtual[p]["key"] for p in paths if p in virtual][:10]
                sample = {"rep": rep, "id": case["id"], "kind": case["kind"],
                          "korean_target": case["korean_target"], "hit": bool(case["expected"] & set(scoped)),
                          "latency_ms": elapsed}
                samples.append(sample)
                if checkpoint:
                    append_sample(checkpoint, sample)
        output[name] = samples
    return output


def run_warm_mcp(cases: list[dict], manifest: dict, url: str, repetitions: int = 5,
                 selected_arms: set[str] | None = None, checkpoint_dir: Path | None = None) -> dict:
    """Use one persistent QMD MCP server; typed searches are not CLI expansion."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload: dict) -> tuple[dict, object]:
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read()), response.headers

    init, response_headers = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "adr001-r5-eval", "version": "1"}}})
    if "result" not in init:
        raise RuntimeError(f"MCP initialize failed: {init}")
    headers["mcp-session-id"] = response_headers["mcp-session-id"]
    virtual = virtual_manifest(manifest)
    available_projects = {row["project"] for row in manifest.values() if row["project"]}
    arms = {
        "mcp_lex": (["lex"], False),
        "mcp_vec": (["vec"], False),
        "mcp_hybrid_rerank": (["lex", "vec"], True),
        "mcp_hybrid_no_rerank": (["lex", "vec"], False),
    }
    output = {}
    request_id = 2
    for name, (types, rerank) in arms.items():
        if selected_arms is not None and name not in selected_arms:
            continue
        samples, checkpoint = checkpoint_samples(checkpoint_dir, name) if checkpoint_dir else ([], None)
        done = {(s["rep"], s["id"]) for s in samples}
        for rep in range(repetitions):
            for case in cases:
                if (rep, case["id"]) in done:
                    continue
                collections = ([f"adr001_project_{safe_part(case['project'])}"]
                               if case["project"] in available_projects else ["adr001_fleet_fleet"])
                payload = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                    "name": "query", "arguments": {"searches": [{"type": t, "query": vector_query(case["query"]) if t == "vec" else case["query"]} for t in types],
                    "collections": collections, "rerank": rerank, "limit": 10}}}
                request_id += 1
                started = time.perf_counter()
                response, _ = post(payload)
                elapsed = (time.perf_counter() - started) * 1000
                if response.get("error") or response.get("result", {}).get("isError"):
                    raise RuntimeError(f"MCP {name} failed: {response}")
                rows = response["result"]["structuredContent"]["results"]
                found = {virtual[f"qmd://{row['file']}"]["key"] for row in rows
                         if f"qmd://{row['file']}" in virtual}
                sample = {"rep": rep, "id": case["id"], "kind": case["kind"],
                          "korean_target": case["korean_target"],
                          "hit": bool(case["expected"] & found), "latency_ms": elapsed}
                samples.append(sample)
                if checkpoint:
                    append_sample(checkpoint, sample)
        output[name] = samples
    return output


def summarize_results(results: dict) -> dict:
    summary = {}
    for name, samples in results["arms"].items():
        first = [sample for sample in samples if sample["rep"] == 0]
        latencies = sorted(sample["latency_ms"] for sample in samples)
        pick = lambda p: round(latencies[math.ceil(p * len(latencies)) - 1], 1)
        summary[name] = {
            "owner_hits": sum(s["hit"] for s in first if s["kind"] == "owner"),
            "task_hits": sum(s["hit"] for s in first if s["kind"] == "task"),
            "owner_korean_hits": sum(s["hit"] for s in first if s["kind"] == "owner" and s["korean_target"]),
            "owner_english_hits": sum(s["hit"] for s in first if s["kind"] == "owner" and not s["korean_target"]),
            "p50_ms": pick(.5), "p95_ms": pick(.95), "samples": len(samples),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--eval-set", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("export", "embed", "eval", "warm", "summarize", "cleanup"), required=True)
    parser.add_argument("--arms", nargs="*", default=None)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--mcp-url", default="http://localhost:18176/mcp")
    args = parser.parse_args()
    work = args.work
    if work.resolve() == Path("/dev/shm").resolve() or not work.resolve().is_relative_to(Path("/dev/shm").resolve()):
        raise ValueError("work directory must be under /dev/shm")
    if args.stage == "cleanup":
        shutil.rmtree(work, ignore_errors=True)
        return
    data = load_eval_set(args.eval_set, EVAL_SHA256)
    if args.stage == "summarize":
        print(json.dumps(summarize_results(json.loads(args.output.read_text())), indent=2))
        return
    work.mkdir(parents=True, exist_ok=True)
    copy = work / "adr001_memory.db"
    env = qmd_env(work)
    if args.stage == "export":
        backup(args.db, copy)
        shutil.rmtree(work / "documents", ignore_errors=True)
        manifest, stats = export_rows(copy, work / "documents")
        stats["snapshot_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
        (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
        (work / "export.json").write_text(json.dumps(stats))
        create_collection(work, work / "documents", env)
        print(json.dumps(stats))
    elif args.stage == "embed":
        start = time.perf_counter()
        result = qmd(["embed"], env, check=False)
        wall = time.perf_counter() - start
        rss_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        (work / "embed.json").write_text(json.dumps({"wall_s": wall, "peak_rss_kib": rss_kib,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}))
        print(json.dumps({"wall_s": wall, "returncode": result.returncode}))
        if result.returncode:
            raise SystemExit(result.returncode)
    else:
        manifest = json.loads((work / "manifest.json").read_text())
        cases = eval_cases(data, copy, manifest)
        if args.stage == "warm":
            results = run_warm_mcp(cases, manifest, args.mcp_url, args.repetitions,
                                   set(args.arms) if args.arms else None, work)
        else:
            results = run_cli(cases, manifest, env, args.repetitions,
                              set(args.arms) if args.arms else None, work)
        previous = json.loads(args.output.read_text()) if args.output.exists() else {"arms": {}}
        previous["cases"] = [{**c, "expected": sorted(c["expected"])} for c in cases]
        previous["arms"].update(results)
        args.output.write_text(json.dumps(previous, ensure_ascii=False))
        print(json.dumps({"cases": len(cases), "arms": list(results)}))


if __name__ == "__main__":
    main()
