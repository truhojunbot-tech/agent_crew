"""Canonical policy snapshot reader (§5.3, P5, O20) — alfred ``governance/control_policy_snapshot.json``.

One JSON document per generation::

    {"generation": 12, "produced_at": "2026-09-23T13:00:00Z", "tier": "T0",
     "decisions": [{"decision_id": "T0-1234", "body_hash": "...", "supersedes": [],
                    "principals": ["owner:hojun"], "build_commits": ["<sha>"],
                    "runtimes": ["agent_crew"],
                    "scope": {"project": "agent_crew", "work_class": "implement"}}],
     "review_test_matrix": {"implement": {"reviewer": true, "tester": true}},
     "human_gate_predicates": [{"project": "...", "state": "PENDING"}],
     "signature": {"alg": "...", "key_id": "...", "value": "..."}}

``hash`` is ``sha256:`` over the canonical JSON of everything except
``signature``. The signature hook is ``verifier(body_bytes, signature) ->
SignatureStatus``; with no verifier configured the snapshot is ``UNKEYED`` (a
``signature`` block present) or ``UNSIGNED`` (absent). The engine uses only
``VALID`` — so an unkeyed snapshot is an **UNVERIFIED input** and admission BLOCKs
(P7). That is the honest state until alfred's producer signs (O3).

Missing / unreadable / malformed file ⇒ ``available=False``. Older than
``max_age_seconds`` (O20) ⇒ still returned with ``age_seconds`` set and
``available=False``: a stale authority record is not a current one.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import fcntl
import re
import tempfile
from contextlib import contextmanager
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from agent_crew.cea.intent import Intent
from agent_crew.cea.providers import PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import DecisionRev

DEFAULT_SNAPSHOT = "/home/truhojun/alfred/governance/control_policy_snapshot.json"
DEFAULT_MAX_AGE_SECONDS = 24 * 3600

Verifier = Callable[[bytes, dict], SignatureStatus]


@contextmanager
def _directory_fd(path: str):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def check_high_water_mark(path: str, generation: int, content_hash: str, *,
                          bootstrap: bool = False) -> Optional[str]:
    """Compare and atomically advance a signed snapshot's generation.

    The owner installer alone may bootstrap an absent enforce mark. Locking the
    private directory serializes readers so an older concurrent read cannot
    replace a newer mark. An invalid or unreadable mark always fails closed.
    """
    directory = os.path.dirname(os.path.abspath(path))
    try:
        with _directory_fd(directory) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as fh:
                    old = json.load(fh)
                old_generation = old["generation"]
                old_hash = old["content_hash"]
                if type(old_generation) is not int or old_generation < 0 or not isinstance(old_hash, str) or not old_hash.startswith("sha256:"):
                    return "SNAPSHOT_ROLLBACK"
            except FileNotFoundError:
                if not bootstrap:
                    return "SNAPSHOT_ROLLBACK"
                old_generation, old_hash = -1, ""
            if generation < old_generation or (generation == old_generation and content_hash != old_hash):
                return "SNAPSHOT_ROLLBACK"
            if generation > old_generation:
                fd, temporary = tempfile.mkstemp(prefix=".snapshot-hwm-", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump({"generation": generation, "content_hash": content_hash}, fh, sort_keys=True)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, path)
                    os.fsync(lock)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return "SNAPSHOT_ROLLBACK"


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _epoch(ts) -> Optional[float]:
    if isinstance(ts, (int, float)):
        return float(ts) if math.isfinite(ts) else None
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _strs(value) -> tuple[str, ...]:
    """A JSON list of ids as a tuple of strings; anything else is empty, never partial."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value)


def _scope_project(scope) -> Optional[str]:
    """``scope.project`` as a string, or ``None`` when the record is not project-scoped."""
    project = scope.get("project") if isinstance(scope, dict) else None
    return None if project in (None, "*") else str(project)


def _in_scope(scope: dict, intent: Optional[Intent]) -> bool:
    if intent is None:
        return True
    ident = intent.identity
    want = {"project": ident.project,
            "work_class": getattr(ident.work_class, "value", ident.work_class),
            "capability_id": ident.capability_id, "repo": ident.target.repo}
    for k, v in (scope or {}).items():
        if k in want and v not in (None, "*") and v != want[k]:
            return False
    return True


