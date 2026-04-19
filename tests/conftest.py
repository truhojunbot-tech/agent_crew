import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def task_queue(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture
def test_client(tmp_db):
    app = create_app(tmp_db)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def tmux_session():
    if not shutil.which("tmux"):
        pytest.skip("tmux not available")
    subprocess.run(["tmux", "new-session", "-d", "-s", "test_crew"], capture_output=True)
    yield "test_crew"
    subprocess.run(["tmux", "kill-session", "-t", "test_crew"], capture_output=True)


@pytest.fixture
def stub_agents(tmp_path):
    scripts = {}
    for agent in ["claude", "codex"]:
        script = tmp_path / f"{agent}_stub.sh"
        script.write_text("#!/bin/sh\necho stub agent running\n")
        script.chmod(0o755)
        scripts[agent] = str(script)
    return scripts
