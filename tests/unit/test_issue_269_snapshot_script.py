"""#269 — the verification script must refuse, not degrade.

Review of PR #271, P2. `provider_sizes()` caught the ImportError for the cap
functions, printed one line to stderr, returned `[]`, and `main()` went on to
emit a complete-looking snapshot and exit 0. So a stale or missing install
could pass a post-deploy verification with **no cap data at all** — and the one
signal was a stderr line that a `2>/dev/null` redirect eats. The run that
produced this repo's own baseline artifact used exactly that redirect.

Reproduced before fixing, with a poisoned `PYTHONPATH`:

    exit code: 0
    provider_sizes entries: 0
    dispatchers entries: 8

⛔The distinction these tests pin is between a *refusal* and a *degradation*.
  Missing cap data is not a smaller answer to #269's criteria 3 and 4 — it is
  no answer, and an artifact that omits it while looking complete is worse than
  no artifact. Per-project schema gaps stay degradations, because those are
  recorded in the output as facts about the fleet.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "context_economics_snapshot.py")


@pytest.fixture(scope="module")
def snap():
    """The script loaded as a module — it lives in scripts/, not the package."""
    spec = importlib.util.spec_from_file_location("_ces_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _break_import(monkeypatch, snap, exc=ImportError("no agent_crew.server here")):
    """Make the cap-function import fail the way a stale install does."""
    real = __import__

    def fake(name, *a, **k):
        if name == "agent_crew.server" or name == "agent_crew":
            raise exc
        return real(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)


# ── 1. the refusal ────────────────────────────────────────────────────


def test_a_broken_import_raises_instead_of_returning_no_sizes(snap, monkeypatch):
    """★★The finding. `[]` was indistinguishable from "this fleet has no
    worktrees", which is a thing that can legitimately be true."""
    _break_import(monkeypatch, snap)
    with pytest.raises(snap.SnapshotUnavailable, match="cap functions"):
        snap.provider_sizes()


def test_the_refusal_says_how_to_fix_it(snap, monkeypatch):
    """An operator reading this at 3am needs the command, not a diagnosis."""
    _break_import(monkeypatch, snap)
    with pytest.raises(snap.SnapshotUnavailable, match=r"PYTHONPATH=src"):
        snap.cap_functions()


def test_a_foreign_agent_crew_is_refused_even_though_it_imports(snap, monkeypatch):
    """★★The dangerous half: the import SUCCEEDS against the wrong tree.

    A bare `python3` inside an agent worktree here resolves `agent_crew` to a
    stale editable install elsewhere on the box. That yields numbers, and the
    numbers look right, which is exactly why it cannot be a warning."""
    import agent_crew

    monkeypatch.setattr(agent_crew, "__file__", "/somewhere/else/agent_crew/__init__.py")
    with pytest.raises(snap.SnapshotUnavailable, match="not this checkout"):
        snap.cap_functions()


def test_the_checkout_it_expects_is_the_one_it_ships_in(snap):
    assert snap.expected_package_dir() == os.path.realpath(
        os.path.join(REPO, "src", "agent_crew"))


# ── 2. what main() does with it ───────────────────────────────────────


def test_main_exits_non_zero_and_prints_no_snapshot(snap, monkeypatch, capsys):
    """⛔Both halves matter. A non-zero exit that still printed a snapshot would
    leave a partial artifact on disk for anyone who redirected stdout — which
    is how every artifact in this repo has been produced."""
    _break_import(monkeypatch, snap)
    monkeypatch.setattr(sys, "argv", ["ces", "--json"])

    assert snap.main() != 0
    out = capsys.readouterr()
    assert out.out == "", f"a snapshot was printed anyway: {out.out[:200]}"
    assert "REFUSING TO REPORT" in out.err


def test_main_refuses_before_it_touches_the_fleet(snap, monkeypatch, capsys):
    """The refusal has to come first. Reading dispatchers and DBs before
    discovering there will be no cap data wastes the run and, worse, invites a
    future edit to "just report what we got"."""
    _break_import(monkeypatch, snap)
    monkeypatch.setattr(snap, "running_dispatchers",
                        lambda: pytest.fail("polled the fleet despite refusing"))
    monkeypatch.setattr(snap, "attribution",
                        lambda days: pytest.fail("read the task DBs despite refusing"))
    monkeypatch.setattr(sys, "argv", ["ces"])
    assert snap.main() != 0
    capsys.readouterr()


def test_a_healthy_run_still_produces_a_snapshot(snap, monkeypatch, capsys, tmp_path):
    """⛔The control. A refusal that also refuses the healthy path is a broken
    script, not a strict one."""
    monkeypatch.setattr(snap, "BASE", str(tmp_path))   # an empty, harmless fleet
    monkeypatch.setattr(sys, "argv", ["ces", "--json"])

    assert snap.main() == 0
    body = json.loads(capsys.readouterr().out)
    assert body["provider_sizes"] == []      # empty fleet, not a broken import
    assert body["measured_with"] == snap.expected_package_dir()


def test_the_snapshot_records_which_code_measured_it(snap, monkeypatch, capsys, tmp_path):
    """The #248 lesson applied to the output: an artifact that does not say
    which tree produced it cannot be trusted six weeks later."""
    monkeypatch.setattr(snap, "BASE", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["ces", "--json"])
    snap.main()
    assert "agent_crew" in json.loads(capsys.readouterr().out)["measured_with"]


# ── 3. end to end, the way it actually gets run ───────────────────────


def test_a_poisoned_pythonpath_fails_the_whole_process(tmp_path):
    """★★The reproduction from the review, as a subprocess — stdout redirected
    to a file and stderr discarded, which is how the baseline artifact in this
    PR was generated. Before the fix this wrote a complete-looking JSON file
    and exited 0."""
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "agent_crew.py").write_text('raise ImportError("stale install")\n')

    env = dict(os.environ, PYTHONPATH=str(stale))
    proc = subprocess.run([sys.executable, SCRIPT, "--json"], env=env,
                          capture_output=True, text=True, timeout=120)

    assert proc.returncode != 0, "a broken environment passed verification"
    assert proc.stdout.strip() == "", f"partial artifact written: {proc.stdout[:200]}"


def test_the_healthy_subprocess_still_emits_parseable_json(tmp_path):
    """⛔Control for the above, so the assertion cannot pass by the script being
    broken outright."""
    env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "src"),
               AGENT_CREW_BASE=str(tmp_path))
    proc = subprocess.run([sys.executable, SCRIPT, "--json"], env=env,
                          capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, proc.stderr[-500:]
    assert json.loads(proc.stdout)["provider_sizes"] == []
