#!/usr/bin/env bash
# Owner-only, offline phase-2 broker installation. See docs/cea_broker_oop_deploy.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export CREW_AUTHZ_LAUNCHER_SOURCE="$SCRIPT_DIR/broker-launch.sh"
export CREW_AUTHZ_SOURCE_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
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
import tarfile

p = argparse.ArgumentParser(description="Offline, reversible root broker setup")
mode = p.add_mutually_exclusive_group()
mode.add_argument("--dry-run", action="store_true", help="print plan only (default)")
mode.add_argument("--apply", action="store_true")
mode.add_argument("--undo", action="store_true")
p.add_argument("--caller-tokens", type=Path, help="private adapter token table to copy")
p.add_argument("--with-snapshot-key", action="store_true", help="copy ssot-producer.key")
p.add_argument("--root-prefix", type=Path, help=argparse.SUPPRESS)
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
private = path("/home/truhojun/.verify-private")
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
snapshot_source = private / "ssot-producer.key"
source_launcher = Path(os.environ["CREW_AUTHZ_LAUNCHER_SOURCE"])
source_commit = os.environ["CREW_AUTHZ_SOURCE_COMMIT"]
if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
    p.error("invalid source commit")

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
    if fake:
        for item in targets:
            if item.is_dir():
                shutil.rmtree(item)
            elif item.exists():
                item.unlink()
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter="data")
    else:
        for item in targets:
            if item.is_dir():
                shutil.rmtree(item)
            elif item.exists():
                item.unlink()
        run("tar", "--acls", "--xattrs", "--numeric-owner", "-xf", str(archive), "-C", "/")
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
    f"copy launcher to {launcher} root:root 0755; root-own broker tree and remove group/other write",
    f"validate temporary sudoers with visudo -cf, then install {sudoers} 0440",
    f"write {tree / 'SRC_COMMIT'} = {source_commit}",
    f"sudo -u crew-authz {launcher} --check; require downgrade_reason=none",
]
if args.with_snapshot_key:
    plan.insert(4, f"copy {snapshot_source} to {tree / 'snapshot.key'} root:crew-authz 0640")
else:
    plan.insert(4, "snapshot key omitted: snapshots remain unverified; admission may refuse")
for step in plan:
    say(step)
commands = [
    "tar --acls --xattrs --numeric-owner -cf <manifest>/before.tar -C / <existing targets>",
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
    "chown root:crew-authz <broker.env and copied keys>",
    f"chown root:root <broker tree entries except keys under {tree}>",
    f"visudo -cf {sudoers.with_name('crew-authz-broker.pending')}",
    f"install -m 0440 <validated pending sudoers> {sudoers}",
    f"sudo -u crew-authz {launcher} --check",
]
if args.with_snapshot_key:
    commands.insert(11, f"install -m 0640 {snapshot_source} {tree / 'snapshot.key'}")
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
if args.with_snapshot_key and not snapshot_source.is_file():
    raise SystemExit(f"missing snapshot key: {snapshot_source}")
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
if fake:
    with tarfile.open(archive, "w") as bundle:
        for item in before:
            bundle.add(item, arcname=str(item.relative_to(root)))
else:
    run("tar", "--acls", "--xattrs", "--numeric-owner", "-cf", str(archive),
        "-C", "/", *(str(x.relative_to(root)) for x in before))
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
    if args.with_snapshot_key:
        shutil.copyfile(snapshot_source, tree / "snapshot.key")
        os.chmod(tree / "snapshot.key", 0o640)
    config = tree / "broker.env"
    lines = config.read_text().splitlines() if config.exists() else []
    overrides = {"AGENT_CREW_AUTHZ_CLIENT_GROUP": "crew-authz-clients",
                 "AGENT_CREW_CEA_BROKER_DB": "/home/truhojun/.agent_crew/alfred/tasks.db",
                 "AGENT_CREW_CEA_CALLER_TOKENS": "/opt/agent_crew-authz/caller-tokens.json",
                 "AGENT_CREW_CEA_MODE": "enforce",
                 "AGENT_CREW_CEA_ENFORCE_CODES": "RUNTIME_STATE_FORBIDS"}
    if args.with_snapshot_key:
        overrides["AGENT_CREW_CEA_SNAPSHOT_KEY_FILE"] = "/opt/agent_crew-authz/snapshot.key"
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
                group = "crew-authz" if item.name in ("caller-tokens.json", "snapshot.key", "broker.env") else "root"
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
