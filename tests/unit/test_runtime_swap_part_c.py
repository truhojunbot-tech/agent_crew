"""#940 follow-up: explicit T0 actor and optional CLI source update."""
import json

import pytest

from scripts import runtime_swap
from tests.unit.test_runtime_swap import _ready_go, _swap_fixture
from tests.unit.test_sev0_cea_s4e_wiring import write_key, write_snapshot


def test_go_without_actor_refuses_before_sigterm(tmp_path, monkeypatch):
    swap, _, _, _ = _ready_go(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENT_CREW_SWAP_ACTOR", raising=False)
    signals = []
    monkeypatch.setattr(swap.os, "kill", lambda *_args: signals.append(_args))
    with pytest.raises(SystemExit):
        swap.main(["demo", "a" * 40, "go", "--decision-id", "T0-4E-1"])
    assert signals == []


def test_explicit_actor_reaches_signed_verifier(tmp_path, monkeypatch):
    sha = "a" * 40
    snapshot = write_snapshot(tmp_path, principals=("owner:hojun",),
                              build_commits=(sha,), runtimes=("agent_crew",))
    key = write_key(tmp_path)
    env = {"AGENT_CREW_CEA_MODE": "shadow",
           "AGENT_CREW_CEA_SNAPSHOT_PATH": str(snapshot),
           "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE": str(key)}
    monkeypatch.delenv("AGENT_CREW_SWAP_ACTOR", raising=False)
    runtime_swap.verify_swap_decision("agent_crew", sha, "T0-4E-1", env, "hojun")
    with pytest.raises(RuntimeError, match="T0 decision refused"):
        runtime_swap.verify_swap_decision("agent_crew", sha, "T0-4E-1", env, "other")


def test_go_passes_cli_actor_to_boundary_verifier(tmp_path, monkeypatch):
    swap, _, _, _ = _ready_go(tmp_path, monkeypatch)
    seen = []
    def verify(*args):
        seen.append(args[-1])
        raise RuntimeError("stop at verifier")
    monkeypatch.setattr(swap, "verify_swap_decision", verify)
    with pytest.raises(RuntimeError, match="stop at verifier"):
        swap.main(["demo", "a" * 40, "go", "--decision-id", "T0-4E-1",
                   "--actor", "hojun"])
    assert seen == ["hojun"]


@pytest.mark.parametrize("bad_state", [True, False])
def test_post_succeeds_when_cli_repoint_cannot_verify_all_crews(
    tmp_path, monkeypatch, capsys, bad_state,
):
    swap, _, _ = _swap_fixture(tmp_path, monkeypatch)
    original_readlink = swap.os.readlink
    monkeypatch.setattr(swap.os, "readlink", lambda _path: str(tmp_path))
    assert swap.main(["demo", "a" * 40, "preflight"]) == 0
    monkeypatch.setattr(swap.os, "readlink", original_readlink)
    evidence = tmp_path / ".sev0-evidence" / "crew-swap-demo-aaaaaaa"
    (evidence / "counts.pre.json").write_text('[ ["completed", 1] ]')
    (evidence / "pending.pre.json").write_text('[]')
    root = tmp_path / ".agent_crew"
    old = tmp_path / "old-src"
    old.mkdir()
    (root / "cli_src").symlink_to(old)
    other = root / "other"
    other.mkdir()
    if bad_state:
        (other / "state.json").write_text("{bad json")
        expected = str(other / "state.json")
    else:
        (other / "state.json").write_text(json.dumps({"port": 9000}))
        original_api = swap.api
        def api(port, path, project):
            if port == 9000:
                raise OSError("unreachable")
            return original_api(port, path, project)
        monkeypatch.setattr(swap, "api", api)
        expected = "9000"
    assert swap.main(["demo", "a" * 40, "post"]) == 0
    assert (root / "cli_src").resolve() == old
    output = capsys.readouterr().out
    assert "POST_OK" in output
    assert "CLI source unchanged:" in output
    assert expected in output
