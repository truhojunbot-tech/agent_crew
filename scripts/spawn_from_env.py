#!/usr/bin/env python3
"""Relaunch crew using captured NUL env plus explicit KEY=VAL CEA overrides."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_env(raw: bytes) -> dict[str, str]:
    env = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        name, value = item.split(b"=", 1)
        key = name.decode("ascii")
        if KEY.fullmatch(key):
            env[key] = value.decode("utf-8", "surrogateescape")
    return env


def spawn(env_file: Path, cea_file: Path, log: Path, port: int, source: Path) -> int:
    from runtime_swap import parse_env_file

    env = parse_env(env_file.read_bytes())
    for key in list(env):
        if key in {"_", "OLDPWD", "PWD", "SHLVL"} or key.startswith("AGENT_CREW_CEA_"):
            env.pop(key)
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = re.sub(r"(?:/[^: ]+)?/alfred/(?:projects/agent_crew|runtime/agent_crew-[0-9a-f]+)/src",
                                str(source), previous) if previous else str(source)
    if str(source) not in env["PYTHONPATH"].split(":"):
        env["PYTHONPATH"] = str(source) + (":" + env["PYTHONPATH"] if env["PYTHONPATH"] else "")
    env.update(parse_env_file(cea_file))
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent_crew.server:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "info"], env=env,
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    return process.pid


if __name__ == "__main__":
    if len(sys.argv) != 6:
        raise SystemExit("usage: spawn_from_env.py ENV_NUL CEA_ENV LOG PORT NEW_SRC")
    print(spawn(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
                int(sys.argv[4]), Path(sys.argv[5])))
