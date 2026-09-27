#!/usr/bin/env python3
"""Disposable socket ownership and authenticated cross-process broker proof."""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time


def client(sock, credential, expected_uid):
    from agent_crew.cea.broker import BrokerClient
    from agent_crew.cea.intent import Intent, IntentIdentity, Target, WorkClass
    from agent_crew.cea.service import encode_intent
    intent = Intent(identity=IntentIdentity(project="sandbox", work_class=WorkClass.REVIEW,
                    target=Target(repo="sandbox", base_ref="main")), task_id="broker-group-proof",
                    task_type="review", description="isolated authenticated socket proof")
    reply, peer_uid = BrokerClient(sock, expected_uid=expected_uid)._call(
        {"op": "authorize", **encode_intent(intent, credential)})
    if peer_uid != expected_uid or reply.get("code") == "UNAUTHENTICATED" or not reply.get("receipt"):
        raise RuntimeError(f"authenticated authorize failed: peer={peer_uid} reply={reply}")
    from agent_crew.cea.schema import validate_receipt
    errors = validate_receipt(reply["receipt"])
    if errors:
        raise RuntimeError(f"invalid receipt: {errors}")
    from agent_crew.cea import signed_receipt
    public = signed_receipt.load_public(os.environ["AGENT_CREW_CEA_RECEIPT_PUBKEY_FILE"])
    receipt = reply["receipt"]
    valid = signed_receipt.verify_signature(receipt, public)
    tampered = dict(receipt, description="tampered")
    rejected = signed_receipt.verify_signature(tampered, public)
    if not valid or rejected:
        raise RuntimeError(f"signed receipt verification or tamper rejection failed: decision={receipt.get('decision')} valid={valid} tampered_valid={rejected}")
    return {"peer_uid": peer_uid, "authorize_code": reply.get("code"),
            "decision": reply["receipt"].get("decision"), "authenticated": True,
            "schema_valid": True, "signed_receipt_valid": valid,
            "tampered_receipt_rejected": not rejected,
            "downgrade_reason": reply["receipt"].get("downgrade_reason")}


