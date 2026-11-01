#!/usr/bin/env python3
"""Report and migrate project tester roles to the #772 script stage.

Default is read-only. Review the JSON report for every project, then use its
sha256 with --apply --accept-report-sha256. A state change after the report
invalidates the hash. General test tasks use the script tester even when an
older state file opted into a provider tester (#924). This tool never restarts
a dispatcher.

Usage: PYTHONPATH=src python3 scripts/migrate_script_tester.py \
       [--crew-home ~/.agent_crew] [--apply --accept-report-sha256 HASH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent_crew.role_mapping import (  # noqa: E402
    DEFAULT_SOURCE, EXPLICIT_SOURCE, LEGACY_SOURCE, default_role_mapping,
    effective_role_mapping,
)


def _read(path: Path) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError(f"{path}: state.json must be an object")
    if type(state.get("tester_provider_opt_in", False)) is not bool:
        raise ValueError(f"{path}: tester_provider_opt_in must be boolean")
    return raw, state


def plan_migration(crew_home: Path, *, test_timeout_s: float | None = None,
                   test_command: str | None = None) -> dict:
    """Inventory every state.json and bind its current bytes into the report."""
    if test_timeout_s is not None and (not math.isfinite(test_timeout_s)
                                       or test_timeout_s <= 0):
        raise ValueError("test_timeout_s must be a positive finite number")
    crew_home = Path(crew_home).expanduser().resolve()
    projects = []
    for path in sorted(crew_home.glob("*/state.json")):
        raw, state = _read(path)
        # Inventory the stored tester directly. Historical provider testers are
        # invalid for runtime dispatch, but the migration must still read them.
        old = "script"
        explicit = state.get("role_agents")
        source = (EXPLICIT_SOURCE if isinstance(explicit, dict) else
                  LEGACY_SOURCE if isinstance(state.get("roles"), list) else DEFAULT_SOURCE)
        if isinstance(explicit, dict) and isinstance(explicit.get("tester"), str):
            old = explicit["tester"]
        elif isinstance(state.get("roles"), list):
            for entry in state["roles"]:
                if (isinstance(entry, dict) and entry.get("role") == "tester"
                        and isinstance(entry.get("agent"), str)):
                    old = entry["agent"]
        opt_in = state.get("tester_provider_opt_in") is True
        new = "script"
        old_timeout = state.get("test_timeout_s")
        old_command = state.get("test_command")
        new_timeout = old_timeout if test_timeout_s is None else test_timeout_s
        new_command = old_command if test_command is None else test_command
        projects.append({"project": path.parent.name, "path": str(path),
                         "old_tester": old, "new_tester": new,
                         "old_test_timeout_s": old_timeout,
                         "new_test_timeout_s": new_timeout,
                         "old_test_command": old_command,
                         "new_test_command": new_command,
                         "action": "update" if (old != new or old_timeout != new_timeout
                                                or old_command != new_command) else "unchanged",
                         "provider_opt_in": opt_in, "mapping_source": source,
                         "state_sha256": hashlib.sha256(raw).hexdigest()})
    report = {"crew_home": str(crew_home), "projects": projects,
              "updates": sum(row["action"] == "update" for row in projects)}
    report["sha256"] = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report


def _replace_state(path: Path, state: dict) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(state, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def apply_migration(crew_home: Path, accepted_sha256: str, *,
                    test_timeout_s: float | None = None,
                    test_command: str | None = None) -> int:
    """Apply only an unchanged, already-reviewed report. Returns file count."""
    report = plan_migration(crew_home, test_timeout_s=test_timeout_s,
                            test_command=test_command)
    if not accepted_sha256 or report["sha256"] != accepted_sha256:
        raise ValueError("report hash mismatch; run the dry-run again and review every project")
    # Check the complete inventory before changing the first project.
    for row in report["projects"]:
        path = Path(row["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["state_sha256"]:
            raise ValueError(f"{path}: state changed since the dry-run")
    changed = 0
    for row in report["projects"]:
        if row["action"] != "update":
            continue
        path = Path(row["path"])
        raw, state = _read(path)
        if hashlib.sha256(raw).hexdigest() != row["state_sha256"]:
            raise ValueError(f"{path}: state changed since the dry-run")
        candidate = default_role_mapping()
        declared = state.get("role_agents")
        if isinstance(declared, dict):
            candidate.update(declared)
        elif isinstance(state.get("roles"), list):
            candidate.update({entry["role"]: entry["agent"]
                              for entry in state["roles"]
                              if isinstance(entry, dict)
                              and entry.get("role") in ("implementer", "reviewer")
                              and isinstance(entry.get("agent"), str)})
        candidate["tester"] = "script"
        mapping, _ = effective_role_mapping({"role_agents": candidate}, project=row["project"])
        if row["old_tester"] != row["new_tester"]:
            state["role_agents"] = {**mapping, "tester": "script"}
        if test_timeout_s is not None:
            state["test_timeout_s"] = test_timeout_s
        if test_command is not None:
            state["test_command"] = test_command
        _replace_state(path, state)
        changed += 1
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crew-home", type=Path, default=Path.home() / ".agent_crew")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--accept-report-sha256", default="")
    parser.add_argument("--test-timeout-s", type=float)
    parser.add_argument("--test-command")
    args = parser.parse_args(argv)
    if args.apply:
        if not args.accept_report_sha256:
            parser.error("--apply requires --accept-report-sha256 from a prior dry-run")
        count = apply_migration(args.crew_home, args.accept_report_sha256,
                                test_timeout_s=args.test_timeout_s,
                                test_command=args.test_command)
        print(json.dumps({"applied": count, "restart_required": count > 0}))
    else:
        if args.accept_report_sha256:
            parser.error("--accept-report-sha256 requires --apply")
        print(json.dumps(plan_migration(args.crew_home,
                                        test_timeout_s=args.test_timeout_s,
                                        test_command=args.test_command),
                         indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
