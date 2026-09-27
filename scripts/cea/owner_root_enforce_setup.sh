#!/usr/bin/env bash
# Owner-only, offline phase-2 broker installation. No live broker is restarted.
# See docs/cea_broker_oop_deploy.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export CREW_AUTHZ_LAUNCHER_SOURCE="$SCRIPT_DIR/broker-launch.sh"
export CREW_AUTHZ_SOURCE_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
export CREW_AUTHZ_SOURCE_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
python3 - "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys

p = argparse.ArgumentParser(description="Offline, reversible root broker setup")
mode = p.add_mutually_exclusive_group()
mode.add_argument("--dry-run", action="store_true", help="print plan only (default)")
mode.add_argument("--apply", action="store_true")
mode.add_argument("--undo", action="store_true")
p.add_argument("--caller-tokens", type=Path, help="private adapter token table to copy")
p.add_argument("--snapshot-pubkey", type=Path, help="Ed25519 public key PEM source")
p.add_argument("--snapshot-path", type=Path, help="canonical signed snapshot to seed the rollback guard")
p.add_argument("--root-prefix", type=Path, help=argparse.SUPPRESS)
p.add_argument("--update-src", action="store_true", help="update broker source from this clean checkout")
args = p.parse_args()
if args.root_prefix and os.environ.get("AGENT_CREW_OWNER_SETUP_TESTING") != "1":
    p.error("--root-prefix is reserved for isolated tests")
fake = args.root_prefix is not None
if not fake and (args.apply or args.undo) and os.geteuid() != 0:
    p.error("--apply and --undo require root")
root = args.root_prefix.resolve() if fake else Path("/")
def path(name):
    return root / name.lstrip("/")
def run(*cmd, capture=False):
    return subprocess.run(cmd, check=True, text=True, capture_output=capture)
def say(message):
    print(message, flush=True)
def read_broker_env(config):
    if not config.exists():
        return ""
    try:
        return config.read_text()
    except PermissionError as exc:
        if not args.apply and not args.undo:
            return None
        raise SystemExit(
            f"broker.env is not readable: {config}; run --dry-run as root to validate and print the plan"
        ) from exc

tree = path("/opt/agent_crew-authz")
launcher = path("/usr/local/libexec/crew-authz/broker-launch.sh")
sudoers = path("/etc/sudoers.d/crew-authz-broker")
db = path("/home/truhojun/.agent_crew/alfred/tasks.db")
crew_dir = path("/home/truhojun/.agent_crew")
alfred_dir = db.parent
manifest_dir = path("/var/lib/crew-authz/owner-root-enforce")
manifest_file = manifest_dir / "manifest.json"
archive = manifest_dir / "before.tar"
acl_file = manifest_dir / "before.acl"
created_parents = []
token_source = args.caller_tokens
if token_source and fake and token_source.is_absolute():
    token_source = path(str(token_source))
if token_source is None and not (args.undo or args.update_src):
    env_file = tree / "broker.env"
    if env_file.exists():
        for line in (read_broker_env(env_file) or "").splitlines():
            match = re.fullmatch(r"AGENT_CREW_CEA_CALLER_TOKENS=['\"]?([^'\"]+)['\"]?", line.strip())
            if match:
                candidate = Path(match.group(1))
                if candidate != tree / "caller-tokens.json":
                    token_source = path(str(candidate)) if fake else candidate
                break
snapshot_source = args.snapshot_pubkey or path("/home/truhojun/alfred/governance/ssot-producer-ed25519.pub")
if fake and args.snapshot_pubkey and snapshot_source.is_absolute() and not snapshot_source.is_relative_to(root):
    snapshot_source = path(str(snapshot_source))
snapshot_path = args.snapshot_path or path("/home/truhojun/alfred/governance/control_policy_snapshot.json")
if fake and args.snapshot_path and snapshot_path.is_absolute() and not snapshot_path.is_relative_to(root):
    snapshot_path = path(str(snapshot_path))
source_launcher = Path(os.environ["CREW_AUTHZ_LAUNCHER_SOURCE"])
source_commit = os.environ["CREW_AUTHZ_SOURCE_COMMIT"]
source_root = Path(os.environ["CREW_AUTHZ_SOURCE_ROOT"])
schema_source = source_root / "tests/cea_contract/receipt.schema.json"
requirements = source_root / "scripts/cea/broker-requirements.txt"
venv = tree / "venv"
schema_installed = tree / "tests/cea_contract/receipt.schema.json"
SCHEMA_BLOB = "41e7ebf271830790f6aae80a413e51edf7805fcd"
if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
    p.error("invalid source commit")

def preinstall_selftest(tokens):
    if fake and os.environ.get("AGENT_CREW_OWNER_SETUP_SELFTEST") != "1":
        say("fake-root: skipped cross-uid pre-install self-test")
        return
    command = [sys.executable, str(source_root / "scripts/cea/broker_group_sandbox.py"),
               "--source-root", str(source_root), "--caller-tokens", str(tokens)]
    if not fake:
        command.append("--cross-uid")
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise SystemExit(f"pre-install broker self-test failed before changes: {result.stderr.strip()}")
    evidence = json.loads(result.stdout)
    if not evidence.get("pass") or not evidence.get("authenticated"):
        raise SystemExit(f"pre-install broker self-test failed before changes: {evidence}")
    say("pre-install broker self-test JSON: " + json.dumps(evidence, sort_keys=True))

