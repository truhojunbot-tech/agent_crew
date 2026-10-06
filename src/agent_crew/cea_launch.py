"""Shared CEA environment source and broker client-group launch rules."""

from __future__ import annotations

import grp
import os
import re
import shlex
from pathlib import Path


ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def resolve_cea_file(project_dir: Path) -> Path:
    """Use the same explicit override or project-local file for every launch."""
    return Path(os.environ.get("AGENT_CREW_SWAP_CEA_ENV_FILE", str(project_dir / "cea.env")))


def parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not ENV_KEY.fullmatch(key) or not key.startswith("AGENT_CREW_CEA_"):
            raise ValueError(f"invalid env line {number}: expected AGENT_CREW_CEA_KEY=VALUE")
        values[key] = value
    if not values:
        raise ValueError("CEA env file is empty")
    return values


def client_command(env: dict[str, str], command: list[str]) -> list[str]:
    """Enter the broker socket's client group when the login lacks it."""
    sock = env.get("AGENT_CREW_CEA_BROKER_SOCKET")
    if not sock:
        return command
    group = grp.getgrgid(os.stat(Path(sock).parent).st_gid).gr_name
    return ["sg", group, "-c", shlex.join(command)]
