"""P4 intent identity and the caller principal (ADR Π P4, P5, §2.3 J1/J9, §7).

Pure types. No hashing is performed here: P4 names the *inputs* of
``intent_hash`` but not the canonical-JSON rules, and fixing those is an engine
decision after freeze. :class:`IntentIdentity` is exactly the P4 input tuple so
the engine has one place to hash.
"""
from __future__ import annotations

import posixpath
import re
import weakref
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional


class WorkClass(str, Enum):
    """P4: "the task's function (implement/fix/review/test/merge/ops)".

    Cascade successors are distinct intents because their work class differs
    (P4 last bullet; §8). This is *not* the queue's ``task_type`` — ``fix`` is
    dispatched as an implement task today, ``merge``/``ops`` have no task type.
    """
    IMPLEMENT = "implement"
    FIX = "fix"
    REVIEW = "review"
    TEST = "test"
    MERGE = "merge"
    OPS = "ops"


IDENTITY_DEPENDENT_WORK_CLASSES = frozenset({WorkClass.OPS.value})
"""Work classes whose admission is a question about *who is asking* (§2.3 J9).

J9 asks who may do OPS. That judgement has no principal-invariant answer: the
whole content of it is which principal is entitled to act. So while
``caller_identity_status`` is ``UNVERIFIED`` — unconditionally, until the O21b
broker exists (P2a) — the engine has nothing to decide it with, and P2a's rule
applies: REVIEW or HUMAN_GATE, never ALLOW.

⛔This is the load-bearing half of the answer to codex's ``_mint_caller`` /
  registry-poisoning reproductions. An in-process Python closure is **not** an
  authentication boundary and this repo does not claim it is: a forged caller
  can be minted or injected into the mint registry by anything already running
  in this interpreter. What closes the hole is that a caller who does so gains
  nothing — the decision is identical for every principal while identity is
  unverified, so the forgery buys exactly what an honest UNVERIFIED caller
  already had.
"""


class CallerProvenance(str, Enum):
    """§3 ``caller_provenance``: the ingress adapter id (§2.2, §7.1).

    Recorded on every receipt; per P2a it is **never an admission input** until
    the caller-role boundary (O21b) exists.
    """
    CRON = "cron"                  # alfred admitted_trigger
    DIRECT = "direct"              # bare POST /tasks
    COORDINATOR = "coordinator"    # coordinator-created tasks (`coordinator_managed`)
    MANUAL = "manual"              # operator: crew run, curl
    CASCADE = "cascade"            # server-internal review/test/fix/merge successors
    RETRY = "retry"                # retry / requeue / recovery incl. _requeue_orphans
    WATCHDOG = "watchdog"          # watchdog redispatch


class IdentityStatus(str, Enum):
    """§3 ``executor_binding_status`` / ``caller_identity_status``.

    ``VERIFIED`` is written only by the P2a broker (P2a last paragraph). Under
    the shared uid every status is ``UNVERIFIED``.
    """
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class Target:
    """P4 ``target{repo, base_ref, scope_anchors[]}``.

    ``scope_anchors`` are the declared target paths / modules / config keys
    (§5.2) the engine matches against the registry's artefact anchors. Kept as
    a tuple so the identity is hashable and order-stable.
    """
    repo: str
    base_ref: str
    scope_anchors: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentIdentity:
    """The P4 input tuple of ``intent_hash``.

    ``intent_hash = sha256(canonical_json{project, work_class,
    target{repo, base_ref, scope_anchors[]}, capability_id | null,
    authority_decision_ids[]})``.

    ⛔The description text, ``task_id`` and ``operation_id`` are **not**
      members: rewording or a new opid does not make new work (P4; E10 4c,
      fixtures CX-4c / CX-P4a). ``authority_decision_ids`` *is* a member so a
      superseding decision record changes the intent (P4 ALREADY_COMPLETED
      exception).
    """
    project: str
    work_class: WorkClass
    target: Target
    capability_id: Optional[str] = None
    authority_decision_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Intent:
    """What an adapter hands the engine (§7.1 step 2), identity plus context.

    Only :attr:`identity` participates in ``intent_hash``. The rest is carried
    for the receipt (``task_id``), for J4 matching (``description``, until
    anchors are declared) and for provenance.
    """
    identity: IntentIdentity
    task_id: str
    task_type: str                       # queue task_type as received (§5.2: match runs for every type)
    description: str
    coordinator_id: Optional[str] = None  # §7.2: `coordinator_managed` requires a named principal
    shadow: bool = False                  # P2a: shadow work is caller-independent
    idempotency_key: Optional[str] = None  # P4: caller-supplied; same key ⇒ existing receipt
    parent_receipt_id: Optional[str] = None  # §8: cascades / retries name their lineage root
    extra: dict = field(default_factory=dict)  # provenance only; never an admission input


