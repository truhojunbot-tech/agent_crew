#!/usr/bin/env python3
"""Refuse to move a CLI checkout while live crew servers import from it.

Usage: python3 scripts/cli_checkout_move_preflight.py ~/alfred/projects/agent_crew
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path


def health(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
        return json.load(response)


def _env_value(raw: bytes, name: bytes) -> str:
    prefix = name + b"="
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix):].decode("utf-8", errors="replace")
    return ""


def _same_path(value: str, wanted: Path) -> bool:
    return bool(value) and Path(value).resolve() == wanted


def importers(checkout: Path, *, proc_root: Path = Path("/proc"),
              state_root: Path | None = None) -> list[tuple[int, str, str, str]]:
    """Return (pid, port, project, matched path); never return other env data."""
    source = (checkout / "src").resolve()
    package = source / "agent_crew"
    found: dict[int, tuple[int, str, str, str]] = {}

    # The process environment catches older servers without /health provenance.
    # A process owned by another user may hide environ; skip only that entry.
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            if not any(b"agent_crew.server:app" in arg or
                       arg == b"agent_crew.server" for arg in cmdline):
                continue
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        pythonpath = _env_value(environ, b"PYTHONPATH")
        if not any(_same_path(part, source) for part in pythonpath.split(os.pathsep)):
            continue
        pid = int(entry.name)
        found[pid] = (pid, _env_value(environ, b"AGENT_CREW_PORT") or "-",
                      _env_value(environ, b"AGENT_CREW_PROJECT") or "-", str(source))

    # Health can identify a live importer even when /proc/<pid>/environ is
    # unreadable across the server's client-group boundary.
    states = state_root if state_root is not None else Path.home() / ".agent_crew"
    for state_path in states.glob("*/state.json"):
        try:
            port = int(json.loads(state_path.read_text())["port"])
            if not 0 < port < 65536:
                continue
            report = health(port)
            build = report.get("build") or {}
            pid = int(build["pid"])
            matched = build.get("source_root") or ""
            if pid <= 0 or not _same_path(matched, package):
                continue
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        found[pid] = (pid, str(port), str(report.get("project") or state_path.parent.name),
                      str(package))

    return [found[pid] for pid in sorted(found)]


def main(argv: list[str] | None = None, *, proc_root: Path = Path("/proc"),
         state_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args(argv)
    rows = importers(args.checkout, proc_root=proc_root, state_root=state_root)
    if not rows:
        print("no live crew server imports from this checkout")
        return 0
    for pid, port, project, matched in rows:
        print(f"pid={pid} port={port} project={project} matched_path={matched}")
    print("checkout move refused: swap it to a pinned runtime first")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
