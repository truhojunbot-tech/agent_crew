#!/usr/bin/env bash
# Owner-only, offline phase-2 broker installation. See docs/cea_broker_oop_deploy.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export CREW_AUTHZ_LAUNCHER_SOURCE="$SCRIPT_DIR/broker-launch.sh"
export CREW_AUTHZ_SOURCE_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
export CREW_AUTHZ_SOURCE_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
python3 - "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import re
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
if token_source is None:
    env_file = tree / "broker.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            match = re.fullmatch(r"AGENT_CREW_CEA_CALLER_TOKENS=['\"]?([^'\"]+)['\"]?", line.strip())
            if match:
                candidate = Path(match.group(1))
                if candidate != tree / "caller-tokens.json":
                    token_source = path(str(candidate)) if fake else candidate
                break
snapshot_source = args.snapshot_pubkey or path("/home/truhojun/alfred/governance/ssot-producer-ed25519.pub")
if fake and args.snapshot_pubkey and snapshot_source.is_absolute() and not snapshot_source.is_relative_to(root):
    snapshot_source = path(str(snapshot_source))
source_launcher = Path(os.environ["CREW_AUTHZ_LAUNCHER_SOURCE"])
source_commit = os.environ["CREW_AUTHZ_SOURCE_COMMIT"]
source_root = Path(os.environ["CREW_AUTHZ_SOURCE_ROOT"])
if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
    p.error("invalid source commit")

if args.update_src:
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
    if not args.apply:
        say("dry-run: no changes made")
        sys.exit(0)
    if not tree.is_dir() or tree.is_symlink() or not source.is_dir():
        raise SystemExit("broker tree or checkout src missing")
    if previous.exists() or staging.exists() or update_manifest.exists():
        raise SystemExit("source update backup or manifest exists; undo first")
    if not fake and run("git", "-C", str(source_root), "status", "--porcelain", capture=True).stdout.strip():
        raise SystemExit(f"source checkout is dirty: {source_root}")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise SystemExit(f"symlink in source tree: {item}")
    old_commit = marker.read_text() if marker.is_file() else None
    manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    update_manifest.write_text(json.dumps({"had_src": installed.is_dir(),
                                           "old_commit": old_commit, "new_commit": source_commit}, indent=2))
    os.chmod(update_manifest, 0o600)
    try:
        shutil.copytree(source, staging)
        for directory, dirs, files in os.walk(staging):
            for name in dirs + files:
                item = Path(directory) / name
                os.chmod(item, stat.S_IMODE(item.stat().st_mode) & ~0o022)
                if not fake:
                    run("chown", "root:root", str(item))
        if not fake:
            run("chown", "root:root", str(staging))
        if installed.exists():
            os.replace(installed, previous)
        os.replace(staging, installed)
        marker.write_text(source_commit + "\n")
        os.chmod(marker, 0o644)
        if not fake:
            run("chown", "root:root", str(marker))
    except Exception:
        say(f"source update failed; restore with --undo --update-src using {update_manifest}")
        raise
    say("source update complete")
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

if args.undo:
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

check_safe()
plan = [
    "create dedicated crew-authz-clients group; add truhojun and crew-authz if absent",
    f"install acl if missing; save ACLs, grant crew-authz x on {crew_dir}, rwx on {alfred_dir}, rw on {db}",
    f"backup existing broker tree, launcher directory and sudoers in {manifest_dir}",
    f"copy caller tokens to {tree / 'caller-tokens.json'} root:crew-authz 0640",
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
    f"install -m 0640 <caller tokens> {tree / 'caller-tokens.json'}",
    f"install -m 0644 {snapshot_source} {tree / 'snapshot.pub'}",
    "chown root:crew-authz <broker.env, caller tokens and snapshot.pub>",
    f"chown root:root <broker tree entries except keys under {tree}>",
    f"visudo -cf {sudoers.with_name('crew-authz-broker.pending')}",
    f"install -m 0440 <validated pending sudoers> {sudoers}",
    f"sudo -u crew-authz {launcher} --check",
]
for command in commands:
    say(f"PLAN $ {command}")
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
    os.chmod(tree / "caller-tokens.json", 0o640)
    (tree / "snapshot.key").unlink(missing_ok=True)
    shutil.copyfile(snapshot_source, tree / "snapshot.pub")
    os.chmod(tree / "snapshot.pub", 0o644)
    config = tree / "broker.env"
    lines = config.read_text().splitlines() if config.exists() else []
    overrides = {"AGENT_CREW_AUTHZ_CLIENT_GROUP": "crew-authz-clients",
                 "AGENT_CREW_CEA_BROKER_DB": "/home/truhojun/.agent_crew/alfred/tasks.db",
                 "AGENT_CREW_CEA_CALLER_TOKENS": "/opt/agent_crew-authz/caller-tokens.json",
                 "AGENT_CREW_CEA_MODE": "enforce",
                 "AGENT_CREW_CEA_ENFORCE_CODES": "RUNTIME_STATE_FORBIDS",
                 "AGENT_CREW_CEA_SNAPSHOT_PUBKEY_FILE": "/opt/agent_crew-authz/snapshot.pub"}
    lines = [line for line in lines if line.split("=", 1)[0] not in
             (set(overrides) | {"AGENT_CREW_CEA_SNAPSHOT_KEY_FILE"})]
    lines += [f"{key}={value}" for key, value in overrides.items()]
    config.write_text("\n".join(lines) + "\n")
    os.chmod(config, 0o640)
    (tree / "SRC_COMMIT").write_text(source_commit + "\n")
    for directory, dirs, files in os.walk(tree):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink():
                raise RuntimeError(f"symlink in broker tree: {item}")
            os.chmod(item, stat.S_IMODE(item.stat().st_mode) & ~0o022)
            if not fake:
                group = "crew-authz" if item.name in ("caller-tokens.json", "snapshot.pub", "broker.env") else "root"
                run("chown", f"root:{group}", str(item))
    os.chmod(tree, stat.S_IMODE(tree.stat().st_mode) & ~0o022)
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
