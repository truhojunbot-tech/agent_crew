"""#1020 — an exported forge/context-pack flag must not reach a test body.

A pytest run started from a shell that inherited the live server env
(`AGENT_CREW_FORGE_PROVIDER=1`) built context packs with the real
ForgeProvider and called the live forge tail server with fixture repos.

The autouse fixture runs before every test body, so exporting the key
in-process cannot exercise it. Instead a child pytest is started with the
key in its *process* env, against an inner test that sits next to a
byte-for-byte copy of the repo `tests/conftest.py` — the same fixture that
guards the real suite. The inner test asserts the keys are gone and that a
pack builds without any network call (urlopen raises if touched), and that
a test's own ``monkeypatch.undo()`` does not bring the keys back.
"""

import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

REPO_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
SRC = Path(__file__).resolve().parents[2] / "src"
# Resolved at import, before the autouse fixture points HOME at tmp_path, so
# the child finds a user-site pytest install.
USER_BASE = site.getuserbase()

INNER_TEST = '''
import os
import urllib.request

import pytest

from agent_crew import context_pack as cp

LEAKED = ("AGENT_CREW_FORGE_PROVIDER", "AGENT_CREW_FORGE_URL",
          "AGENT_CREW_CONTEXT_PACK", "AGENT_CREW_CONTEXT_PACK_SHADOW")


def test_inherited_forge_and_pack_keys_never_reach_the_body(monkeypatch):
    for key in LEAKED:
        assert key not in os.environ, key
    assert not cp.forge_enabled()

    def no_network(*a, **kw):
        raise AssertionError("network call from a test body")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(cp.ForgeProvider, "__init__",
                        lambda *a, **kw: pytest.fail("ForgeProvider constructed"))
    pack = cp.build_pack_for_task(
        {"issue_title": "fix widget drop", "repo": "org/repo", "issue": 42,
         "issue_body": "## Acceptance criteria\\n- [ ] widget survives"},
        task_id="t-1", task_type="implement", role="implementer",
    )
    assert pack.telemetry()["forge_items"] == 0


def test_monkeypatch_undo_does_not_restore_inherited_keys(monkeypatch):
    # test_issue_258's _shadow_dispatch calls monkeypatch.undo() mid-test.
    monkeypatch.setenv("UNRELATED_1020", "x")
    monkeypatch.undo()
    for key in LEAKED:
        assert key not in os.environ, key
'''


def _run_inner(tmp_path, *, with_conftest):
    if with_conftest:
        shutil.copy(REPO_CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(INNER_TEST)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({
        "AGENT_CREW_FORGE_PROVIDER": "1",
        "AGENT_CREW_FORGE_URL": "http://127.0.0.1:9",
        "AGENT_CREW_CONTEXT_PACK": "1",
        "AGENT_CREW_CONTEXT_PACK_SHADOW": "1",
        "PYTHONUSERBASE": USER_BASE,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")])),
    })
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_repo_conftest_scrubs_exported_forge_and_pack_keys(tmp_path):
    proc = _run_inner(tmp_path, with_conftest=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


def test_without_the_scrub_the_inner_guard_fails(tmp_path):
    """The inner guard is not vacuous: without the conftest the key leaks."""
    proc = _run_inner(tmp_path, with_conftest=False)
    assert proc.returncode != 0, proc.stdout
    assert "AGENT_CREW_FORGE_PROVIDER" in proc.stdout
