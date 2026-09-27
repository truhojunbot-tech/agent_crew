"""Root updater gates, exercised under an isolated, caller-owned fixture root."""
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/cea/broker-update"
loader = importlib.machinery.SourceFileLoader("broker_update_under_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
update = importlib.util.module_from_spec(spec)
loader.exec_module(update)


def _keypair():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, public


def _signature(key, public, body):
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"ed25519": {"key_id": hashlib.sha256(public).hexdigest()[:16],
                         "value": base64.b64encode(key.sign(encoded)).decode()}}


def _fixture(tmp_path, *, sha="a" * 40, expiry=None, owner_key=False, reviewed_head="b" * 40):
    root = tmp_path / "opt/agent_crew-authz"
    state = root / "state"
    state.mkdir(parents=True)
    root.chmod(0o755)
    (root / "src").mkdir()
    (root / "src").chmod(0o755)
    state.chmod(0o700)
    key, pub = _keypair()
    (root / "snapshot.pub").write_bytes(pub)
    (root / "snapshot.pub").chmod(0o644)
    decision = {"decision_id": "T0-updater", "body_hash": "sha256:owner-proof",
                "scope": "broker-update", "sha": sha, "reviewed_head": reviewed_head,
                "pr_number": 469, "expires_at": expiry or time.time() + 3600}
    if owner_key:
        owner, owner_pub = _keypair()
        (root / "owner-t0.pub").write_bytes(owner_pub)
        (root / "owner-t0.pub").chmod(0o644)
        decision["owner_signature"] = _signature(owner, owner_pub, decision)
    body = {"generation": 4, "produced_at": time.time(), "decisions": [decision]}
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({**body, "signature": _signature(key, pub, body)}))
    digest = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode()).hexdigest()
    (state / "snapshot-hwm.json").write_text(json.dumps({"generation": 4, "content_hash": digest}))
    (state / "snapshot-hwm.json").chmod(0o600)
    return root, snapshot, key, pub, body