def proof(source_root, token_source, *, cross_uid, installed_root=None):
    import secrets
    broker_uid = pwd.getpwnam("crew-authz").pw_uid if cross_uid else os.geteuid()
    broker_gid = pwd.getpwnam("crew-authz").pw_gid if cross_uid else os.getegid()
    client_uid = 1000 if cross_uid else os.geteuid()
    client_gid = pwd.getpwuid(client_uid).pw_gid
    user_site = (Path(pwd.getpwuid(client_uid).pw_dir) / ".local" / "lib" /
                 f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    group_gid = grp.getgrnam("crew-authz-clients").gr_gid
    if cross_uid and os.geteuid() != 0:
        raise RuntimeError("cross-uid proof requires root")
    if cross_uid and (broker_uid != 998 or client_uid != 1000):
        raise RuntimeError(f"unexpected broker/client uid: {broker_uid}/{client_uid}")
    with tempfile.TemporaryDirectory(prefix="crew-broker-group-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        staged = installed_root / "src" if installed_root else root / "src"
        if not installed_root:
            for item in (source_root / "src").rglob("*"):
                if item.is_symlink():
                    raise RuntimeError(f"source symlink: {item}")
            shutil.copytree(source_root / "src", staged, symlinks=False)
        staged_client = root / "broker_group_client.py"
        shutil.copyfile(Path(__file__), staged_client)
        staged_client.chmod(0o644)
        if installed_root:
            contract_dir = installed_root / "tests" / "cea_contract"
            commit_marker = installed_root / "SRC_COMMIT"
            if not (contract_dir / "receipt.schema.json").is_file():
                raise RuntimeError("installed receipt schema missing")
        else:
            contract_dir = root / "tests" / "cea_contract"
            contract_dir.mkdir(parents=True)
            shutil.copyfile(source_root / "tests" / "cea_contract" / "receipt.schema.json",
                            contract_dir / "receipt.schema.json")
            commit_marker = root / "SRC_COMMIT"
            commit_marker.write_text("sandbox-preinstall\n")
        if cross_uid and not installed_root:
            for parent, dirs, files in os.walk(staged):
                for name in dirs + files:
                    item = Path(parent) / name
                    if item.is_symlink():
                        raise RuntimeError(f"source symlink: {item}")
                    os.chown(item, 0, 0)
                    os.chmod(item, stat.S_IMODE(item.stat().st_mode) & ~0o022)
            os.chown(staged, 0, 0)
        private = root / "private"
        private.mkdir(mode=0o700)
        if cross_uid:
            os.chown(private, broker_uid, broker_gid)
        tokens = installed_root / "caller-tokens.json" if installed_root else private / "tokens.json"
        # Use an isolated token with the same adapter-table shape; never emit it.
        original = json.loads(Path(token_source).read_text())
        adapters = original.get("adapters") or {}
        if not adapters:
            raise RuntimeError("caller token table has no adapters")
        identity = next(iter(adapters.values()))
        credential = next(iter(adapters)) if installed_root else secrets.token_urlsafe(32)
        if not installed_root:
            tokens.write_text(json.dumps({"adapters": {credential: identity}}))
            tokens.chmod(0o600)
            if cross_uid:
                os.chown(tokens, broker_uid, broker_gid)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        signing_private = installed_root / "receipt-signing.key" if installed_root else private / "receipt-signing.key"
        signing_public = installed_root / "receipt-signing.pub" if installed_root else root / "receipt-signing.pub"
        if not installed_root:
            key = Ed25519PrivateKey.generate()
            signing_private.write_bytes(key.private_bytes(serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw, serialization.NoEncryption()))
            signing_public.write_bytes(key.public_key().public_bytes(serialization.Encoding.Raw,
                serialization.PublicFormat.Raw))
            signing_private.chmod(0o400)
            signing_public.chmod(0o644)
            if cross_uid:
                os.chown(signing_private, broker_uid, broker_gid)
        sockdir = root / "sock"
        sockdir.mkdir()
        if cross_uid:
            os.chown(sockdir, broker_uid, group_gid)
        else:
            # sg gives this process the client group for the real chmod/chgrp path.
            os.chown(sockdir, broker_uid, group_gid)
        sockdir.chmod(0o710)
        db = private / "receipts.db"
        # The production broker opens an already initialized task database.
        sys.path.insert(0, str(staged))
        if not installed_root:
            sys.path.insert(1, str(user_site))
        from agent_crew.queue import TaskQueue
        TaskQueue(str(db))
        if cross_uid:
            os.chown(db, broker_uid, broker_gid)
        env = ({"PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1"} if installed_root else
               {k: v for k, v in os.environ.items() if not k.startswith("AGENT_CREW_")})
        env.update(PYTHONPATH=(str(staged) if installed_root else os.pathsep.join((str(staged), str(user_site)))),
                   AGENT_CREW_AUTHZ_SRC_COMMIT_PATH=str(commit_marker),
                   AGENT_CREW_CEA_BROKER_DB=str(db),
                   AGENT_CREW_CEA_RECEIPT_SIGNING_KEY_FILE=str(signing_private),
                   AGENT_CREW_CEA_RECEIPT_PUBKEY_FILE=str(signing_public),
                   AGENT_CREW_CEA_CALLER_TOKENS=str(tokens), AGENT_CREW_CEA_MODE="enforce",
                   AGENT_CREW_CEA_REGISTRY_PATH=str(root / "absent-registry.json"),
                   AGENT_CREW_CEA_SNAPSHOT_PATH=str(root / "absent-snapshot.json"),
                   AGENT_CREW_CEA_QUOTA_CACHE_DIR=str(root / "absent-quota"),
                   AGENT_CREW_CEA_CODEX_AUTH_PATH=str(root / "absent-auth.json"))
        if installed_root:
            env["AGENT_CREW_AUTHZ_LAUNCHER_PATH"] = str(installed_root.parent.parent / "usr/local/libexec/crew-authz/broker-launch.sh")
            env["AGENT_CREW_AUTHZ_CONFIG_PATH"] = str(installed_root / "broker.env")
        python = str(installed_root / "venv/bin/python") if installed_root else sys.executable
        broker_cmd = [python, "-m", "agent_crew.cea.broker", "--sock-dir", str(sockdir),
                      "--client-uid", str(client_uid)]
        if cross_uid:
            broker_cmd = ["setpriv", "--reuid", str(broker_uid), "--regid", str(broker_gid),
                          "--init-groups", *broker_cmd]
        else:
            broker_cmd.append("--degraded")
        broker = subprocess.Popen(broker_cmd, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            sock = sockdir / "broker.sock"
            for _ in range(100):
                if sock.exists():
                    break
                if broker.poll() is not None:
                    raise RuntimeError(f"broker exited: {broker.stderr.read()}")
                time.sleep(.05)
            else:
                raise RuntimeError("broker socket timed out")
            st = sock.lstat()
            if (not stat.S_ISSOCK(st.st_mode) or
                    (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (broker_uid, group_gid, 0o660)):
                raise RuntimeError(f"wrong socket ownership/mode: {st.st_uid}:{st.st_gid} {stat.S_IMODE(st.st_mode):04o}")
            client_env = dict(env, BROKER_PROOF_CREDENTIAL=credential)
            client_cmd = [python, str(staged_client), "--client", str(sock),
                          str(broker_uid)]
            if cross_uid:
                client_cmd = ["setpriv", "--reuid", str(client_uid), "--regid", str(client_gid),
                              "--groups", str(group_gid), *client_cmd]
            else:
                client_cmd = ["sg", "crew-authz-clients", "-c", shlex.join(client_cmd)]
            result = subprocess.run(client_cmd, env=client_env, capture_output=True, text=True, timeout=20)
            if result.returncode:
                broker.terminate()
                _, broker_error = broker.communicate(timeout=5)
                raise RuntimeError(f"client failed: {result.stderr.strip()} broker: {broker_error}")
            return {"pass": True, "cross_uid": cross_uid, "socket_uid": st.st_uid,
                    "socket_gid": st.st_gid, "socket_mode": "0660", **json.loads(result.stdout)}
        finally:
            broker.terminate()
            try:
                broker.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.communicate()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--caller-tokens", type=Path)
    parser.add_argument("--cross-uid", action="store_true")
    parser.add_argument("--installed-root", type=Path)
    parser.add_argument("--client", nargs=2, metavar=("SOCKET", "EXPECTED_UID"))
    args = parser.parse_args()
    if args.client:
        print(json.dumps(client(args.client[0], os.environ["BROKER_PROOF_CREDENTIAL"], int(args.client[1]))))
        return
    if args.installed_root:
        args.caller_tokens = args.installed_root / "caller-tokens.json"
    if not args.caller_tokens:
        parser.error("--caller-tokens is required")
    if not args.cross_uid and os.getegid() != grp.getgrnam("crew-authz-clients").gr_gid:
        command = [sys.executable, str(Path(__file__).resolve()), "--source-root", str(args.source_root),
                   "--caller-tokens", str(args.caller_tokens)]
        result = subprocess.run(["sg", "crew-authz-clients", "-c", shlex.join(command)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        print(result.stdout, end="")
        return
    print(json.dumps(proof(args.source_root, args.caller_tokens, cross_uid=args.cross_uid,
                           installed_root=args.installed_root), sort_keys=True))


if __name__ == "__main__":
    main()
