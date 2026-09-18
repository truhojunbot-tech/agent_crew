"""Server imports must not configure the application's root logger (#337)."""

import os
import pathlib
import subprocess
import sys


def test_importing_server_does_not_mutate_root_logging_configuration():
    script = """
import logging
root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)
before = (root.level, len(root.handlers))
import agent_crew.server
after = (root.level, len(root.handlers))
assert after == before, (before, after)
"""
    source_root = pathlib.Path(__file__).parents[2] / "src"
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
