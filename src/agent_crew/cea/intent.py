"""P4 intent identity and the caller principal (ADR Π P4, P5, §2.3 J1/J9, §7).

Pure types. No hashing is performed here: P4 names the *inputs* of
``intent_hash`` but not the canonical-JSON rules, and fixing those is an engine
decision after freeze. :class:`IntentIdentity` is exactly the P4 input tuple so
the engine has one place to hash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Caller:
    """The authenticated principal presented to ``authorize`` (P5, §6.5, J9).

    ``identity_status`` is ``UNVERIFIED`` until O21b; a per-adapter ``0400``
    token under one uid is tamper-evident only (P2a).
    """
    principal: str                        # caller class + instance, e.g. "cron:admitted_trigger"
    provenance: CallerProvenance
    identity_status: IdentityStatus = IdentityStatus.UNVERIFIED
    credential_kind: Optional[str] = None  # "adapter_token" | "broker_registered" | None