class Caller:
    """The authenticated principal presented to ``authorize`` (P5, §6.5, J9).

    ⛔**Sealed: this class has no usable constructor.** ``Caller(...)`` raises,
      and an instance forced into existence another way (``object.__new__``,
      ``copy``, unpickling) is not *minted* — :func:`is_authenticated_caller`
      answers ``False`` for it and :meth:`AuthorizationEngine.authorize` refuses
      it. The only producer is :mod:`agent_crew.cea.auth`, which holds the
      module-private :func:`_mint_caller`.

    Why the earlier shape was not authentication (codex P1 #4, re-review of
    ``cb01d49``): ``Caller`` was a plain frozen dataclass and the engine's test
    was ``credential_kind in ("adapter_token", "broker_registered")`` — a string
    the caller chose. So the in-process entry point accepted
    ``Caller(principal="attacker", provenance=DIRECT,
    identity_status=VERIFIED, credential_kind="broker_registered")`` and returned
    ALLOW for an OPS intent, with ``caller_identity_status="VERIFIED"`` on the
    receipt, although :mod:`agent_crew.cea.auth` has no broker producer at all.
    A field an attacker can set is not evidence; being *minted* is, because
    minting happens only after a credential matched.

    ``identity_status`` is therefore never taken from anybody: the authenticator
    derives it, and until the O21b broker exists it is unconditionally
    ``UNVERIFIED`` (P2a — a ``0600`` token under one shared uid is
    tamper-evident, not verified).
    """

    __slots__ = ("principal", "provenance", "identity_status", "credential_kind",
                 "__weakref__")

    def __init__(self, *args, **kwargs):
        raise TypeError(
            "Caller is minted by agent_crew.cea.auth after a credential matched; it "
            "cannot be constructed. Authenticate (StaticTokenAuthenticator / "
            "TokenFileAuthenticator) and use what you are given (J9, P2a)")

    def __setattr__(self, name, value):
        raise AttributeError("Caller is immutable once minted (J9)")

    def __delattr__(self, name):
        raise AttributeError("Caller is immutable once minted (J9)")

    # Identity, not value: two callers are the same caller only if they are the
    # same minted object. Value equality would let a forgery compare equal to a
    # real principal — and would break the mint registry, which is a set.
    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __repr__(self) -> str:
        return (f"Caller(principal={self.principal!r}, provenance={self.provenance!r}, "
                f"identity_status={self.identity_status!r}, "
                f"credential_kind={self.credential_kind!r})")

    def __reduce__(self):
        # Pickling would otherwise round-trip a Caller through a public API and
        # hand the copy back unminted — which then reads as an authenticated
        # object right up until authorize() refuses it. Fail where it happens.
        raise TypeError("a Caller does not serialise: authenticate on the receiving side (J9)")


def _seal_caller():
    """Close the mint over a registry so neither can be reached by name.

    The registry is weak: a Caller stops being authenticated exactly when the
    last reference to it is dropped, and a long-lived engine does not accumulate
    every principal that ever called it.
    """
    minted: "weakref.WeakSet[Caller]" = weakref.WeakSet()

    def mint(principal: str, provenance: CallerProvenance, credential_kind: str) -> "Caller":
        if not isinstance(principal, str) or not principal:
            raise ValueError("a minted Caller needs a principal")
        if not isinstance(provenance, CallerProvenance):
            raise TypeError("provenance must be a CallerProvenance, not a string from a peer")
        if not isinstance(credential_kind, str) or not credential_kind:
            raise ValueError("a minted Caller names the credential kind that produced it")
        caller = object.__new__(Caller)
        setattr_ = object.__setattr__
        setattr_(caller, "principal", principal)
        setattr_(caller, "provenance", provenance)
        # ⛔P2a: **derived here, never accepted.** One line, and it is the whole
        #   O21b seam: when a broker that independently verifies a spawn exists,
        #   it mints VERIFIED and nothing else in this file changes. Until then
        #   every caller — every credential kind, every provenance — is
        #   UNVERIFIED, so there is no input that produces VERIFIED anywhere.
        setattr_(caller, "identity_status", IdentityStatus.UNVERIFIED)
        setattr_(caller, "credential_kind", credential_kind)
        minted.add(caller)
        return caller

    def is_minted(obj) -> bool:
        return isinstance(obj, Caller) and obj in minted

    return mint, is_minted