def test_signed_exact_t0_and_separate_owner_key(tmp_path):
    root, snapshot, *_ = _fixture(tmp_path, owner_key=True)
    decision, ref = update.verified_t0("a" * 40, root=root, snapshot=snapshot)
    assert decision["pr_number"] == 469 and ref.generation == 4
    saved = json.loads(snapshot.read_text())
    saved["decisions"][0]["owner_signature"]["ed25519"]["value"] = "AAAA"
    key, pub = _keypair()
    # Re-sign the snapshot; the owner T0 proof must still fail independently.
    (root / "snapshot.pub").write_bytes(pub)
    body = {k: v for k, v in saved.items() if k != "signature"}
    saved["signature"] = _signature(key, pub, body)
    snapshot.write_text(json.dumps(saved))
    mark = root / "state/snapshot-hwm.json"
    mark.write_text(json.dumps({"generation": 4, "content_hash":
        "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode()).hexdigest()}))
    mark.chmod(0o600)
    with pytest.raises(update.Refused, match="owner T0"):
        update.verified_t0("a" * 40, root=root, snapshot=snapshot)


@pytest.mark.parametrize("change,reason", [
    ("wrong_sha", "owner T0"), ("no_t0", "owner T0"),
    ("expired", "owner T0"), ("bad_sig", "snapshot invalid"),
    ("rollback", "SNAPSHOT_ROLLBACK"), ("unreviewed", "owner T0"),
])
def test_t0_gate_refuses(tmp_path, change, reason):
    root, snapshot, key, pub, body = _fixture(tmp_path)
    if change == "wrong_sha": body["decisions"][0]["sha"] = "c" * 40
    if change == "no_t0": body["decisions"] = []
    if change == "expired": body["decisions"][0]["expires_at"] = time.time() - 1
    if change == "unreviewed": body["decisions"][0].pop("reviewed_head")
    if change == "rollback":
        (root / "state/snapshot-hwm.json").write_text(json.dumps({"generation": 5,
                                                                 "content_hash": "sha256:older"}))
    else:
        (root / "state/snapshot-hwm.json").write_text(json.dumps({"generation": 4,
            "content_hash": "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}))
    signed = {**body, "signature": _signature(key, pub, body)}
    if change == "bad_sig": signed["signature"]["ed25519"]["value"] = "AAAA"
    snapshot.write_text(json.dumps(signed))
    with pytest.raises(update.Refused, match=reason):
        update.verified_t0("a" * 40, root=root, snapshot=snapshot)


def test_bad_sha_and_caller_env_not_used(monkeypatch, tmp_path):
    with pytest.raises(update.Refused, match="full lowercase"):
        update.run_update("a" * 39, root=tmp_path / "root", state=tmp_path / "state")
    monkeypatch.setenv("PYTHONPATH", "/tmp/attacker")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/attacker-config")
    assert "PYTHONPATH" not in update.SAFE_ENV
    assert update.SAFE_ENV["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "/usr/sbin" in update.SAFE_ENV["PATH"].split(":")  # owner apply runs visudo
    assert update.REMOTE.startswith("https://github.com/truhojunbot-tech/")


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                                          "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                          "GIT_COMMITTER_NAME": "Test",
                                          "GIT_COMMITTER_EMAIL": "test@example.invalid"}).stdout.strip()


def test_source_must_be_reachable_from_main_and_merge(tmp_path):
    source = tmp_path / "source"
    _git("init", "-b", "main", str(source))
    (source / "README").write_text("base")
    _git("add", ".", cwd=source); _git("commit", "-m", "base", cwd=source)
    base = _git("rev-parse", "HEAD", cwd=source)
    _git("checkout", "-b", "feature", cwd=source)
    (source / "feature").write_text("reviewed")
    _git("add", ".", cwd=source); _git("commit", "-m", "feature", cwd=source)
    head = _git("rev-parse", "HEAD", cwd=source)
    _git("checkout", "main", cwd=source)
    _git("merge", "--no-ff", "feature", "-m", "merge", cwd=source)
    merged = _git("rev-parse", "HEAD", cwd=source)
    mirror, reviewed = update.fetch_source(merged, state=tmp_path / "private", remote=str(source))
    assert mirror.is_dir() and reviewed == head
    with pytest.raises(update.Refused, match="two-parent"):
        update.fetch_source(base, state=tmp_path / "private", remote=str(source))
    _git("checkout", "feature", cwd=source)
    (source / "unmerged").write_text("private")
    _git("add", ".", cwd=source); _git("commit", "-m", "unmerged", cwd=source)
    unreachable = _git("rev-parse", "HEAD", cwd=source)
    with pytest.raises(update.Refused, match="not a commit|not reachable"):
        update.fetch_source(unreachable, state=tmp_path / "private", remote=str(source))


def test_rehearsal_failure_prevents_apply(monkeypatch, tmp_path):
    root, snapshot, *_ = _fixture(tmp_path)
    (root / "caller-tokens.json").write_text("{}")
    (root / "caller-tokens.json").chmod(0o400)
    state = tmp_path / "state"
    calls = []
    monkeypatch.setattr(update, "fetch_source", lambda sha, **kwargs: (tmp_path / "mirror", "b" * 40))
    def cmd(*argv, **kwargs):
        if "clone" in argv:
            setup = Path(argv[-1]) / "scripts/cea/owner_root_enforce_setup.sh"
            setup.parent.mkdir(parents=True)
            setup.write_text("exit 0\n")
        return ""
    monkeypatch.setattr(update, "command", cmd)
    original = update.subprocess.run
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "rehearsal refused")
    monkeypatch.setattr(update.subprocess, "run", run)
    with pytest.raises(update.Refused, match="owner rehearsal failed"):
        update.run_update("a" * 40, root=root, state=state, snapshot=snapshot)
    assert len(calls) == 1 and "--rehearse" in calls[0]


def test_fake_root_full_gate_and_successful_rehearsal(monkeypatch, tmp_path):
    root, snapshot, *_ = _fixture(tmp_path)
    (root / "caller-tokens.json").write_text("{}")
    (root / "caller-tokens.json").chmod(0o400)
    monkeypatch.setattr(update, "fetch_source", lambda sha, **kwargs: (tmp_path / "mirror", "b" * 40))
    def cmd(*argv, **kwargs):
        if "clone" in argv:
            setup = Path(argv[-1]) / "scripts/cea/owner_root_enforce_setup.sh"
            setup.parent.mkdir(parents=True)
            setup.write_text("exit 0\n")
        return ""
    monkeypatch.setattr(update, "command", cmd)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"] == update.SAFE_ENV
        return subprocess.CompletedProcess(argv, 0, "rehearsal complete", "")
    monkeypatch.setattr(update.subprocess, "run", run)
    details = update.run_update("a" * 40, root=root, state=tmp_path / "private",
                                snapshot=snapshot, rehearse=True)
    assert details["result"] == "rehearsed" and details["reviewed_head"] == "b" * 40
    assert len(calls) == 1 and "--rehearse" in calls[0]


@pytest.mark.skipif(os.getenv("RUN_BROKER_UPDATER_REHEARSAL") != "1",
                    reason="set RUN_BROKER_UPDATER_REHEARSAL=1 for real fetch and pinned venv")
def test_real_nonroot_updater_rehearsal(tmp_path):
    assert os.geteuid() != 0
    # Make a local reviewed merge from this checkout. origin/main may predate
    # the updater and its pinned-venv rehearsal fixes while the PR is open.
    source = tmp_path / "reviewed-repo"
    _git("clone", "--no-hardlinks", str(SCRIPT.parents[2]), str(source))
    reviewed = _git("rev-parse", "HEAD", cwd=source)
    _git("checkout", "-b", "main", "origin/main", cwd=source)
    _git("-c", "user.name=Updater Test", "-c", "user.email=updater@test.invalid",
         "merge", "--no-ff", reviewed, "-m", "reviewed merge", cwd=source)
    merged = _git("rev-parse", "HEAD", cwd=source)
    root, snapshot, key, *_ = _fixture(tmp_path, sha=merged, reviewed_head=reviewed)
    (root / "snapshot.pub").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    shutil.copytree(SCRIPT.parents[2] / "src", root / "src", dirs_exist_ok=True)
    (root / "src").chmod(0o755)
    tokens = Path("/home/truhojun/.agent_crew/alfred-cea/caller-tokens.json")
    shutil.copyfile(tokens, root / "caller-tokens.json")
    (root / "caller-tokens.json").chmod(0o400)
    completed = subprocess.run(["python3", str(SCRIPT), "--rehearse", merged],
        env={**os.environ, "AGENT_CREW_BROKER_UPDATE_REHEARSE_ROOT": str(tmp_path),
             "AGENT_CREW_BROKER_UPDATE_REHEARSE_REMOTE": str(source)},
        check=True, capture_output=True, text=True)
    details = json.loads(completed.stdout)
    assert details["result"] == "rehearsed"
