"""Offline owner setup contract: all mutations stay under a fake root."""
import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/cea/owner_root_enforce_setup.sh"


def _run(root, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--root-prefix", str(root)],
        text=True, capture_output=True,
        env={**os.environ, "AGENT_CREW_OWNER_SETUP_TESTING": "1"},
    )


def _seed(root):
    tree = root / "opt/agent_crew-authz"
    tree.mkdir(parents=True)
    (tree / "SRC_COMMIT").write_text("old-build\n")
    (tree / "broker.env").write_text(
        "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE=/home/truhojun/.verify-private/ssot-producer.key\n"
    )
    db = root / "home/truhojun/.agent_crew/alfred/tasks.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"SQLite fixture")
    token = root / "home/truhojun/.verify-private/tokens.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"private":"token"}')
    (token.parent / "ssot-producer.key").write_bytes(b"snapshot-key")
    sudoers = root / "etc/sudoers.d/crew-authz-broker"
    sudoers.parent.mkdir(parents=True)
    sudoers.write_text("old sudoers\n")
    return tree, token, sudoers


def test_dry_run_changes_nothing_and_does_not_show_secrets(tmp_path):
    tree, token, sudoers = _seed(tmp_path)
    before = (tree / "SRC_COMMIT").read_bytes()
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "dry-run: no changes made" in result.stdout
    assert "setfacl -m u:crew-authz:--x" in result.stdout
    assert "setfacl -m u:crew-authz:rwx" in result.stdout
    assert "setfacl -m u:crew-authz:rw-" in result.stdout
    assert "setfacl -m u:crew-authz:rwx " + str(tmp_path / "home/truhojun/.verify-private") not in result.stdout
    assert "setfacl" not in result.stderr
    assert "private" not in result.stdout
    assert (tree / "SRC_COMMIT").read_bytes() == before
    assert sudoers.read_text() == "old sudoers\n"
    assert not (tmp_path / "var/lib/crew-authz").exists()


def test_apply_and_undo_restore_existing_files(tmp_path):
    tree, token, sudoers = _seed(tmp_path)
    old_mode = tree.stat().st_mode & 0o777
    result = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stderr
    assert (tree / "caller-tokens.json").read_text() == token.read_text()
    assert (tree / "caller-tokens.json").stat().st_mode & 0o777 == 0o640
    assert not (tree / "snapshot.key").exists()
    assert "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE" not in (tree / "broker.env").read_text()
    assert "crew-authz-clients" in (tree / "broker.env").read_text()
    assert "libexec/crew-authz/broker-launch.sh" in sudoers.read_text()
    assert (tmp_path / "var/lib/crew-authz/owner-root-enforce/manifest.json").exists()
    repeat = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert repeat.returncode == 0, repeat.stderr
    assert "already applied: no changes made" in repeat.stdout
    undo = _run(tmp_path, "--undo")
    assert undo.returncode == 0, undo.stderr
    assert (tree / "SRC_COMMIT").read_text() == "old-build\n"
    assert tree.stat().st_mode & 0o777 == old_mode
    assert not (tree / "caller-tokens.json").exists()
    assert sudoers.read_text() == "old sudoers\n"
    assert not (tmp_path / "usr/local/libexec/crew-authz").exists()
    assert not (tmp_path / "usr/local/libexec").exists()
    assert not (tmp_path / "var/lib/crew-authz/owner-root-enforce").exists()


def test_snapshot_key_requires_explicit_flag(tmp_path):
    tree, _, _ = _seed(tmp_path)
    result = _run(tmp_path, "--apply", "--with-snapshot-key", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stderr
    assert (tree / "snapshot.key").read_bytes() == b"snapshot-key"
    assert (tree / "snapshot.key").stat().st_mode & 0o777 == 0o640
    assert "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE=/opt/agent_crew-authz/snapshot.key" in (tree / "broker.env").read_text()
