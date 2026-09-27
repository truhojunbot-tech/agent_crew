"""Offline owner setup contract: all mutations stay under a fake root."""
import json
import base64
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import subprocess
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/cea/owner_root_enforce_setup.sh"


def test_broker_import_and_schema_in_clean_environment():
    root = SCRIPT.parents[2]
    result = subprocess.run(
        ["env", "-i", "HOME=/nonexistent", "PYTHONNOUSERSITE=1",
         f"PYTHONPATH={root / 'src'}", "python3", "-c",
         "from agent_crew.cea import broker; from agent_crew.cea.schema import load_schema; "
         "assert load_schema()['$schema'].endswith('2020-12/schema')"],
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_missing_schema_fails_at_first_use(monkeypatch, tmp_path):
    from agent_crew.cea import schema
    schema.load_schema.cache_clear()
    monkeypatch.setattr(schema, "_schema_path", lambda: tmp_path / "missing.schema.json")
    with pytest.raises(FileNotFoundError):
        schema.load_schema()
    schema.load_schema.cache_clear()


def test_broker_refuses_startup_without_installed_schema(monkeypatch, tmp_path, capsys):
    from agent_crew.cea import broker, schema
    schema.load_schema.cache_clear()
    monkeypatch.setattr(schema, "_schema_path", lambda: tmp_path / "missing.schema.json")
    assert broker.main(["--sock-dir", str(tmp_path / "socket")]) == 3
    assert "receipt schema unavailable" in capsys.readouterr().err
    schema.load_schema.cache_clear()


def _run(root, *args, extra_env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--root-prefix", str(root)],
        text=True, capture_output=True,
        env={**os.environ, "AGENT_CREW_OWNER_SETUP_TESTING": "1", **(extra_env or {})},
    )


def _seed(root):
    tree = root / "opt/agent_crew-authz"
    tree.mkdir(parents=True)
    (tree / "SRC_COMMIT").write_text("old-build\n")
    (tree / "broker.env").write_text(
        "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE=/opt/agent_crew-authz/snapshot.key\n"
        "AGENT_CREW_CEA_SNAPSHOT_PATH=/home/truhojun/alfred/governance/cea_policy_snapshot.json\n"
        "AGENT_CREW_CEA_CALLER_TOKENS=/home/truhojun/.verify-private/tokens.json\n"
    )
    (tree / "snapshot.key").write_bytes(b"old-secret")
    db = root / "home/truhojun/.agent_crew/alfred/tasks.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"SQLite fixture")
    token = root / "home/truhojun/.verify-private/tokens.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"private":"token"}')
    token.chmod(0o600)
    pubkey = root / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub"
    pubkey.parent.mkdir(parents=True)
    signing_key = Ed25519PrivateKey.generate()
    pubkey.write_bytes(signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    raw = signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"generation": 2, "produced_at": datetime.now(timezone.utc).isoformat(), "decisions": []}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    (pubkey.parent / "cea_policy_snapshot.json").write_text(json.dumps({**body,
        "signature": {"ed25519": {"key_id": hashlib.sha256(raw).hexdigest()[:16],
                                  "value": base64.b64encode(signing_key.sign(canonical)).decode()}}}))
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


@pytest.mark.parametrize("blocker,expected", [
    ("src_old", "source update has no installed src and SRC_COMMIT"),
    ("src_new", "source update staging exists"),
    ("update_manifest", "source update has no installed src and SRC_COMMIT"),
    ("dirty_checkout", "source checkout is dirty"),
    ("wrong_commit", "differs from expected"),
    ("token_mode", "caller token mode 0644"),
])
def test_source_update_preview_reports_apply_blockers(tmp_path, blocker, expected):
    tree, token, _ = _seed(tmp_path)
    extra_env = {}
    if blocker == "src_old":
        (tree / "src.old").mkdir()
    elif blocker == "src_new":
        (tree / "src.new").mkdir()
    elif blocker == "update_manifest":
        manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
    elif blocker == "dirty_checkout":
        extra_env["AGENT_CREW_OWNER_SETUP_TEST_GIT_STATUS"] = " M src/agent_crew/cea/broker.py"
    elif blocker == "wrong_commit":
        extra_env["AGENT_CREW_OWNER_SETUP_TEST_GIT_COMMIT"] = "0" * 40
    elif blocker == "token_mode":
        token.chmod(0o644)
    before = (tree / "SRC_COMMIT").read_bytes()
    result = _run(tmp_path, "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json", extra_env=extra_env)
    assert result.returncode != 0
    assert "WOULD ABORT:" in result.stdout and expected in result.stdout
    assert "UNVERIFIED as non-root:" in result.stdout
    assert (tree / "SRC_COMMIT").read_bytes() == before
    assert not (tree / "src" / "agent_crew").exists()
    if blocker == "src_old":
        assert "source update has no installed src and SRC_COMMIT" in result.stdout
    if blocker == "update_manifest":
        assert "--undo --update-src" in result.stdout


@pytest.mark.parametrize("blocker,expected", [
    ("missing_pubkey", "missing snapshot public key"),
    ("unsafe_sudoers", "refusing symlink"),
    ("incomplete_manifest", "incomplete apply manifest"),
    ("outstanding_update", "source update has no installed src and SRC_COMMIT"),
])
def test_full_preview_reports_apply_blockers(tmp_path, blocker, expected):
    tree, _, sudoers = _seed(tmp_path)
    if blocker == "missing_pubkey":
        (tmp_path / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub").unlink()
    elif blocker == "unsafe_sudoers":
        sudoers.unlink()
        sudoers.symlink_to(tree / "SRC_COMMIT")
    elif blocker == "incomplete_manifest":
        manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"completed":false}')
    elif blocker == "outstanding_update":
        manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert f"WOULD ABORT: {expected}" in result.stdout
    assert not (tree / "caller-tokens.json").exists()


def test_preview_marks_restricted_manifest_unverified(tmp_path):
    _seed(tmp_path)
    manifest_dir = tmp_path / "var/lib/crew-authz/owner-root-enforce"
    manifest_dir.mkdir(parents=True)
    manifest_dir.chmod(0)
    try:
        result = _run(tmp_path, "--update-src", "--caller-tokens",
                      "/home/truhojun/.verify-private/tokens.json")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"UNVERIFIED as non-root: source update manifest in restricted directory: {manifest_dir}" in result.stdout
    finally:
        manifest_dir.chmod(0o700)


def test_private_read_only_caller_tokens_mode_is_allowed(tmp_path):
    _, token, _ = _seed(tmp_path)
    token.chmod(0o400)
    result = _run(tmp_path, "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WOULD ABORT: caller token mode" not in result.stdout


@pytest.mark.parametrize("extra_args", [(), ("--caller-tokens", "/home/truhojun/.verify-private/tokens.json")])
def test_dry_run_reports_unreadable_existing_broker_env(tmp_path, extra_args):
    tree, _, _ = _seed(tmp_path)
    config = tree / "broker.env"
    config.chmod(0o000)
    result = _run(tmp_path, "--dry-run", *extra_args)
    assert result.returncode == 0, result.stderr
    assert "existing broker.env unreadable as non-root; validated at apply" in result.stdout
    assert "PLAN $" in result.stdout
    assert "dry-run: no changes made" in result.stdout
    assert not (tmp_path / "var/lib/crew-authz").exists()


def test_apply_and_undo_restore_existing_files(tmp_path):
    tree, token, sudoers = _seed(tmp_path)
    old_mode = tree.stat().st_mode & 0o777
    result = _run(tmp_path, "--apply", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stderr
    assert (tree / "caller-tokens.json").read_text() == token.read_text()
    assert (tree / "caller-tokens.json").stat().st_mode & 0o777 == 0o400
    assert not (tree / "snapshot.key").exists()
    assert (tree / "snapshot.pub").read_bytes() == (
        tmp_path / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub").read_bytes()
    assert (tree / "snapshot.pub").stat().st_mode & 0o777 == 0o644
    assert "AGENT_CREW_CEA_SNAPSHOT_PUBKEY_FILE=/opt/agent_crew-authz/snapshot.pub" in (tree / "broker.env").read_text()
    assert "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE" not in (tree / "broker.env").read_text()
    assert "crew-authz-clients" in (tree / "broker.env").read_text()
    assert "libexec/crew-authz/broker-launch.sh" in sudoers.read_text()
    updater = tmp_path / "usr/local/libexec/crew-authz/broker-update"
    assert updater.is_file() and updater.stat().st_mode & 0o777 == 0o755
    assert "(root) NOPASSWD: /usr/local/libexec/crew-authz/broker-update" not in sudoers.read_text()
    assert not (tree / "owner-t0.pub").exists()
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


def test_optional_owner_t0_pubkey_is_installed_and_undone(tmp_path):
    tree, _, _ = _seed(tmp_path)
    source = tmp_path / "owner-t0.pub"
    source.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    applied = _run(tmp_path, "--apply", "--caller-tokens",
                   "/home/truhojun/.verify-private/tokens.json", "--owner-t0-pubkey", str(source))
    assert applied.returncode == 0, applied.stderr
    installed = tree / "owner-t0.pub"
    assert installed.read_bytes() == source.read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o644
    sudoers = tmp_path / "etc/sudoers.d/crew-authz-broker"
    assert "(root) NOPASSWD: /usr/local/libexec/crew-authz/broker-update [0-9a-f]*" in sudoers.read_text()
    undone = _run(tmp_path, "--undo")
    assert undone.returncode == 0, undone.stderr
    assert not installed.exists()


def test_source_update_retains_grant_for_installed_owner_key(tmp_path):
    tree, _, sudoers = _seed(tmp_path)
    source = tmp_path / "owner-t0.pub"
    source.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    installed = tree / "owner-t0.pub"
    installed.write_bytes(source.read_bytes())
    installed.chmod(0o644)
    result = _run(tmp_path, "--apply", "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed.read_bytes() == source.read_bytes()
    assert "(root) NOPASSWD: /usr/local/libexec/crew-authz/broker-update [0-9a-f]*" in sudoers.read_text()


def test_owner_reviewer_id_install_dry_run_and_undo(tmp_path):
    tree, _, sudoers = _seed(tmp_path)
    dry = _run(tmp_path, "--dry-run", "--update-src", "--caller-tokens",
               "/home/truhojun/.verify-private/tokens.json", "--owner-reviewer-id", "10932361")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "owner reviewer ID 10932361" in dry.stdout
    assert not (tree / "owner-reviewer.json").exists()
    applied = _run(tmp_path, "--apply", "--caller-tokens",
                   "/home/truhojun/.verify-private/tokens.json", "--owner-reviewer-id", "10932361")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    pinned = tree / "owner-reviewer.json"
    assert json.loads(pinned.read_text()) == {"user_id": 10932361}
    assert pinned.stat().st_mode & 0o777 == 0o644
    assert "(root) NOPASSWD" in sudoers.read_text()
    undone = _run(tmp_path, "--undo")
    assert undone.returncode == 0, undone.stdout + undone.stderr
    assert not pinned.exists()


def test_source_update_keeps_installed_reviewer_grant(tmp_path):
    tree, _, sudoers = _seed(tmp_path)
    pinned = tree / "owner-reviewer.json"
    pinned.write_text('{"user_id": 10932361}\n')
    pinned.chmod(0o644)
    result = _run(tmp_path, "--apply", "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(pinned.read_text()) == {"user_id": 10932361}
    assert "(root) NOPASSWD" in sudoers.read_text()


def test_apply_requires_ed25519_public_key(tmp_path):
    _seed(tmp_path)
    pubkey = tmp_path / "home/truhojun/alfred/governance/ssot-producer-ed25519.pub"
    pubkey.unlink()
    result = _run(tmp_path, "--apply", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode != 0
    assert "WOULD ABORT: missing snapshot public key" in result.stdout
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
    token.chmod(0o600)
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
    assert "WOULD ABORT: incomplete apply manifest" in retry.stdout
    assert "--undo" in retry.stdout
    assert manifest_path.exists()
    undo = _run(tmp_path, "--undo")
    assert undo.returncode == 0, undo.stderr
    assert not manifest_path.exists()


def test_update_src_is_repeatable_without_owner_cleanup(tmp_path):
    tree, _, _ = _seed(tmp_path)
    installed = tree / "src"
    installed.mkdir()
    (installed / "old.py").write_text("original")
    dry = _run(tmp_path, "--update-src", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert dry.returncode == 0, dry.stderr
    assert "dry-run: no changes made" in dry.stdout
    assert (installed / "old.py").read_text() == "original"
    assert not (tree / "src.old").exists()
    applied = _run(tmp_path, "--apply", "--update-src", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert applied.returncode == 0, applied.stderr
    assert "broker restart required" in applied.stdout
    assert not (tree / "src.old").exists()
    assert (installed / "agent_crew" / "cea" / "broker.py").is_file()
    assert (tree / "tests/cea_contract/receipt.schema.json").is_file()
    assert (tree / "venv/bin/python").is_file()
    mark = tree / "state/snapshot-hwm.json"
    assert json.loads(mark.read_text())["generation"] == 2
    assert (tree / "state").stat().st_mode & 0o777 == 0o700
    assert mark.stat().st_mode & 0o777 == 0o600
    assert (tree / "caller-tokens.json").is_file()
    assert (tmp_path / "usr/local/libexec/crew-authz/broker-launch.sh").is_file()
    assert (tree / "tests").stat().st_mode & 0o777 == 0o755
    assert (tree / "tests/cea_contract/receipt.schema.json").stat().st_mode & 0o777 == 0o644
    assert (tree / "caller-tokens.json").stat().st_mode & 0o777 == 0o400
    private = tree / "receipt-signing.key"
    public = tree / "receipt-signing.pub"
    assert private.stat().st_mode & 0o777 == 0o400
    assert public.stat().st_mode & 0o777 == 0o644
    pair_before = (private.read_bytes(), public.read_bytes())
    assert Ed25519PrivateKey.from_private_bytes(pair_before[0]).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw) == pair_before[1]
    assert (tree / "SRC_COMMIT").read_text().strip() == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SCRIPT.parent.parent.parent, text=True).strip()
    assert not (tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json").exists()
    repeat = _run(tmp_path, "--apply", "--update-src")
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert (private.read_bytes(), public.read_bytes()) == pair_before
    assert (installed / "agent_crew" / "cea" / "broker.py").is_file()
    assert not (tree / "src.old").exists()
    assert not (tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json").exists()


@pytest.mark.parametrize("with_manifest", [False, True])
def test_completed_legacy_update_rotates_all_backups(tmp_path, with_manifest):
    tree, _, _ = _seed(tmp_path)
    (tree / "src").mkdir()
    (tree / "src/installed.py").write_text("committed")
    launcher = tmp_path / "usr/local/libexec/crew-authz/broker-launch.sh"
    launcher.parent.mkdir(parents=True)
    backup_names = ("src.old", "tests.old", "venv.old", "state.old",
                    "caller-tokens.json.old", "receipt-signing.key.old",
                    "receipt-signing.pub.old")
    for name in backup_names:
        (tree / name).write_text("evidence")
    launcher.with_name("broker-launch.old").write_text("evidence")
    manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json"
    if with_manifest:
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"new_commit": "old-build"}))
    args = ("--update-src", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    dry = _run(tmp_path, *args)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert dry.stdout.count("WOULD ROTATE") >= 8
    assert all((tree / name).exists() for name in backup_names)
    applied = _run(tmp_path, "--apply", *args)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert not manifest.exists()
    for backup in [*(tree / name for name in backup_names), launcher.with_name("broker-launch.old")]:
        assert not backup.exists()
        rotated = list(backup.parent.glob(backup.name + ".committed-old-bui-*") )
        assert len(rotated) == 1
        assert rotated[0].read_text() == "evidence"


def test_incomplete_legacy_update_still_aborts(tmp_path):
    tree, _, _ = _seed(tmp_path)
    (tree / "src").mkdir()
    (tree / "src.old").mkdir()
    manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"new_commit": "different"}))
    result = _run(tmp_path, "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode != 0
    assert "WOULD ABORT: source update manifest does not match SRC_COMMIT" in result.stdout
    assert (tree / "src.old").exists() and manifest.exists()


def test_snapshot_default_and_broker_path_mismatch(tmp_path):
    tree, _, _ = _seed(tmp_path)
    good = _run(tmp_path, "--update-src", "--caller-tokens",
                "/home/truhojun/.verify-private/tokens.json")
    assert good.returncode == 0, good.stdout + good.stderr
    assert "cea_policy_snapshot.json" not in good.stdout or "WOULD ABORT" not in good.stdout
    config = tree / "broker.env"
    config.write_text(config.read_text().replace("cea_policy_snapshot.json", "control_policy_snapshot.json"))
    bad = _run(tmp_path, "--apply", "--update-src", "--caller-tokens",
               "/home/truhojun/.verify-private/tokens.json")
    assert bad.returncode != 0
    assert "HWM seed path" in bad.stdout and "differs from broker snapshot path" in bad.stdout


def test_update_src_accepts_existing_broker_env_without_snapshot_path(tmp_path):
    tree, _, _ = _seed(tmp_path)
    config = tree / "broker.env"
    config.write_text("\n".join(
        line for line in config.read_text().splitlines()
        if not line.startswith("AGENT_CREW_CEA_SNAPSHOT_PATH=")
    ) + "\n")
    args = ("--update-src", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    preview = _run(tmp_path, *args)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    applied = _run(tmp_path, "--apply", *args)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert (tree / "state/snapshot-hwm.json").is_file()


def test_update_src_refuses_symlinked_snapshot_state(tmp_path):
    tree, _, _ = _seed(tmp_path)
    state = tree / "state"
    state.mkdir()
    (state / "snapshot-hwm.json").symlink_to(tmp_path / "elsewhere.json")
    result = _run(tmp_path, "--apply", "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json")
    assert result.returncode != 0
    assert "snapshot state contains a symlink" in result.stderr
    assert not (tree / "state.new").exists()
    assert (state / "snapshot-hwm.json").is_symlink()


@pytest.mark.parametrize("stage", ["src", "schema", "venv", "state", "tokens", "receipt-key",
                                   "receipt-pubkey", "launcher", "updater", "sudoers", "snapshot-pubkey", "owner-reviewer", "selftest"])
def test_update_failure_restores_all_installed_paths(tmp_path, stage):
    tree, _, _ = _seed(tmp_path)
    launcher = tmp_path / "usr/local/libexec/crew-authz/broker-launch.sh"
    paths = {
        tree / "src/old.py": b"old source",
        tree / "tests/cea_contract/receipt.schema.json": b"old schema",
        tree / "venv/bin/python": b"old python",
        tree / "state/snapshot-hwm.json": b'{"generation": 1, "content_hash": "sha256:old"}',
        tree / "caller-tokens.json": b"old tokens",
        tree / "receipt-signing.key": Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()),
        launcher: b"old launcher",
        launcher.with_name("broker-update"): b"old updater",
        tmp_path / "etc/sudoers.d/crew-authz-broker": b"old sudoers",
        tree / "snapshot.pub": b"old snapshot pubkey",
    }
    old_key = Ed25519PrivateKey.from_private_bytes(paths[tree / "receipt-signing.key"])
    paths[tree / "receipt-signing.pub"] = old_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    for path, contents in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    (tree / "caller-tokens.json").chmod(0o400)
    (tree / "state").chmod(0o700)
    (tree / "state/snapshot-hwm.json").chmod(0o600)
    (tree / "receipt-signing.key").chmod(0o400)
    before = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in paths}
    reviewer_args = ("--owner-reviewer-id", "10932361") if stage == "owner-reviewer" else ()
    result = _run(tmp_path, "--apply", "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json", *reviewer_args,
                  extra_env={"AGENT_CREW_OWNER_SETUP_TEST_FAIL_STAGE": stage})
    assert result.returncode != 0, result.stdout
    assert "injected update failure" in result.stderr
    assert {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in paths} == before
    assert (tree / "SRC_COMMIT").read_text() == "old-build\n"
    for name in ("src.old", "src.new", "tests.old", "tests.new", "venv.old", "venv.new",
                 "state.old", "state.new",
                 "caller-tokens.json.old", "caller-tokens.new", "receipt-signing.key.old",
                 "receipt-signing.key.new", "receipt-signing.pub.old", "receipt-signing.pub.new"):
        assert not (tree / name).exists()
    assert not launcher.with_name("broker-launch.old").exists()
    assert not launcher.with_name("broker-launch.new").exists()
    assert not launcher.with_name("broker-update.old").exists()
    assert not launcher.with_name("broker-update.new").exists()
    assert not (tree / "owner-reviewer.json").exists()
    assert not (tree / "owner-reviewer.json.new").exists()


def test_update_src_undo_restores_every_saved_component(tmp_path):
    tree, _, _ = _seed(tmp_path)
    launcher = tmp_path / "usr/local/libexec/crew-authz/broker-launch.sh"
    entries = [(tree / "src", tree / "src.old", True),
               (tree / "tests", tree / "tests.old", True),
               (tree / "venv", tree / "venv.old", True),
               (tree / "state", tree / "state.old", True),
               (tree / "caller-tokens.json", tree / "caller-tokens.json.old", False),
               (tree / "receipt-signing.key", tree / "receipt-signing.key.old", False),
               (tree / "receipt-signing.pub", tree / "receipt-signing.pub.old", False),
               (launcher, launcher.with_name("broker-launch.old"), False),
               (launcher.with_name("broker-update"), launcher.with_name("broker-update.old"), False),
               (tmp_path / "etc/sudoers.d/crew-authz-broker",
                tmp_path / "etc/sudoers.d/crew-authz-broker.old", False),
               (tree / "snapshot.pub", tree / "snapshot.pub.old", False),
               (tree / "owner-t0.pub", tree / "owner-t0.pub.old", False),
               (tree / "owner-reviewer.json", tree / "owner-reviewer.json.old", False)]
    for live, backup, directory in entries:
        live.parent.mkdir(parents=True, exist_ok=True)
        if directory:
            live.mkdir()
            backup.mkdir()
            (live / "value").write_text("new")
            (backup / "value").write_text("old")
        else:
            live.write_text("new")
            backup.write_text("old")
    manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"had_src": True, "had_tests": True, "had_venv": True,
                                    "had_state": True,
                                    "had_tokens": True, "had_launcher": True,
                                    "had_updater": True, "had_sudoers": True,
                                    "had_snapshot_pub": True, "had_owner_t0_pub": True,
                                    "had_owner_reviewer": True,
                                    "had_receipt-signing.key": True,
                                    "had_receipt-signing.pub": True,
                                    "old_commit": "old-build\n"}))
    result = _run(tmp_path, "--undo", "--update-src")
    assert result.returncode == 0, result.stderr
    for live, backup, directory in entries:
        assert not backup.exists()
        assert ((live / "value").read_text() if directory else live.read_text()) == "old"
    assert (tree / "SRC_COMMIT").read_text() == "old-build\n"
    assert not manifest.exists()


@pytest.mark.parametrize("mode", ["--start", "--stop", "--restart", "--health"])
def test_lifecycle_modes_require_service_uid(tmp_path, mode):
    launcher = SCRIPT.with_name("broker-launch.sh")
    result = subprocess.run(["bash", str(launcher), mode], text=True, capture_output=True,
                            env={**os.environ, "AGENT_CREW_AUTHZ_CONFIG": str(tmp_path / "missing.env")})
    assert result.returncode == 3
    assert "lifecycle requires crew-authz" in result.stderr


def test_unreadable_broker_env_previews_print_plans(tmp_path):
    tree, _, _ = _seed(tmp_path)
    env_file = tree / "broker.env"
    env_file.chmod(0)
    dry = _run(tmp_path)
    assert dry.returncode == 0, dry.stderr
    assert "existing broker.env unreadable as non-root; validated at apply" in dry.stdout
    assert "PLAN $" in dry.stdout
    source_preview = _run(tmp_path, "--update-src", "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
    assert source_preview.returncode == 0, source_preview.stderr
    assert "existing broker.env unreadable as non-root; validated at apply" in source_preview.stdout
    assert "dry-run: no changes made" in source_preview.stdout


def test_main_undo_refuses_outstanding_source_update(tmp_path):
    tree, _, _ = _seed(tmp_path)
    applied = _run(tmp_path, "--apply", "--caller-tokens",
                   "/home/truhojun/.verify-private/tokens.json")
    assert applied.returncode == 0, applied.stderr
    source_manifest = tmp_path / "var/lib/crew-authz/owner-root-enforce/src-update.json"
    source_manifest.write_text('{}')
    assert source_manifest.is_file()
    blocked = _run(tmp_path, "--undo")
    assert blocked.returncode != 0
    assert "undo --update-src first" in blocked.stderr
    assert source_manifest.is_file()
    assert (tree / "src" / "agent_crew" / "cea" / "broker.py").is_file()
    source_manifest.unlink()
    main_undo = _run(tmp_path, "--undo")
    assert main_undo.returncode == 0, main_undo.stderr


def test_preinstall_selftest_failure_changes_no_fake_root_files(tmp_path):
    tree, token, _ = _seed(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    result = subprocess.run(
        ["bash", str(SCRIPT), "--apply", "--caller-tokens",
         "/home/truhojun/.verify-private/tokens.json", "--root-prefix", str(tmp_path)],
        text=True, capture_output=True,
        env={**os.environ, "AGENT_CREW_OWNER_SETUP_TESTING": "1",
             "AGENT_CREW_OWNER_SETUP_SELFTEST": "1"},
    )
    assert result.returncode != 0
    assert "pre-install broker self-test failed before changes" in result.stderr
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
    assert not (tree / "caller-tokens.json").exists()


def test_group_sandbox_authenticated_authorize(tmp_path):
    _, token, _ = _seed(tmp_path)
    token.write_text(json.dumps({"adapters": {"sandbox-token": {
        "principal": "cron:sandbox", "provenance": "cron"}}}))
    script = SCRIPT.with_name("broker_group_sandbox.py")
    result = subprocess.run(["python3", str(script), "--caller-tokens", str(token)],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["pass"] and evidence["authenticated"]
    assert evidence["socket_mode"] == "0660"
    assert evidence["peer_uid"] == os.geteuid()


def test_fake_apply_and_source_update_run_preinstall_proof(tmp_path):
    tree, token, _ = _seed(tmp_path)
    token.write_text(json.dumps({"adapters": {"sandbox-token": {
        "principal": "cron:sandbox", "provenance": "cron"}}}))
    env = {**os.environ, "AGENT_CREW_OWNER_SETUP_TESTING": "1",
           "AGENT_CREW_OWNER_SETUP_SELFTEST": "1"}
    apply = subprocess.run(
        ["bash", str(SCRIPT), "--apply", "--caller-tokens",
         "/home/truhojun/.verify-private/tokens.json", "--root-prefix", str(tmp_path)],
        text=True, capture_output=True, env=env, timeout=60)
    assert apply.returncode == 0, apply.stderr
    assert '"authenticated": true' in apply.stdout
    update = subprocess.run(
        ["bash", str(SCRIPT), "--apply", "--update-src", "--root-prefix", str(tmp_path)],
        text=True, capture_output=True, env=env, timeout=60)
    assert update.returncode == 0, update.stderr
    assert '"authenticated": true' in update.stdout
    assert (tree / "src" / "agent_crew" / "cea" / "broker.py").is_file()


def test_installed_selftest_argv_uses_installed_venv(tmp_path):
    tree, token, _ = _seed(tmp_path)
    result = _run(tmp_path, "--apply", "--update-src", "--caller-tokens",
                  "/home/truhojun/.verify-private/tokens.json",
                  extra_env={"AGENT_CREW_OWNER_SETUP_TEST_CAPTURE_SELFTEST_ARGV": "1"})
    assert result.returncode == 0, result.stderr
    expected = [str(tree / "venv/bin/python"), str(SCRIPT.with_name("broker_group_sandbox.py")),
                "--installed-root", str(tree)]
    assert "fake-root installed self-test argv: " + json.dumps(expected) in result.stdout


@pytest.mark.skipif(os.environ.get("RUN_OWNER_REHEARSAL") != "1",
                    reason="set RUN_OWNER_REHEARSAL=1 for the real pinned-venv rehearsal")
def test_owner_rehearsal_with_real_venv():
    token = Path(os.environ.get("RUN_OWNER_REHEARSAL_TOKENS",
                                "/home/truhojun/.agent_crew/alfred-cea/caller-tokens.json"))
    result = subprocess.run(["bash", str(SCRIPT), "--rehearse", "--update-src",
                             "--caller-tokens", str(token)], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"downgrade_reason": "BROKER_TREE_USER_WRITABLE"' in result.stdout
    assert "rehearsal legacy orphan pid " in result.stdout
    assert "broker stopped pid=" in result.stdout
    assert "replaced by managed broker" in result.stdout
    assert "broker started pid=" in result.stdout
    assert "rehearsal complete" in result.stdout


def test_broker_env_quoted_values_round_trip(tmp_path):
    tree, _, _ = _seed(tmp_path)
    owner_note = "owner's two words"
    (tree / "broker.env").write_text(
        "AGENT_CREW_CEA_CREDIT_CLASS='{" + '"gemini":"plan"' + "}'\n"
        "AGENT_CREW_CEA_NOTE='two words'\n"
        f"AGENT_CREW_CEA_OWNER_NOTE={shlex.quote(owner_note)}\n"
    )
    dry = _run(tmp_path, "--caller-tokens", "/home/truhojun/.verify-private/tokens.json")
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
