"""Broker-owned Ed25519 dispatch receipts.

The queue database is writable by workers. Only the broker private key grants
authority; database rows and their claimed verification status do not.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_crew.cea.input_providers.snapshot import _canonical

DEFAULT_PRIVATE = "/opt/agent_crew-authz/receipt-signing.key"
DEFAULT_PUBLIC = "/opt/agent_crew-authz/receipt-signing.pub"
DISPATCH_BINDING = "signed_dispatch"
VOLATILE_CONTEXT = frozenset({"cea_enqueue", "cea_cascade", "push_refusals",
    "push_refusal_reason", "push_not_before", "test_lock_defer_count",
    "test_lock_first_deferred_at"})


def payload_hash(*, task_type: str, branch: str, description: str, context: dict) -> str:
    stable = {k: v for k, v in (context or {}).items() if k not in VOLATILE_CONTEXT}
    return "sha256:" + hashlib.sha256(_canonical({"task_type": task_type or "",
        "branch": branch or "", "description": description or "", "context": stable})).hexdigest()


def _key_id(public) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def load_private(path: str | Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = Path(path).read_bytes()
    key = (Ed25519PrivateKey.from_private_bytes(raw) if len(raw) == 32 else
           serialization.load_pem_private_key(raw, password=None))
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("receipt signing key is not Ed25519")
    return key


def load_public(path: str | Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = Path(path).read_bytes()
    key = (Ed25519PublicKey.from_public_bytes(raw) if len(raw) == 32 else
           serialization.load_pem_public_key(raw))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("receipt verification key is not Ed25519")
    return key


def sign(receipt: dict, private, *, payload: str, build_commit: str,
         lifetime_seconds: int = 3600) -> dict:
    """Sign the complete receipt after adding a fresh, single-use dispatch grant."""
    issued = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    binding = {"task_id": receipt["task_id"], "payload_hash": payload,
        "decision": receipt["decision"], "receipt_id": receipt["receipt_id"],
        "serving_build_commit": build_commit, "issued_at": issued,
        "expires_at": datetime.fromtimestamp(time.time() + lifetime_seconds, timezone.utc)
            .isoformat().replace("+00:00", "Z"), "nonce": secrets.token_hex(32)}
    receipt = dict(receipt)
    receipt["provenance"] = dict(receipt.get("provenance") or {}, **{DISPATCH_BINDING: binding})
    body = {k: v for k, v in receipt.items() if k != "signature"}
    receipt["signature"] = {"alg": "ed25519", "key_id": _key_id(private.public_key()),
        "value": base64.b64encode(private.sign(_canonical(body))).decode("ascii"),
        "status": "VERIFIED"}
    return receipt


def verify_signature(receipt: dict, public) -> bool:
    """Check the broker signature on any decision, including BLOCK receipts."""
    from agent_crew.cea.providers import SignatureStatus
    from agent_crew.cea.input_providers.snapshot import ed25519_verifier
    if not isinstance(receipt, dict):
        return False
    sig = receipt.get("signature") or {}
    provenance = receipt.get("provenance") or {}
    if not isinstance(sig, dict) or not isinstance(provenance, dict):
        return False
    binding = provenance.get(DISPATCH_BINDING) or {}
    if (sig.get("alg") != "ed25519" or sig.get("status") != "VERIFIED"
            or not isinstance(binding, dict)):
        return False
    # Reuse the snapshot's Ed25519 verifier and canonical JSON representation.
    nested = {"ed25519": {"key_id": sig.get("key_id"), "value": sig.get("value")}}
    from cryptography.hazmat.primitives import serialization
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {k: v for k, v in receipt.items() if k != "signature"}
    if ed25519_verifier(raw)(_canonical(body), nested) is not SignatureStatus.VALID:
        return False
    return True


def verify(receipt: dict, public, *, task_id: str, payload: str,
           receipt_id: str, now: float | None = None) -> tuple[bool, str | None]:
    """Check cryptographic validity, exact task binding, ALLOW and freshness."""
    if not verify_signature(receipt, public):
        return False, None
    binding = receipt["provenance"][DISPATCH_BINDING]
    if (receipt.get("decision") != "ALLOW" or receipt.get("task_id") != task_id
            or receipt.get("receipt_id") != receipt_id
            or binding.get("task_id") != task_id or binding.get("receipt_id") != receipt_id
            or binding.get("decision") != "ALLOW" or binding.get("payload_hash") != payload
            or not binding.get("serving_build_commit") or not binding.get("nonce")):
        return False, None
    try:
        issued = datetime.fromisoformat(binding["issued_at"].replace("Z", "+00:00")).timestamp()
        expires = datetime.fromisoformat(binding["expires_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return False, None
    current = time.time() if now is None else now
    if not (issued <= current < expires and expires > issued):
        return False, None
    return True, binding["nonce"]
