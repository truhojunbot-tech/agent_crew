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

import calendar
import hashlib
import hmac
import json
import os
import time
from typing import Callable, Optional

from agent_crew.cea.intent import Intent
from agent_crew.cea.providers import PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import DecisionRev

DEFAULT_SNAPSHOT = "/home/truhojun/alfred/governance/control_policy_snapshot.json"
DEFAULT_MAX_AGE_SECONDS = 24 * 3600

Verifier = Callable[[bytes, dict], SignatureStatus]


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _epoch(ts) -> Optional[float]:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return float(calendar.timegm(time.strptime(ts.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            return None
    return None


def _strs(value) -> tuple[str, ...]:
    """A JSON list of ids as a tuple of strings; anything else is empty, never partial."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value)


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
                 env: Optional[dict] = None):
        e = os.environ if env is None else env
        self.path = path or (e.get("AGENT_CREW_CEA_POLICY_SNAPSHOT") or "").strip() or DEFAULT_SNAPSHOT
        self.verifier = verifier
        self.max_age_seconds = max_age_seconds
        self._clock = clock

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
        if self.verifier is not None and isinstance(sig, dict):
            try:
                status = self.verifier(canon, sig)
            except Exception:                    # noqa: BLE001 — a verifier that throws did not verify
                status = SignatureStatus.INVALID
        else:
            status = SignatureStatus.UNKEYED if isinstance(sig, dict) else SignatureStatus.UNSIGNED
        decisions, in_scope = [], []
        for d in doc.get("decisions") or ():
            if not isinstance(d, dict) or not d.get("decision_id") or not d.get("body_hash"):
                return self._unavailable()      # a malformed record is not a partial snapshot
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
                              runtimes=_strs(d.get("runtimes")))
            decisions.append(rev)
            if _in_scope(d.get("scope") or {}, intent):
                in_scope.append(rev)
        produced = _epoch(doc.get("produced_at"))
        age = None if produced is None else max(0.0, self._clock() - produced)
        fresh = age is not None and age <= self.max_age_seconds
        return PolicySnapshotRef(
            generation=generation, hash="sha256:" + hashlib.sha256(canon).hexdigest(),
            produced_at=produced, decisions=tuple(decisions), in_scope=tuple(in_scope),
            signature=status, age_seconds=age, available=fresh, tier=doc.get("tier"),
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
