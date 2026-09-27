"""Ed25519 snapshot signatures use the same canonical body as HMAC."""
import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_crew.cea.input_providers.snapshot import (
    CanonicalPolicySnapshotReader, ed25519_verifier,
)
from agent_crew.cea.providers import SignatureStatus
from agent_crew.cea.wiring import _snapshot


@pytest.fixture
def keys():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, raw, pem


def signed_doc(private, raw, *, include_ed25519=True):
    body = {"generation": 1, "produced_at": "2026-09-27T00:00:00Z", "decisions": []}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    signature = {"alg": "hmac-sha256", "key_id": "hmac", "value": hmac.new(b"shared", canonical, hashlib.sha256).hexdigest()}
    if include_ed25519:
        signature["ed25519"] = {
            "key_id": hashlib.sha256(raw).hexdigest()[:16],
            "value": base64.b64encode(private.sign(canonical)).decode(),
        }
    return {**body, "signature": signature}


def write_doc(tmp_path: Path, doc):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("encoding", ["raw", "pem"])
def test_ed25519_valid_and_tampered_body(tmp_path, keys, encoding):
    private, raw, pem = keys
    doc = signed_doc(private, raw)
    path = write_doc(tmp_path, doc)
    reader = CanonicalPolicySnapshotReader(str(path), verifier=ed25519_verifier(raw if encoding == "raw" else pem))
    assert reader.current().signature is SignatureStatus.VALID
    doc["tier"] = "T0"
    write_doc(tmp_path, doc)
    assert reader.current().signature is SignatureStatus.INVALID


def test_ed25519_wrong_key_id_key_and_missing_block(tmp_path, keys):
    private, raw, _ = keys
    doc = signed_doc(private, raw)
    path = write_doc(tmp_path, doc)
    reader = CanonicalPolicySnapshotReader(str(path), verifier=ed25519_verifier(raw))
    doc["signature"]["ed25519"]["key_id"] = "0000000000000000"
    write_doc(tmp_path, doc)
    assert reader.current().signature is SignatureStatus.INVALID
    doc = signed_doc(private, raw)
    doc["signature"]["ed25519"]["value"] = "invalid base64!"
    write_doc(tmp_path, doc)
    assert reader.current().signature is SignatureStatus.INVALID
    doc = signed_doc(private, raw)
    doc["signature"]["ed25519"]["value"] = base64.b64encode(bytes(64)).decode()
    write_doc(tmp_path, doc)
    assert reader.current().signature is SignatureStatus.INVALID
    write_doc(tmp_path, signed_doc(private, raw, include_ed25519=False))
    assert reader.current().signature is SignatureStatus.INVALID
    write_doc(tmp_path, signed_doc(private, raw))
    other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert CanonicalPolicySnapshotReader(str(path), verifier=ed25519_verifier(other)).current().signature is SignatureStatus.INVALID


def test_pubkey_precedes_hmac_without_fallback(tmp_path, keys):
    private, raw, _ = keys
    path = write_doc(tmp_path, signed_doc(private, raw, include_ed25519=False))
    pubkey = tmp_path / "public.key"
    pubkey.write_bytes(raw)
    hmac_key = tmp_path / "hmac.key"
    hmac_key.write_bytes(b"shared")
    env = {"AGENT_CREW_CEA_SNAPSHOT_PATH": str(path),
           "AGENT_CREW_CEA_SNAPSHOT_PUBKEY_FILE": str(pubkey),
           "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE": str(hmac_key)}
    reader, status, configured = _snapshot(env)
    assert configured and "ed25519" in status.reason
    assert reader.current().signature is SignatureStatus.INVALID
    write_doc(tmp_path, signed_doc(private, raw))
    assert reader.current().signature is SignatureStatus.VALID
    pubkey.write_bytes(b"bad")
    reader, status, configured = _snapshot(env)
    assert not configured and "ed25519" in status.reason
    assert reader.current().signature is not SignatureStatus.VALID
    pubkey.unlink()
    reader, status, configured = _snapshot(env)
    assert not configured and "ed25519" in status.reason
    assert reader.current().signature is SignatureStatus.INVALID


def test_hmac_only_remains_valid(tmp_path, keys):
    private, raw, _ = keys
    path = write_doc(tmp_path, signed_doc(private, raw, include_ed25519=False))
    key = tmp_path / "hmac.key"
    key.write_bytes(b"shared")
    reader, status, configured = _snapshot({"AGENT_CREW_CEA_SNAPSHOT_PATH": str(path),
                                            "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE": str(key)})
    assert configured and "hmac-sha256" in status.reason
    assert reader.current().signature is SignatureStatus.VALID
