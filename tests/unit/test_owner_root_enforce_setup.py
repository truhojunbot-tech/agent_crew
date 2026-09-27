"""Offline owner setup contract: all mutations stay under a fake root."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


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
        "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE=/opt/agent_crew-authz/snapshot.key\n"
    )
    (tree / "snapshot.key").write_bytes(b"old-secret")
    db = root / "home/truhojun/.agent_crew/alfred/tasks.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"SQLite fixture")
    token = root / "home/truhojun/.verify-private/tokens.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"private":"token"}')
    pubkey = root / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub"
    pubkey.parent.mkdir(parents=True)
    pubkey.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
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
    assert (tree / "snapshot.pub").read_bytes() == (
        tmp_path / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub").read_bytes()
    assert (tree / "snapshot.pub").stat().st_mode & 0o777 == 0o644
    assert "AGENT_CREW_CEA_SNAPSHOT_PUBKEY_FILE=/opt/agent_crew-authz/snapshot.pub" in (tree / "broker.env").read_text()
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
    assert (tree / "snapshot.key").read_bytes() == b"old-secret"
    assert sudoers.read_text() == "old sudoers\n"
    assert not (tmp_path / "usr/local/libexec/crew-authz").exists()
    assert not (tmp_path / "usr/local/libexec").exists()
    assert not (tmp_path / "var/lib/crew-authz/owner-root-enforce").exists()


def test_public_key_source_can_be_overridden(tmp_path):
    tree, _, _ = _seed(tmp_path)
    alternate = tmp_path / "alternate.pub"
    alternate.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    result = _run(tmp_path, "--apply", "--snapshot-pubkey", str(alternate), "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stderr
    assert (tree / "snapshot.pub").read_bytes() == alternate.read_bytes()
    assert "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE" not in (tree / "broker.env").read_text()


def test_apply_requires_ed25519_public_key(tmp_path):
    _seed(tmp_path)
    pubkey = tmp_path / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub"
    pubkey.unlink()
    result = _run(tmp_path, "--apply", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode != 0
    assert "missing snapshot public key" in result.stderr
    assert not (tmp_path / "var/lib/crew-authz/owner-root-enforce").exists()


def test_old_hmac_copy_flag_is_rejected(tmp_path):
    _seed(tmp_path)
    result = _run(tmp_path, "--with-snapshot-key")
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_first_apply_with_no_backup_targets_can_be_undone(tmp_path):
    db = tmp_path / "home/truhojun/.agent_crew/alfred/tasks.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"SQLite fixture")
    token = tmp_path / "home/truhojun/.verify-private/tokens.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"private":"token"}')
    pubkey = tmp_path / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub"
    pubkey.parent.mkdir(parents=True)
    pubkey.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert not (tmp_path / "opt/agent_crew-authz").exists()
    assert not (tmp_path / "usr/local/libexec/crew-authz").exists()
    assert not (tmp_path / "etc/sudoers.d/crew-authz-broker").exists()
    apply = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert apply.returncode == 0, apply.stderr
    assert "-T /dev/null" in apply.stdout
    undo = _run(tmp_path, "--undo")
    assert undo.returncode == 0, undo.stderr
    assert db.read_bytes() == b"SQLite fixture"
    assert token.read_text() == '{"private":"token"}'
    assert not (tmp_path / "opt/agent_crew-authz").exists()
    assert not (tmp_path / "usr/local/libexec/crew-authz").exists()
    assert not (tmp_path / "etc/sudoers.d/crew-authz-broker").exists()


def test_incomplete_apply_requires_undo_before_retry(tmp_path):
    _seed(tmp_path)
    first = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert first.returncode == 0, first.stderr
    manifest_path = tmp_path / "var/lib/crew-authz/owner-root-enforce/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["completed"] = False
    manifest_path.write_text(json.dumps(manifest))
    retry = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert retry.returncode != 0
    assert "incomplete apply; undo first" in retry.stderr
    assert manifest_path.exists()
    undo = _run(tmp_path, "--undo")
    assert undo.returncode == 0, undo.stderr
    assert not manifest_path.exists()


def test_broker_env_quoted_values_round_trip(tmp_path):
    tree, _, _ = _seed(tmp_path)
    owner_note = "owner's two words"
    (tree / "broker.env").write_text(
        "AGENT_CREW_CEA_CREDIT_CLASS='{" + '"gemini":"plan"' + "}'\n"
        "AGENT_CREW_CEA_NOTE='two words'\n"
        f"AGENT_CREW_CEA_OWNER_NOTE={shlex.quote(owner_note)}\n"
    )
    dry = _run(tmp_path)
    assert dry.returncode == 0, dry.stderr
    assert "broker.env validation passed" in dry.stdout
    applied = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert applied.returncode == 0, applied.stderr
    config = (tree / "broker.env").read_text()
    assert "AGENT_CREW_CEA_CREDIT_CLASS='{" + '"gemini":"plan"' + "}'" in config
    assert "AGENT_CREW_CEA_NOTE='two words'" in config
    assert f"AGENT_CREW_CEA_OWNER_NOTE={shlex.quote(owner_note)}" in config


@pytest.mark.parametrize("line", [
    "AGENT_CREW_CEA_NOTE=two words",
    "AGENT_CREW_CEA_MEMORY_CMD=python3 /tmp/admission_inputs.py",
    'AGENT_CREW_CEA_CREDIT_CLASS={"gemini":"plan"}',
    "AGENT_CREW_CEA_MEMORY_CMD='python3 /tmp/admission_inputs.py'",
    "AGENT_CREW_CEA_NOTE=$(touch {fake_root}/sentinel)",
    "export AGENT_CREW_CEA_MODE=enforce",
])
def test_invalid_existing_broker_env_refused_before_apply(tmp_path, line):
    tree, _, _ = _seed(tmp_path)
    config = tree / "broker.env"
    line = line.replace("{fake_root}", str(tmp_path))
    config.write_text(line + "\n")
    dry = _run(tmp_path)
    assert dry.returncode != 0
    assert "broker.env" in dry.stderr
    assert config.read_text() == line + "\n"
    applied = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert applied.returncode != 0
    assert "broker.env" in applied.stderr
    assert config.read_text() == line + "\n"
    assert not (tmp_path / "var/lib/crew-authz/owner-root-enforce").exists()
    assert not (tmp_path / "sentinel").exists()