class CanonicalPolicySnapshotReader:
    def __init__(self, path: Optional[str] = None, *, verifier: Optional[Verifier] = None,
                 max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS, clock=time.time,
                 env: Optional[dict] = None, hwm_path: Optional[str] = None,
                 hwm_enforce: bool = False):
        e = os.environ if env is None else env
        self.path = path or (e.get("AGENT_CREW_CEA_POLICY_SNAPSHOT") or "").strip() or DEFAULT_SNAPSHOT
        self.verifier = verifier
        self.max_age_seconds = max_age_seconds
        self._clock = clock
        self.hwm_path = hwm_path
        self.hwm_enforce = hwm_enforce

    def _unavailable(self) -> PolicySnapshotRef:
        return PolicySnapshotRef(generation=0, hash="", produced_at=None, decisions=(), available=False)

    def current(self, intent: Optional[Intent] = None) -> PolicySnapshotRef:
        try:
            with open(self.path, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8"))
            generation = int(doc["generation"])
        except (OSError, ValueError, KeyError, TypeError):
            return self._unavailable()
        if not isinstance(doc, dict):
            return self._unavailable()
        sig = doc.get("signature")
        body = {k: v for k, v in doc.items() if k != "signature"}
        canon = _canonical(body)
        if self.verifier is not None:
            if not isinstance(sig, dict):
                status = SignatureStatus.INVALID
            else:
                try:
                    status = self.verifier(canon, sig)
                except Exception:                # noqa: BLE001 — a verifier that throws did not verify
                    status = SignatureStatus.INVALID
        else:
            status = SignatureStatus.UNKEYED if isinstance(sig, dict) else SignatureStatus.UNSIGNED
        now = self._clock()
        content_hash = "sha256:" + hashlib.sha256(canon).hexdigest()
        signed_content_hash = body.get("content_hash")
        malformed_content_hash = ("content_hash" in body and not (
            isinstance(signed_content_hash, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", signed_content_hash)))
        hwm_hash = content_hash if signed_content_hash is None else signed_content_hash
        rollback_status = None
        if self.hwm_path and status is SignatureStatus.VALID:
            rollback_status = ("SNAPSHOT_ROLLBACK" if malformed_content_hash else
                               check_high_water_mark(self.hwm_path, generation, hwm_hash,
                                                     bootstrap=not self.hwm_enforce))
        elif self.hwm_path and self.hwm_enforce:
            rollback_status = "SNAPSHOT_ROLLBACK"
        if rollback_status and self.hwm_enforce:
            return PolicySnapshotRef(generation=generation, hash=content_hash,
                                     produced_at=None, decisions=(), signature=status,
                                     available=False, rollback_status=rollback_status)
        decisions, in_scope = [], []
        for d in doc.get("decisions") or ():
            if not isinstance(d, dict) or not d.get("decision_id") or not d.get("body_hash"):
                return self._unavailable()      # a malformed record is not a partial snapshot
            expiry = None
            if "expires_at" in d:
                expiry = _epoch(d["expires_at"])
                if expiry is None or expiry <= now:
                    continue  # Expiry is per decision, so other valid records remain usable.
            # ⛔`principals`, `build_commits` and `runtimes` are not decoration.
            #   Without them every record this reader produces has empty tuples,
            #   and `SnapshotLooseningAuthority` refuses conditions 3 and 4 for
            #   *every* record — so a correctly signed T0 restoration could never
            #   be granted through the production reader, only through a test
            #   fake that built DecisionRev directly. A verifier that structurally
            #   cannot say yes is not a verifier.
            rev = DecisionRev(decision_id=str(d["decision_id"]), body_hash=str(d["body_hash"]),
                              supersedes=_strs(d.get("supersedes")),
                              principals=_strs(d.get("principals")),
                              build_commits=_strs(d.get("build_commits")),
                              runtimes=_strs(d.get("runtimes")),
                              project=_scope_project(d.get("scope")), expires_at=expiry)
            decisions.append(rev)
            if _in_scope(d.get("scope") or {}, intent):
                in_scope.append(rev)
        produced = _epoch(doc.get("produced_at"))
        age = None if produced is None else max(0.0, now - produced)
        fresh = age is not None and age <= self.max_age_seconds
        return PolicySnapshotRef(
            generation=generation, hash=content_hash,
            produced_at=produced, decisions=tuple(decisions), in_scope=tuple(in_scope),
            signature=status, age_seconds=age, available=fresh, tier=doc.get("tier"),
            rollback_status=rollback_status,
            review_test_matrix=dict(doc.get("review_test_matrix") or {}),
            human_gate_predicates=tuple(p for p in doc.get("human_gate_predicates") or ()
                                        if isinstance(p, dict)))


def hmac_sha256_verifier(key: bytes) -> Verifier:
    """A :data:`Verifier` for the ``hmac-sha256`` signature block the engine writes.

    The snapshot producer is alfred, not this process, so this is the *reader*
    side of a shared secret: ``value`` must be HMAC-SHA256 over the canonical
    body under ``key``. Anything else — a different ``alg``, a missing value, a
    mismatch — is ``INVALID``, never ``UNKEYED``: we had a key and it did not
    check out, which is a stronger statement than "nobody checked".
    """
    def verify(body: bytes, signature: dict) -> SignatureStatus:
        if not isinstance(signature, dict) or signature.get("alg") != "hmac-sha256":
            return SignatureStatus.INVALID
        value = signature.get("value")
        if not isinstance(value, str) or not value:
            return SignatureStatus.INVALID
        expected = hmac.new(key, body, hashlib.sha256).hexdigest()
        return SignatureStatus.VALID if hmac.compare_digest(expected, value) \
            else SignatureStatus.INVALID
    return verify


def ed25519_verifier(public_key_bytes: bytes) -> Verifier:
    """Verify ``signature.ed25519`` over the canonical snapshot body.

    The optional cryptography dependency is imported only when this verifier is
    configured. A public key may be raw Ed25519 bytes or PEM SubjectPublicKeyInfo.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if len(public_key_bytes) == 32:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    else:
        public_key = serialization.load_pem_public_key(public_key_bytes)
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("snapshot public key is not Ed25519")
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(raw).hexdigest()[:16]

    def verify(body: bytes, signature: dict) -> SignatureStatus:
        nested = signature.get("ed25519") if isinstance(signature, dict) else None
        if not isinstance(nested, dict) or nested.get("key_id") != key_id:
            return SignatureStatus.INVALID
        value = nested.get("value")
        if not isinstance(value, str):
            return SignatureStatus.INVALID
        try:
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 64:
                return SignatureStatus.INVALID
            public_key.verify(decoded, body)
        except (ValueError, binascii.Error, InvalidSignature):
            return SignatureStatus.INVALID
        return SignatureStatus.VALID

    return verify
