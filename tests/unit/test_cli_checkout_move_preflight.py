"""The checkout move guard reports importers without exposing process secrets."""

import json

from scripts import cli_checkout_move_preflight as preflight


def _server(proc, pid, pythonpath, *, project="demo", port="8105", secret="private-key"):
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"python3\0-m\0uvicorn\0agent_crew.server:app\0")
    (entry / "environ").write_bytes(
        f"PYTHONPATH={pythonpath}\0AGENT_CREW_PROJECT={project}\0"
        f"AGENT_CREW_PORT={port}\0SECRET={secret}\0".encode()
    )


def test_importer_refuses_and_never_prints_secrets(tmp_path, capsys):
    checkout = tmp_path / "checkout"
    proc = tmp_path / "proc"
    proc.mkdir()
    _server(proc, 1234, str(checkout / "src"), secret="do-not-print-me")

    assert preflight.main([str(checkout)], proc_root=proc, state_root=tmp_path / "none") == 1
    output = capsys.readouterr().out
    assert "1234" in output and "8105" in output and "demo" in output
    assert str(checkout / "src") in output
    assert "swap it to a pinned runtime first" in output
    assert "do-not-print-me" not in output


def test_no_importers_passes(tmp_path, capsys):
    proc = tmp_path / "proc"
    proc.mkdir()
    assert preflight.main([str(tmp_path / "checkout")], proc_root=proc,
                          state_root=tmp_path / "none") == 0
    assert "no live crew server imports" in capsys.readouterr().out


def test_other_pinned_runtime_passes(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    _server(proc, 1234, str(tmp_path / "runtime" / "src"))
    assert preflight.main([str(tmp_path / "checkout")], proc_root=proc,
                          state_root=tmp_path / "none") == 0


def test_health_source_root_detects_importer(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "checkout"
    proc = tmp_path / "proc"
    proc.mkdir()
    state = tmp_path / "state" / "demo"
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"port": 8105}))
    monkeypatch.setattr(preflight, "health", lambda port: {
        "project": "demo", "build": {
            "pid": 4321, "source_root": str(checkout / "src" / "agent_crew")}})

    assert preflight.main([str(checkout)], proc_root=proc,
                          state_root=state.parent) == 1
    assert "4321" in capsys.readouterr().out
