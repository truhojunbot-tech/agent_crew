"""Signed snapshot generations cannot move behind the installed high-water mark."""
import base64
import hashlib
import time
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_crew.cea.input_providers.snapshot import (
    CanonicalPolicySnapshotReader, check_high_water_mark, ed25519_verifier,
)
from agent_crew.cea.providers import SignatureStatus


def _signed(key, generation, *, tier="T0"):
    body = {"generation": generation, "produced_at": "2026-09-27T00:00:00Z",
            "decisions": [], "tier": tier}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {**body, "signature": {"ed25519": {
        "key_id": hashlib.sha256(raw).hexdigest()[:16],
        "value": base64.b64encode(key.sign(canonical)).decode()}}}


def test_enforce_rollback_same_generation_conflict_and_forward(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=True, clock=lambda: 1790467200)
    snapshot.write_text(json.dumps(_signed(key, 2)))
    assert reader.current().rollback_status == "SNAPSHOT_ROLLBACK"  # no implicit bootstrap
    initial = reader.current()
    assert check_high_water_mark(str(mark), 2, initial.hash, bootstrap=True) is None
    assert reader.current().rollback_status is None
    snapshot.write_text(json.dumps(_signed(key, 1)))
    refused = reader.current()
    assert refused.rollback_status == "SNAPSHOT_ROLLBACK" and not refused.available
    snapshot.write_text(json.dumps(_signed(key, 2, tier="T1")))
    assert reader.current().rollback_status == "SNAPSHOT_ROLLBACK"
    snapshot.write_text(json.dumps(_signed(key, 3)))
    assert reader.current().rollback_status is None
    assert json.loads(mark.read_text())["generation"] == 3


def test_invalid_signature_never_advances_and_shadow_is_advisory(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    snapshot.write_text(json.dumps(_signed(key, 2)))
    shadow = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), clock=lambda: 1790467200)
    assert shadow.current().rollback_status is None
    snapshot.write_text(json.dumps(_signed(key, 1)))
    result = shadow.current()
    assert result.rollback_status == "SNAPSHOT_ROLLBACK" and result.available
    snapshot.write_text(json.dumps(_signed(key, 4)))
    doc = json.loads(snapshot.read_text())
    doc["tier"] = "tampered"
    snapshot.write_text(json.dumps(doc))
    assert shadow.current().signature is SignatureStatus.INVALID
    assert json.loads(mark.read_text())["generation"] == 2


def test_signed_owner_reuse_scope_is_preserved_and_tampering_invalidates_it(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path = tmp_path / "snapshot.json"
    body = {"generation": 7, "produced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "decisions": [{"decision_id": "OWNER-594", "body_hash": "a" * 32,
                           "scope": {"capabilities": ["reuse:other.capability"],
                                     "projects": ["agent_crew"],
                                     "capability_id": "reuse:other.capability",
                                     "project": "agent_crew"},
                           "intent_hash": "sha256:" + "b" * 64}]}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    doc = {**body, "signature": {"ed25519": {
        "key_id": hashlib.sha256(raw).hexdigest()[:16],
        "value": base64.b64encode(key.sign(canonical)).decode()}}}
    path.write_text(json.dumps(doc))
    reader = CanonicalPolicySnapshotReader(str(path), verifier=ed25519_verifier(raw))
    snapshot = reader.current()
    assert snapshot.signature is SignatureStatus.VALID
    assert snapshot.decisions[0].capabilities == ("reuse:other.capability",)
    assert snapshot.decisions[0].projects == ("agent_crew",)
    assert snapshot.decisions[0].scope_capability_id == "reuse:other.capability"
    assert snapshot.decisions[0].intent_hash == "sha256:" + "b" * 64
    doc["decisions"][0]["scope"]["projects"] = ["another_project"]
    path.write_text(json.dumps(doc))
    assert reader.current().signature is SignatureStatus.INVALID
