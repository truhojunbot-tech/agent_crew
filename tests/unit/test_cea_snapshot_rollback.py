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


def _signed(key, generation, *, tier="T0", produced_at="2026-09-27T00:00:00Z",
            signed_content_hash=False):
    body = {"generation": generation, "produced_at": produced_at,
            "decisions": [], "tier": tier}
    if signed_content_hash:
        content = {k: v for k, v in body.items() if k != "produced_at"}
        body["content_hash"] = (signed_content_hash if isinstance(signed_content_hash, str)
                                else "sha256:" + hashlib.sha256(json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()).hexdigest())
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


def test_same_signed_content_hash_survives_timestamp_only_reemit(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), clock=lambda: 1790467200)
    first = _signed(key, 2, signed_content_hash=True)
    second = _signed(key, 2, signed_content_hash=True,
                     produced_at="2026-09-27T00:10:00Z")
    assert first["content_hash"] == second["content_hash"]
    assert first["signature"] != second["signature"]
    snapshot.write_text(json.dumps(first))
    assert reader.current().rollback_status is None
    assert json.loads(mark.read_text())["content_hash"] == first["content_hash"]
    snapshot.write_text(json.dumps(second))
    enforcing = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=True, clock=lambda: 1790467200)
    assert enforcing.current().rollback_status is None
    assert enforcing.current().available


def test_same_generation_legacy_body_mark_migrates_to_signed_content_hash(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    doc = _signed(key, 2, signed_content_hash=True)
    snapshot.write_text(json.dumps(doc))
    body = {k: v for k, v in doc.items() if k != "signature"}
    legacy_hash = "sha256:" + hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    assert legacy_hash != doc["content_hash"]
    mark.write_text(json.dumps({"generation": 2, "content_hash": legacy_hash}))
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=True, clock=lambda: 1790467200)
    first = reader.current()
    assert first.signature is SignatureStatus.VALID
    assert first.rollback_status is None and first.available
    assert json.loads(mark.read_text()) == {
        "generation": 2, "content_hash": doc["content_hash"]}
    assert reader.current().rollback_status is None


def test_same_generation_unknown_mark_still_rolls_back(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    snapshot.write_text(json.dumps(_signed(key, 2, signed_content_hash=True)))
    mark.write_text(json.dumps({"generation": 2, "content_hash": "sha256:" + "0" * 64}))
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=True, clock=lambda: 1790467200)
    assert reader.current().rollback_status == "SNAPSHOT_ROLLBACK"
    assert json.loads(mark.read_text())["content_hash"] == "sha256:" + "0" * 64


def test_legacy_body_mark_at_newer_generation_still_rolls_back(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    doc = _signed(key, 2, signed_content_hash=True)
    snapshot.write_text(json.dumps(doc))
    body = {k: v for k, v in doc.items() if k != "signature"}
    legacy_hash = "sha256:" + hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    mark.write_text(json.dumps({"generation": 3, "content_hash": legacy_hash}))
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=True, clock=lambda: 1790467200)
    assert reader.current().rollback_status == "SNAPSHOT_ROLLBACK"
    assert json.loads(mark.read_text())["generation"] == 3


def test_changed_signed_content_hash_same_generation_is_rollback(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=False, clock=lambda: 1790467200)
    first = _signed(key, 2, signed_content_hash=True)
    changed = _signed(key, 2, tier="T1", signed_content_hash=True)
    assert first["content_hash"] != changed["content_hash"]
    snapshot.write_text(json.dumps(first))
    assert reader.current().rollback_status is None
    snapshot.write_text(json.dumps(changed))
    assert reader.current().rollback_status == "SNAPSHOT_ROLLBACK"


def test_malformed_present_signed_content_hash_fails_closed(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    snapshot, mark = tmp_path / "snapshot.json", tmp_path / "hwm.json"
    snapshot.write_text(json.dumps(_signed(key, 2, signed_content_hash="sha256:bad")))
    reader = CanonicalPolicySnapshotReader(str(snapshot), verifier=ed25519_verifier(raw),
        hwm_path=str(mark), hwm_enforce=False, clock=lambda: 1790467200)
    assert reader.current().signature is SignatureStatus.VALID
    assert reader.current().rollback_status == "SNAPSHOT_ROLLBACK"
    assert not mark.exists()
