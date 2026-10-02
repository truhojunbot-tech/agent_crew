"""A built wheel starts the standalone CLI without fleet state (#310)."""

import importlib.util
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _run(args, *, cwd=None, env=None, timeout=90):
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=timeout)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout


def test_wheel_cli_help_with_empty_home_and_no_fleet_env(tmp_path):
    if (importlib.util.find_spec("build") is None or
            importlib.util.find_spec("wheel") is None):
        pytest.skip("build and wheel are required for the wheel smoke test")

    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(ROOT / "src", source / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    dist = tmp_path / "dist"
    # The test fixture sets HOME to tmp_path; keep the test host's public
    # build backend visible to this build subprocess.
    build_env = {**os.environ, "PYTHONUSERBASE": str(site.USER_BASE)}
    _run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
          "--outdir", str(dist)], cwd=source, env=build_env, timeout=120)
    wheels = list(dist.glob("agent_crew-*.whl"))
    assert len(wheels) == 1

    venv = tmp_path / "venv"
    # Public CLI dependencies are already installed on the test host. Reuse
    # them without a network install while keeping this wheel in the venv.
    _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)])
    python = venv / "bin" / "python"
    _run([str(python), "-m", "pip", "install", "--no-deps", "--no-index",
          str(wheels[0])], timeout=60)

    home = tmp_path / "empty-home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENT_CREW_", "XDG_", "PYTHON"))
           and key != "VIRTUAL_ENV"}
    env["HOME"] = str(home)
    env["PYTHONUSERBASE"] = str(site.USER_BASE)  # public test-host dependencies
    assert not any(key.startswith("AGENT_CREW_") for key in env)
    imported_from = _run([str(python), "-c", "import agent_crew; print(agent_crew.__file__)"],
                         env=env, timeout=20).strip()
    assert str(venv) in imported_from, imported_from
    output = _run([str(venv / "bin" / "crew"), "--help"], env=env, timeout=20)
    assert "Usage: crew" in output