_mint_caller, is_authenticated_caller = _seal_caller()
is_authenticated_caller.__doc__ = """True only for a Caller an authenticator minted.

The engine's J9 test (§7.1). Membership is by *identity*, so a forged object
that copies every field of a real principal still answers ``False``."""


# ═══════════════════════════════════════════════════════════════════════════
# scope-anchor canonicalisation (P4 identity, §5.2 registry matching)
# ═══════════════════════════════════════════════════════════════════════════

class InvalidScopeAnchor(ValueError):
    """A scope anchor that has no canonical form. Admission refuses it (P7)."""


_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):(//)?")
"""``scheme:`` / ``scheme://`` — the only thing that makes an anchor a non-path."""


def canonical_scope_anchor(anchor: str) -> str:
    """One spelling per target, so one target is one lineage.

    ⛔``intent_hash`` used to sort and de-duplicate anchors but never
      *canonicalise* them, so ``src/x.py`` and ``src/./x.py`` hashed differently
      and opened two live lineages for the same file (codex P1 #5). Set
      semantics do not help if the members are spelled freely: de-duplication
      compares the strings a caller chose.

    The rule, in two cases:

    **Paths** (no ``scheme:`` prefix) are normalised with :mod:`posixpath` —
    ``./`` dropped, repeated separators collapsed, ``a/../b`` resolved, a
    trailing ``/`` removed. A leading ``/`` is *preserved*: an absolute path and
    a relative one are genuinely different anchors and folding them would create
    collisions rather than remove them. Case is preserved, because POSIX paths
    are case-sensitive and lowercasing would alias two real files.

    **Non-path anchors** (``scheme:rest`` — config keys, module URNs, service
    refs) keep ``rest`` byte-for-byte and lowercase only the scheme, which RFC
    3986 already defines as case-insensitive. For the ``scheme://authority/path``
    form the authority is lowercased too (also case-insensitive) and the path
    part goes through the path rule. Anything after the scheme in the opaque
    form is the namespace's business, not ours — a config key may well be
    case-sensitive, and guessing would silently merge two distinct anchors.

    Parent traversal that escapes the anchor root (``../x``, ``a/../../x``) is
    rejected rather than clamped: it names something outside the declared scope,
    and clamping it to ``x`` would let a caller point at one path and have the
    registry match another.
    """
    if not isinstance(anchor, str) or not anchor.strip():
        raise InvalidScopeAnchor(f"scope anchor {anchor!r} is empty")
    raw = anchor.strip()
    m = _SCHEME.match(raw)
    if m is None:
        return _canonical_path(raw, raw)
    scheme, slashes = m.group(1).lower(), m.group(2)
    rest = raw[m.end():]
    if not slashes:
        # opaque form: `config:server.port`, `urn:acme:thing`
        if not rest:
            raise InvalidScopeAnchor(f"scope anchor {anchor!r} has a scheme and nothing else")
        return f"{scheme}:{rest}"
    authority, _, path = rest.partition("/")
    canonical_path = _canonical_path(path, raw) if path else ""
    return f"{scheme}://{authority.lower()}" + (f"/{canonical_path}" if canonical_path else "")


def _canonical_path(path: str, original: str) -> str:
    normalised = posixpath.normpath(path)
    # POSIX keeps exactly two leading slashes meaningful and normpath preserves
    # them; nothing in a repo anchor means that, and leaving it in would be one
    # more spelling of one path.
    if normalised.startswith("//") and not normalised.startswith("///"):
        normalised = normalised[1:]
    if normalised in (".", "") :
        raise InvalidScopeAnchor(f"scope anchor {original!r} normalises to nothing")
    if normalised == ".." or normalised.startswith("../"):
        raise InvalidScopeAnchor(
            f"scope anchor {original!r} traverses above its own root; an anchor must name "
            f"what it declares (§5.2)")
    return normalised


def canonical_anchors(anchors) -> tuple[str, ...]:
    """Canonicalise, de-duplicate and sort — in that order. Order matters:
    de-duplicating first would keep both spellings of one anchor."""
    return tuple(sorted({canonical_scope_anchor(a) for a in (anchors or ())}))


def canonical_identity(identity: "IntentIdentity") -> "IntentIdentity":
    """The identity as the engine and the E4 registry must both see it.

    Applied *before* hashing and before registry matching, so the two never
    disagree about which file was named."""
    target = replace(identity.target, scope_anchors=canonical_anchors(identity.target.scope_anchors))
    return replace(identity, target=target)