def normalize(root_dir):
    """Copy source modes never confer write access on the protected broker tree."""
    for parent, dirs, files in os.walk(root_dir):
        for name in dirs + files:
            item = Path(parent) / name
            if item.is_symlink():
                raise RuntimeError(f"symlink in staged broker tree: {item}")
            os.chmod(item, 0o755 if item.is_dir() else 0o644)
            if not fake:
                os.chown(item, 0, 0)
    os.chmod(root_dir, 0o755)
    if not fake:
        os.chown(root_dir, 0, 0)

def build_venv(destination):
    if fake:
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/python").write_text("fake pinned broker python\n")
        say("fake-root: pinned venv build represented without installing packages")
        return
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
           "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": "/dev/null"}
    subprocess.run(["/usr/bin/python3", "-m", "venv", str(destination)], check=True, env=env)
    subprocess.run([str(destination / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-cache-dir", "-r", str(requirements)], check=True, env=env)
    # The interpreter is a system symlink; normalize every file around it.
    for parent, dirs, files in os.walk(destination):
        for name in dirs + files:
            item = Path(parent) / name
            if item.is_symlink():
                continue
            os.chown(item, 0, 0)
            os.chmod(item, 0o755 if item.is_dir() or os.access(item, os.X_OK) else 0o644)
    os.chown(destination, 0, 0)
    os.chmod(destination, 0o755)

def installed_selftest():
    if fake and os.environ.get("AGENT_CREW_OWNER_SETUP_TEST_FAIL_STAGE") == "selftest":
        raise RuntimeError("injected update failure: selftest")
    if fake:
        say("fake-root: skipped cross-uid installed-path self-test")
        return
    command = [sys.executable, str(source_root / "scripts/cea/broker_group_sandbox.py"),
               "--installed-root", str(tree)]
    if not fake:
        command.append("--cross-uid")
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"installed broker self-test failed: {result.stderr.strip()}")
    evidence = json.loads(result.stdout)
    if (not evidence.get("pass") or not evidence.get("authenticated") or
            not evidence.get("schema_valid") or not evidence.get("signed_receipt_valid") or
            not evidence.get("tampered_receipt_rejected") or
            not evidence.get("snapshot_rollback_refused") or evidence.get("downgrade_reason") is not None):
        raise RuntimeError(f"installed broker self-test failed: {evidence}")
    say("installed broker self-test JSON: " + json.dumps(evidence, sort_keys=True))

def apply_preconditions(source_update=False):
    """Return failures visible before root changes, plus checks requiring root."""
    failures, unverified = [], []
    update_manifest = manifest_dir / "src-update.json"
    previous = tree / "src.old"
    staging = tree / "src.new"
    undo_update = f"sudo {shlex.quote(str(source_root / 'scripts/cea/owner_root_enforce_setup.sh'))} --undo --update-src"
    manifest_visible = not manifest_dir.exists() or os.access(manifest_dir, os.X_OK)
    tree_visible = not tree.exists() or os.access(tree, os.X_OK)
    if not manifest_visible:
        unverified.append(f"source update manifest in restricted directory: {manifest_dir}")
    elif update_manifest.exists():
        failures.append(f"source update manifest exists: {update_manifest}; recover with: {undo_update}")
    if not tree_visible:
        unverified.append(f"source backup and staging in restricted directory: {tree}")
    else:
        if previous.exists():
            recovery = undo_update if manifest_visible and update_manifest.exists() else (
                f"mv -- {shlex.quote(str(previous))} {shlex.quote(str(previous) + '.saved')}")
            failures.append(f"source update backup exists: {previous}; recover with: {recovery}")
        if staging.exists():
            failures.append(f"source update staging exists: {staging}; inspect and remove it before retrying")
    if not (source_root / "src").is_dir():
        failures.append(f"checkout src missing: {source_root / 'src'}")
    elif any(item.is_symlink() for item in (source_root / "src").rglob("*")):
        failures.append(f"symlink in checkout src: {source_root / 'src'}")
    if not schema_source.is_file() or subprocess.run(["git", "hash-object", str(schema_source)],
            capture_output=True, text=True).stdout.strip() != SCHEMA_BLOB:
        failures.append(f"frozen receipt schema missing or wrong blob: {schema_source}; recover with: git checkout -- tests/cea_contract/receipt.schema.json")
    if not requirements.is_file():
        failures.append(f"pinned broker requirements missing: {requirements}; recover by restoring checkout")
    if source_update:
        if not snapshot_path.is_file() or snapshot_path.is_symlink():
            failures.append(f"signed snapshot missing: {snapshot_path}; recover by publishing a signed snapshot")
        elif not os.access(snapshot_path, os.R_OK) or not os.access(snapshot_source, os.R_OK):
            unverified.append(f"signed snapshot or public key unreadable as non-root: {snapshot_path}")
        else:
            try:
                sys.path.insert(0, str(source_root / "src"))
                from agent_crew.cea.input_providers.snapshot import CanonicalPolicySnapshotReader, ed25519_verifier
                from agent_crew.cea.providers import SignatureStatus
                current = CanonicalPolicySnapshotReader(str(snapshot_path),
                    verifier=ed25519_verifier(snapshot_source.read_bytes())).current()
                if current.signature is not SignatureStatus.VALID or not current.available:
                    failures.append(f"current snapshot is not signed and fresh: {snapshot_path}; recover by publishing a signed snapshot")
            except Exception as exc:
                failures.append(f"snapshot verification failed: {exc}; recover by publishing a signed snapshot")
        if not venv.is_dir():
            unverified.append(f"pinned venv absent at {venv}; --update-src will build it")
        elif not (venv / "bin/python").exists():
            failures.append(f"pinned venv has no python: {venv}; recover with: --undo --update-src")
        elif not fake and os.access(venv / "bin/python", os.X_OK):
            check = subprocess.run([str(venv / "bin/python"), "-c",
                "import jsonschema,cryptography,httpx; assert hasattr(jsonschema,'Draft202012Validator')"],
                env={"HOME": "/nonexistent", "PYTHONNOUSERSITE": "1"}, capture_output=True)
            if check.returncode:
                unverified.append(f"pinned venv dependencies at {venv} will be refreshed")
    if fake:
        actual_commit = os.environ.get("AGENT_CREW_OWNER_SETUP_TEST_GIT_COMMIT", source_commit)
        dirty = os.environ.get("AGENT_CREW_OWNER_SETUP_TEST_GIT_STATUS", "")
    else:
        actual_commit = run("git", "-C", str(source_root), "rev-parse", "HEAD", capture=True).stdout.strip()
        dirty = run("git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=all",
                    capture=True).stdout
        # The dispatcher supplies this local instruction file outside Git.
        dirty = "\n".join(line for line in dirty.splitlines() if line != "?? AGENTS.md")
    if actual_commit != source_commit:
        failures.append(f"checkout commit {actual_commit} differs from expected {source_commit}")
    if dirty.strip():
        failures.append(f"source checkout is dirty: {source_root}")
    if source_update:
        if not tree.is_dir() or tree.is_symlink():
            failures.append(f"broker tree missing or symlink: {tree}")
        tokens = token_source or tree / "caller-tokens.json"
    else:
        if not db.is_file() or db.is_symlink():
            failures.append(f"tasks DB missing or symlink: {db}")
        if not source_launcher.is_file() or source_launcher.is_symlink():
            failures.append(f"launcher source missing or symlink: {source_launcher}")
        if any(item.is_symlink() for item in (tree, launcher.parent, crew_dir, alfred_dir)):
            failures.append("unsafe symlink in broker or queue path")
        tokens = token_source
        if manifest_visible and manifest_file.exists():
            try:
                old = json.loads(manifest_file.read_text())
                if not (old.get("completed") and launcher.is_file() and sudoers.is_file()):
                    failures.append(f"incomplete apply manifest: {manifest_file}; recover with: "
                                    f"sudo {shlex.quote(str(source_root / 'scripts/cea/owner_root_enforce_setup.sh'))} --undo")
            except PermissionError:
                unverified.append(f"apply manifest contents: {manifest_file}")
            except (ValueError, OSError) as exc:
                failures.append(f"invalid apply manifest: {manifest_file}: {exc}")
        if snapshot_source.parent.exists() and not os.access(snapshot_source.parent, os.X_OK):
            unverified.append(f"snapshot public key in restricted directory: {snapshot_source.parent}")
        elif not snapshot_source.is_file() or snapshot_source.is_symlink():
            failures.append(f"missing snapshot public key: {snapshot_source}")
        elif os.access(snapshot_source, os.R_OK):
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                if not isinstance(serialization.load_pem_public_key(snapshot_source.read_bytes()), Ed25519PublicKey):
                    failures.append(f"invalid Ed25519 public key: {snapshot_source}")
            except (ImportError, ValueError) as exc:
                failures.append(f"invalid Ed25519 public key {snapshot_source}: {exc}")
        else:
            unverified.append(f"snapshot public key contents: {snapshot_source}")
        if sudoers.is_symlink() or sudoers.parent.is_symlink() or (sudoers.exists() and not sudoers.is_file()):
            failures.append(f"unsafe sudoers path: {sudoers}")
    if tokens is None:
        if existing_content is None and not source_update:
            unverified.append(f"caller token source in unreadable broker.env: {config}")
        else:
            failures.append("caller token source missing; recover with: --caller-tokens /path/to/private/tokens.json")
    elif tokens.parent.exists() and not os.access(tokens.parent, os.X_OK):
        unverified.append(f"caller token source in restricted directory: {tokens.parent}")
    elif not tokens.is_file() or tokens.is_symlink():
        failures.append(f"caller token source missing or symlink: {tokens}; recover with: --caller-tokens /path/to/private/tokens.json")
    else:
        mode = stat.S_IMODE(tokens.stat().st_mode)
        if mode not in (0o400, 0o440, 0o600, 0o640):
            failures.append(f"caller token mode {mode:04o} at {tokens}; recover with: chmod 600 {shlex.quote(str(tokens))}")
        if not os.access(tokens, os.R_OK):
            unverified.append(f"caller token contents: {tokens}")
    if os.geteuid() != 0:
        unverified.extend(["ACL, group and user changes", "visudo validation and sudoers install",
                           "cross-UID broker self-test and launcher --check"])
    return failures, unverified

def report_preconditions(source_update=False):
    failures, unverified = apply_preconditions(source_update)
    for check in unverified:
        say(f"UNVERIFIED as non-root: {check}")
    for reason in failures:
        say(f"WOULD ABORT: {reason}")
    if failures:
        raise SystemExit(1)

if args.update_src:
    if not args.apply and not args.undo and (tree / "broker.env").exists() and not os.access(tree / "broker.env", os.R_OK):
        say("existing broker.env unreadable as non-root; validated at apply")
    source = source_root / "src"
    installed = tree / "src"
    previous = tree / "src.old"
    staging = tree / "src.new"
    marker = tree / "SRC_COMMIT"
    update_manifest = manifest_dir / "src-update.json"
    if args.undo:
        if not update_manifest.is_file():
            raise SystemExit(f"no source update manifest: {update_manifest}")
        saved = json.loads(update_manifest.read_text())
        if installed.exists():
            shutil.rmtree(installed)
        if staging.exists():
            shutil.rmtree(staging)
        if saved["had_src"]:
            if not previous.is_dir():
                raise SystemExit(f"source backup missing: {previous}")
            os.replace(previous, installed)
        for name in ("tests", "venv", "state"):
            live, backup = tree / name, tree / (name + ".old")
            if live.exists():
                shutil.rmtree(live)
            if saved.get("had_" + name) and backup.exists():
                os.replace(backup, live)
        for name in ("caller-tokens.json", "receipt-signing.key", "receipt-signing.pub"):
            live, backup = tree / name, tree / (name + ".old")
            live.unlink(missing_ok=True)
            had = "had_tokens" if name == "caller-tokens.json" else "had_" + name
            if saved.get(had) and backup.exists():
                os.replace(backup, live)
        old_launcher = launcher.with_name("broker-launch.old")
        if saved.get("had_launcher") and old_launcher.exists():
            os.replace(old_launcher, launcher)
        elif not saved.get("had_launcher"):
            launcher.unlink(missing_ok=True)
        if saved["old_commit"] is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(saved["old_commit"])
            os.chmod(marker, 0o644)
            if not fake:
                run("chown", "root:root", str(marker))
        update_manifest.unlink()
        say("source update undo complete")
        sys.exit(0)
    say(f"stage clean checkout {source_root} ({source_commit}) into {staging}")
    say(f"backup {installed} to {previous}; write {marker}; record {update_manifest}")
    report_preconditions(source_update=True)
    if not args.apply:
        say("dry-run: no changes made")
        sys.exit(0)
    if not tree.is_dir() or tree.is_symlink() or not source.is_dir():
        raise SystemExit("broker tree or checkout src missing")
    if previous.exists() or staging.exists() or update_manifest.exists():
        raise SystemExit("source update backup or manifest exists; undo first")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise SystemExit(f"symlink in source tree: {item}")
    source_tokens = token_source or tree / "caller-tokens.json"
    if (not fake or os.environ.get("AGENT_CREW_OWNER_SETUP_SELFTEST") == "1") and not source_tokens.is_file():
        raise SystemExit(f"caller tokens missing for source update self-test: {source_tokens}")
    if fake and os.environ.get("AGENT_CREW_OWNER_SETUP_SELFTEST") == "1":
        preinstall_selftest(source_tokens)
    # All expensive and fallible preparation happens before any installed path
    # is changed. The installed-path test follows the atomic swaps below; a
    # failure restores every previous path before this command returns.
    schema_stage = tree / "tests.new"
    venv_stage = tree / "venv.new"
    token_stage = tree / "caller-tokens.new"
    private_stage = tree / "receipt-signing.key.new"
    public_stage = tree / "receipt-signing.pub.new"
    state_stage = tree / "state.new"
    launcher_stage = launcher.with_name("broker-launch.new")
    staged_paths = (staging, schema_stage, venv_stage, token_stage, private_stage,
                    public_stage, state_stage, launcher_stage)
    if any(item.exists() for item in staged_paths):
        raise SystemExit("staged update files exist; inspect and remove before retrying")
    try:
        shutil.copytree(source, staging)
        normalize(staging)
        (schema_stage / "cea_contract").mkdir(parents=True)
        shutil.copyfile(schema_source, schema_stage / "cea_contract/receipt.schema.json")
        normalize(schema_stage)
        build_venv(venv_stage)
        if not fake:
            import pwd
            authz = pwd.getpwnam("crew-authz")
        sys.path.insert(0, str(staging))
        from agent_crew.cea.input_providers.snapshot import (
            CanonicalPolicySnapshotReader, check_high_water_mark, ed25519_verifier)
        from agent_crew.cea.providers import SignatureStatus
        verifier = ed25519_verifier(snapshot_source.read_bytes())
        current = CanonicalPolicySnapshotReader(str(snapshot_path), verifier=verifier).current()
        if current.signature is not SignatureStatus.VALID or not current.available:
            raise RuntimeError(f"current snapshot is not signed and fresh: {snapshot_path}")
        if (tree / "state").exists():
            if (tree / "state").is_symlink() or any(
                    item.is_symlink() for item in (tree / "state").rglob("*")):
                raise RuntimeError("snapshot state contains a symlink")
            shutil.copytree(tree / "state", state_stage, symlinks=True)
        else:
            state_stage.mkdir()
        os.chmod(state_stage, 0o700)
        if not fake:
            os.chown(state_stage, authz.pw_uid, authz.pw_gid)
        if check_high_water_mark(str(state_stage / "snapshot-hwm.json"),
                                 current.generation, current.hash, bootstrap=True):
            raise RuntimeError("current signed snapshot conflicts with existing high-water mark")
        os.chmod(state_stage / "snapshot-hwm.json", 0o600)
        if not fake:
            os.chown(state_stage / "snapshot-hwm.json", authz.pw_uid, authz.pw_gid)
        shutil.copyfile(source_tokens, token_stage)
        os.chmod(token_stage, 0o400)
        if not fake:
            import pwd
            authz = pwd.getpwnam("crew-authz")
            os.chown(token_stage, authz.pw_uid, authz.pw_gid)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        old_private, old_public = tree / "receipt-signing.key", tree / "receipt-signing.pub"
        if old_private.exists() != old_public.exists():
            raise RuntimeError("incomplete receipt signing keypair; recover the missing key")
        if old_private.exists():
            private_bytes, public_bytes = old_private.read_bytes(), old_public.read_bytes()
            signing_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
            if signing_key.public_key().public_bytes(serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw) != public_bytes:
                raise RuntimeError("receipt signing keypair mismatch")
        else:
            signing_key = Ed25519PrivateKey.generate()
            private_bytes = signing_key.private_bytes(serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw, serialization.NoEncryption())
            public_bytes = signing_key.public_key().public_bytes(serialization.Encoding.Raw,
                serialization.PublicFormat.Raw)
        private_stage.write_bytes(private_bytes)
        public_stage.write_bytes(public_bytes)
        os.chmod(private_stage, 0o400)
        os.chmod(public_stage, 0o644)
        if not fake:
            os.chown(private_stage, authz.pw_uid, authz.pw_gid)
            os.chown(public_stage, 0, 0)
        launcher_stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_launcher, launcher_stage)
        os.chmod(launcher_stage, 0o755)
        if not fake:
            os.chown(launcher_stage, 0, 0)
    except Exception:
        for item in staged_paths:
            if item.is_dir(): shutil.rmtree(item)
            else: item.unlink(missing_ok=True)
        raise
    old_commit = marker.read_text() if marker.is_file() else None
    saved = {"had_src": installed.is_dir(), "had_tests": (tree / "tests").is_dir(),
             "had_state": (tree / "state").is_dir(),
             "had_venv": venv.is_dir(), "had_tokens": (tree / "caller-tokens.json").is_file(),
             "had_receipt-signing.key": (tree / "receipt-signing.key").is_file(),
             "had_receipt-signing.pub": (tree / "receipt-signing.pub").is_file(),
             "had_launcher": launcher.is_file(), "old_commit": old_commit,
             "new_commit": source_commit, "tree_mode": stat.S_IMODE(tree.stat().st_mode)}
    swaps = [(installed, previous, staging), (tree / "tests", tree / "tests.old", schema_stage),
             (venv, tree / "venv.old", venv_stage),
             (tree / "state", tree / "state.old", state_stage),
             (tree / "caller-tokens.json", tree / "caller-tokens.json.old", token_stage),
             (tree / "receipt-signing.key", tree / "receipt-signing.key.old", private_stage),
             (tree / "receipt-signing.pub", tree / "receipt-signing.pub.old", public_stage),
             (launcher, launcher.with_name("broker-launch.old"), launcher_stage)]
    try:
        for stage, (live, backup, new) in zip(("src", "schema", "venv", "state", "tokens", "receipt-key",
                                               "receipt-pubkey", "launcher"), swaps):
            if backup.exists():
                raise RuntimeError(f"update backup already exists: {backup}")
            if live.exists():
                os.replace(live, backup)
            os.replace(new, live)
            if fake and os.environ.get("AGENT_CREW_OWNER_SETUP_TEST_FAIL_STAGE") == stage:
                raise RuntimeError(f"injected update failure: {stage}")
        os.chmod(tree, 0o755)
        if not fake:
            os.chown(tree, 0, 0)
        marker.write_text(source_commit + "\n")
        os.chmod(marker, 0o644)
        if not fake:
            run("chown", "root:root", str(marker))
        installed_selftest()
        if not fake:
            check = run("sudo", "-n", "-u", "crew-authz", str(launcher), "--check", capture=True).stdout
            say(check.rstrip())
            if "downgrade_reason=none" not in check:
                raise RuntimeError("installed broker --check still downgraded")
        # A successful update is committed: its temporary backups must not
        # block the next one-command update. Failure above still rolls back.
        for _, backup, _ in swaps:
            if backup.is_dir(): shutil.rmtree(backup)
            else: backup.unlink(missing_ok=True)
        update_manifest.unlink(missing_ok=True)
    except Exception:
        for live, backup, new in reversed(swaps):
            if backup.exists():
                if live.is_dir(): shutil.rmtree(live)
                else: live.unlink(missing_ok=True)
                os.replace(backup, live)
            elif new.exists() is False and live.exists():
                if live.is_dir(): shutil.rmtree(live)
                else: live.unlink(missing_ok=True)
            if new.is_dir(): shutil.rmtree(new)
            else: new.unlink(missing_ok=True)
        os.chmod(tree, saved["tree_mode"])
        if old_commit is None: marker.unlink(missing_ok=True)
        else: marker.write_text(old_commit)
        update_manifest.unlink(missing_ok=True)
        raise
    say("source update complete; broker restart required to load new source (not performed by this script): sudo -n -u crew-authz /usr/local/libexec/crew-authz/broker-launch.sh --restart")
    sys.exit(0)

targets = [tree, launcher.parent, sudoers]
def originals():
    # Backup whole broker tree: phase 2 changes ownership/mode recursively.
    return [x for x in targets if x.exists()]
def check_safe():
    for item in [tree, launcher.parent, sudoers, db, crew_dir, alfred_dir]:
        if item.is_symlink():
            raise RuntimeError(f"refusing symlink: {item}")
    if not db.is_file():
        raise RuntimeError(f"missing tasks DB: {db}")
    if not source_launcher.is_file():
        raise RuntimeError(f"missing launcher source: {source_launcher}")

def parse_broker_env(content):
    values = {}
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise ValueError(f"broker.env line {number}: expected KEY=VALUE")
        key, encoded = match.groups()
        if key.endswith("_CMD"):
            raise ValueError(f"broker.env line {number}: {key} is forbidden")
        if key.startswith("LD_") or key in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PATH", "IFS"}:
            raise ValueError(f"broker.env line {number}: shell control variable {key} is forbidden")
        if key in values:
            raise ValueError(f"broker.env line {number}: duplicate {key}")
        try:
            decoded = shlex.split(encoded)
        except ValueError as exc:
            raise ValueError(f"broker.env line {number}: invalid quoting: {exc}") from exc
        if encoded == "":
            value = ""
        elif len(decoded) == 1 and (re.fullmatch(r"'[^']*'", encoded)
                                         or shlex.quote(decoded[0]) == encoded):
            value = decoded[0]
        else:
            raise ValueError(f"broker.env line {number}: value must be shell-safe or single-quoted")
        values[key] = value
    return values

def sourced_env(content):
    command = ["env", "-i", "bash", "--noprofile", "--norc", "-c",
               'set -a; source /dev/stdin; /usr/bin/env -0']
    result = subprocess.run(command, input=content, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"broker.env source failed: {result.stderr.strip()}")
    return dict(part.decode().split("=", 1) for part in result.stdout.encode().split(b"\0") if part)

def validate_broker_env(content):
    values = parse_broker_env(content)
    syntax = subprocess.run(["bash", "-n"], input=content, text=True, capture_output=True)
    if syntax.returncode:
        raise ValueError(f"broker.env bash -n failed: {syntax.stderr.strip()}")
    baseline = sourced_env("")
    if set(values) & set(baseline):
        raise ValueError("broker.env overrides a baseline shell variable")
    if sourced_env(content) != {**baseline, **values}:
        raise ValueError("broker.env source round-trip differs from intended keys or values")
    return values

if args.undo:
    update_manifest = manifest_dir / "src-update.json"
    if update_manifest.exists():
        raise SystemExit(f"source update is outstanding; run --undo --update-src first: {update_manifest}")
    if not manifest_file.is_file():
        raise SystemExit(f"no apply manifest: {manifest_file}")
    manifest = json.loads(manifest_file.read_text())
    say(f"undo: restore archive {archive} and ACLs {acl_file}")
    for item in targets:
        if item.is_dir():
            shutil.rmtree(item)
        elif item.exists():
            item.unlink()
    run("tar", "--acls", "--xattrs", "--numeric-owner", "-xf", str(archive), "-C", str(root))
    if not fake:
        if acl_file.exists():
            run("setfacl", "--restore", str(acl_file))
        for user in manifest["added_users"]:
            run("gpasswd", "-d", user, "crew-authz-clients")
        if manifest["created_group"]:
            run("groupdel", "crew-authz-clients")
        if manifest["installed_acl"]:
            run("apt-get", "remove", "-y", "acl")
    shutil.rmtree(manifest_dir)
    for name in reversed(manifest.get("created_parents", [])):
        item = Path(name)
        if item.is_dir() and not any(item.iterdir()):
            item.rmdir()
    say("undo complete")
    sys.exit(0)

try:
    check_safe()
except RuntimeError as exc:
    if not args.apply:
        say(f"WOULD ABORT: {exc}")
        raise SystemExit(1) from exc
    raise
config = tree / "broker.env"
if config.is_symlink():
    if not args.apply:
        say(f"WOULD ABORT: refusing symlink: {config}")
    raise SystemExit(f"refusing symlink: {config}")
existing_content = read_broker_env(config)
try:
    if existing_content is not None:
        parse_broker_env(existing_content)
    overrides = {"AGENT_CREW_AUTHZ_CLIENT_GROUP": "crew-authz-clients",
                 "AGENT_CREW_CEA_BROKER_DB": "/home/truhojun/.agent_crew/alfred/tasks.db",
                 "AGENT_CREW_CEA_CALLER_TOKENS": "/opt/agent_crew-authz/caller-tokens.json",
                 "AGENT_CREW_CEA_MODE": "enforce",
                 "AGENT_CREW_CEA_ENFORCE_CODES": "RUNTIME_STATE_FORBIDS,SNAPSHOT_ROLLBACK",
                 "AGENT_CREW_CEA_SNAPSHOT_PUBKEY_FILE": "/opt/agent_crew-authz/snapshot.pub",
                 "AGENT_CREW_CEA_SNAPSHOT_HWM_FILE": "/opt/agent_crew-authz/state/snapshot-hwm.json"}
    retained = [line for line in (existing_content or "").splitlines()
                if line.split("=", 1)[0] not in
                (set(overrides) | {"AGENT_CREW_CEA_SNAPSHOT_KEY_FILE"})]
    final_content = "\n".join(retained + [f"{key}={shlex.quote(value)}"
                                             for key, value in overrides.items()]) + "\n"
    validate_broker_env(final_content)
except ValueError as exc:
    if not args.apply:
        say(f"WOULD ABORT: {exc}")
    raise SystemExit(str(exc)) from exc
if existing_content is not None:
    say("broker.env validation passed (bash -n and clean-env source round-trip)")
else:
    say("existing broker.env unreadable as non-root; validated at apply")
plan = [
    "create dedicated crew-authz-clients group; add truhojun and crew-authz if absent",
    f"install acl if missing; save ACLs, grant crew-authz x on {crew_dir}, rwx on {alfred_dir}, rw on {db}",
    f"backup existing broker tree, launcher directory and sudoers in {manifest_dir}",
    f"copy caller tokens to {tree / 'caller-tokens.json'} crew-authz:crew-authz 0400",
    f"copy Ed25519 public key {snapshot_source} to {tree / 'snapshot.pub'} root:crew-authz 0644; remove staged HMAC key",
    f"copy launcher to {launcher} root:root 0755; root-own broker tree and remove group/other write",
    f"validate temporary sudoers with visudo -cf, then install {sudoers} 0440",
    f"write {tree / 'SRC_COMMIT'} = {source_commit}",
    f"sudo -u crew-authz {launcher} --check; require downgrade_reason=none",
]
for step in plan:
    say(step)
commands = [
    ("tar --acls --xattrs --numeric-owner -cf <manifest>/before.tar -C / "
     + ("<existing targets>" if originals() else "-T /dev/null")),
    "getfacl -p <queue parent> <alfred queue dir> <tasks.db> > <manifest>/before.acl",
    "apt-get update && apt-get install -y acl  # only if setfacl is missing",
    "groupadd --system crew-authz-clients  # only if group is missing",
    "usermod -aG crew-authz-clients truhojun  # only if membership is missing",
    "usermod -aG crew-authz-clients crew-authz  # only if membership is missing",
    f"setfacl -m u:crew-authz:--x {crew_dir}",
    f"setfacl -m u:crew-authz:rwx {alfred_dir}",
    f"setfacl -m u:crew-authz:rw- {db}",
    f"install -m 0755 {source_launcher} {launcher}",
    f"install -m 0400 <caller tokens> {tree / 'caller-tokens.json'}",
    f"install -m 0644 {snapshot_source} {tree / 'snapshot.pub'}",
    "chown root:crew-authz <broker.env, caller tokens and snapshot.pub>",
    f"chown root:root <broker tree entries except keys under {tree}>",
    f"visudo -cf {sudoers.with_name('crew-authz-broker.pending')}",
    f"install -m 0440 <validated pending sudoers> {sudoers}",
    f"sudo -u crew-authz {launcher} --check",
]
for command in commands:
    say(f"PLAN $ {command}")
report_preconditions()
if not args.apply:
    say("dry-run: no changes made")
    sys.exit(0)
if manifest_file.exists():
    old = json.loads(manifest_file.read_text())
    if old.get("completed") and launcher.is_file() and sudoers.is_file():
        say("already applied: no changes made")
        sys.exit(0)
    raise SystemExit(f"incomplete apply; undo first: {manifest_file}")
if not token_source or not token_source.is_file():
    raise SystemExit("--caller-tokens must name a readable token table for apply")
if not snapshot_source.is_file() or snapshot_source.is_symlink():
    raise SystemExit(f"missing snapshot public key: {snapshot_source}")
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    parsed_key = serialization.load_pem_public_key(snapshot_source.read_bytes())
    if not isinstance(parsed_key, Ed25519PublicKey):
        raise ValueError("expected an Ed25519 public key")
except (ImportError, ValueError) as exc:
    raise SystemExit(f"invalid Ed25519 public key {snapshot_source}: {exc}") from exc
# The installed-path proof runs after staging, against the exact paths the
# launcher will use. The manifest permits undo if any later root operation fails.
if fake and os.environ.get("AGENT_CREW_OWNER_SETUP_SELFTEST") == "1":
    preinstall_selftest(token_source)
for base in (manifest_dir, launcher.parent, tree, sudoers.parent):
    missing = []
    current = base
    while not current.exists() and current != root:
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        if item not in created_parents:
            created_parents.append(item)
manifest_dir.mkdir(parents=True, mode=0o700)
before = originals()
manifest = {"existing": [str(x) for x in before], "added_users": [],
            "created_group": False, "installed_acl": False, "completed": False,
            "created_parents": [str(x) for x in created_parents]}
members = [str(x.relative_to(root)) for x in before]
# GNU tar rejects a create command with no operands. An explicit empty file list
# produces a valid empty archive that --undo can extract in the first-install case.
run("tar", "--acls", "--xattrs", "--numeric-owner", "-cf", str(archive),
    "-C", str(root), *(members or ["-T", "/dev/null"]))
if not fake:
    if shutil.which("getfacl"):
        acl_file.write_text(run("getfacl", "-p", str(crew_dir), str(alfred_dir), str(db), capture=True).stdout)
manifest_file.write_text(json.dumps(manifest, indent=2))
os.chmod(manifest_file, 0o600)
try:
    if not fake:
        if not shutil.which("setfacl"):
            run("apt-get", "update")
            run("apt-get", "install", "-y", "acl")
            manifest["installed_acl"] = True
            manifest_file.write_text(json.dumps(manifest, indent=2))
        if not acl_file.exists():
            acl_file.write_text(run("getfacl", "-p", str(crew_dir), str(alfred_dir), str(db), capture=True).stdout)
        group_exists = subprocess.run(["getent", "group", "crew-authz-clients"], capture_output=True).returncode == 0
        if not group_exists:
            run("groupadd", "--system", "crew-authz-clients")
            manifest["created_group"] = True
            manifest_file.write_text(json.dumps(manifest, indent=2))
        for user in ("truhojun", "crew-authz"):
            groups = run("id", "-nG", user, capture=True).stdout.split()
            if "crew-authz-clients" not in groups:
                run("usermod", "-aG", "crew-authz-clients", user)
                manifest["added_users"].append(user)
                manifest_file.write_text(json.dumps(manifest, indent=2))
        manifest_file.write_text(json.dumps(manifest, indent=2))
        run("setfacl", "-m", "u:crew-authz:--x", str(crew_dir))
        run("setfacl", "-m", "u:crew-authz:rwx", str(alfred_dir))
        run("setfacl", "-m", "u:crew-authz:rw-", str(db))
    tree.mkdir(parents=True, exist_ok=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(launcher.parent, stat.S_IMODE(launcher.parent.stat().st_mode) & ~0o022)
    if not fake:
        run("chown", "root:root", str(launcher.parent))
    shutil.copyfile(source_launcher, launcher)
    os.chmod(launcher, 0o755)
    shutil.copyfile(token_source, tree / "caller-tokens.json")
    os.chmod(tree / "caller-tokens.json", 0o400)
    (tree / "snapshot.key").unlink(missing_ok=True)
    shutil.copyfile(snapshot_source, tree / "snapshot.pub")
    os.chmod(tree / "snapshot.pub", 0o644)
    config.write_text(final_content)
    os.chmod(config, 0o640)
    (tree / "SRC_COMMIT").write_text(source_commit + "\n")
    if not (tree / "src").exists():
        shutil.copytree(source_root / "src", tree / "src")
    normalize(tree / "src")
    schema_installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schema_source, schema_installed)
    normalize(tree / "tests")
    if not venv.exists():
        build_venv(venv)
    for directory, dirs, files in os.walk(tree):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink():
                if item.is_relative_to(venv):
                    continue  # venv/bin/python is a root-owned interpreter symlink
                raise RuntimeError(f"symlink in broker tree: {item}")
            if item != tree / "caller-tokens.json":
                os.chmod(item, 0o755 if item.is_dir() or os.access(item, os.X_OK) else 0o644)
            if not fake:
                if item.name == "caller-tokens.json":
                    run("chown", "crew-authz:crew-authz", str(item))
                else:
                    group = "crew-authz" if item.name in ("snapshot.pub", "broker.env") else "root"
                    run("chown", f"root:{group}", str(item))
    os.chmod(tree, 0o755)
    if not fake:
        run("chown", "root:root", str(tree))
        run("chown", "root:root", str(launcher))
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    temp = sudoers.with_name("crew-authz-broker.pending")
    temp.write_text("truhojun ALL=(crew-authz) NOPASSWD: /usr/local/libexec/crew-authz/broker-launch.sh\n")
    os.chmod(temp, 0o440)
    if not fake:
        run("chown", "root:root", str(temp))
        run("visudo", "-cf", str(temp))
    os.replace(temp, sudoers)
    if not fake:
        installed_selftest()
        check = run("sudo", "-u", "crew-authz", str(launcher), "--check", capture=True).stdout
        say(check.rstrip())
        if "downgrade_reason=none" not in check:
            raise RuntimeError("broker check still downgraded")
    else:
        say("fake-root: skipped user/group/ACL and launcher execution")
    manifest["completed"] = True
    manifest_file.write_text(json.dumps(manifest, indent=2))
    say("apply complete")
except Exception:
    say(f"apply failed; restore with --undo using {manifest_file}")
    raise
PY
