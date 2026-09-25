"""J7 review/test contract — decided once at admission, consumed by the cascade.

ADR §11.1 row 13 / §11.2 #14. The review/test contract is the snapshot's J7
``review_test_matrix``; ``risk_tier.py`` is an *input* to that decision, not a
second authority. Before this module, ``pipeline.py`` asked
``risk_tier_enforcement_enabled()`` and ``classify_task()`` again at every
cascade step, which meant:

- the same question was answered twice against config that may have moved
  between admission and the cascade (the same defect ``server.py`` already fixed
  for ``test_scope``, see its dispatch-path comment); and
- ``pipeline.py`` counted as an independent decision implementation in
  ``docs/sev0/cea-guard-inventory.md`` §3, which is what kept the guard count at
  7 against the ADR's ``<= 6``.

Now :func:`decide` runs **once**, in ``TaskQueue.enqueue_with_receipt`` — the
tail every §7 adapter's enqueue runs through, beside the T1 gate record it
already writes — and stores its answer on the row as ``context["cea_cascade"]``. The cascade reads it back with :func:`stored`
and never decides anything: ``pipeline.py`` imports nothing from
``agent_crew.risk_tier``, which
``tests/unit/test_sev0_cea_guard_count.py`` asserts statically.

⛔What this step does **not** do: raise the contract to the receipt's J7 floor.
  ``engine._j7_contract`` names a required reviewer/tester for essentially every
  ``implement`` work class (``REVIEW_FLOOR``), so honouring it as a floor would
  make Tier 0's implement-only cascade unreachable — a change to *which tasks
  get reviewed*, not to where the decision lives. That is a behaviour change and
  belongs to its own step with its own tests. The J7 fields are therefore
  recorded on the contract (``j7_reviewer``/``j7_tester``) so the disagreement is
  visible and measurable, and nothing here acts on them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from agent_crew.risk_tier import (
    TIER_0, TIER_1, TIER_2, TIER_3, cascade_metadata, classify_task,
    effective_fix_round_cap, risk_tier_enforcement_enabled, shadow_decision)

CONTEXT_KEY = "cea_cascade"
"""Where the admission-time answer lives on the task row."""


@dataclass(frozen=True)
class CascadeContract:
    """What admission decided about review/test for one task.

    ``source`` says where the answer came from, so a consumer can tell an
    admitted decision from the floor a legacy row falls back to:

    ``admission``
        read back from ``context[CONTEXT_KEY]`` — a row admitted since this
        module existed.
    ``floor``
        the row predates it (or predates receipts). The floor is the
        pre-risk-tier cascade: review and test everything, gate nothing. Under
        P7's asymmetry that is the safe direction — an unknown contract buys
        *more* independent scrutiny, never less.
    """
    enforced: bool = False
    tier: Optional[int] = None
    tier_source: Optional[str] = None
    needs_reviewer: bool = True
    needs_tester: bool = True
    review_mode: Optional[str] = None
    test_scope: Optional[str] = None
    test_scope_source: Optional[str] = None
    human_gate_required: bool = False
    metadata: dict = field(default_factory=dict)
    source: str = "floor"
    #: The receipt's J7 answer, recorded not enforced — see the module docstring.
    j7_reviewer: Optional[str] = None
    j7_tester: Optional[str] = None

    def fix_round_cap(self, ceiling: Optional[int]) -> int:
        """Return the live operator ceiling; quota-core owns round economics."""
        return max(0, ceiling) if ceiling is not None else 3

    def as_record(self) -> dict:
        return {"enforced": self.enforced, "tier": self.tier,
                "tier_source": self.tier_source, "needs_reviewer": self.needs_reviewer,
                "needs_tester": self.needs_tester, "review_mode": self.review_mode,
                "test_scope": self.test_scope, "test_scope_source": self.test_scope_source,
                "human_gate_required": self.human_gate_required,
                "metadata": dict(self.metadata), "source": "admission",
                "j7_reviewer": self.j7_reviewer, "j7_tester": self.j7_tester}

    @classmethod
    def from_record(cls, record: Mapping) -> "CascadeContract":
        return cls(
            enforced=bool(record.get("enforced")),
            tier=record.get("tier") if isinstance(record.get("tier"), int) else None,
            tier_source=record.get("tier_source"),
            needs_reviewer=bool(record.get("needs_reviewer", True)),
            needs_tester=bool(record.get("needs_tester", True)),
            review_mode=record.get("review_mode"),
            test_scope=record.get("test_scope"),
            test_scope_source=record.get("test_scope_source"),
            human_gate_required=bool(record.get("human_gate_required")),
            metadata=dict(record.get("metadata") or {}),
            source="admission",
            j7_reviewer=record.get("j7_reviewer"),
            j7_tester=record.get("j7_tester"))


def decide(description: str, context: Optional[Mapping],
           receipt: Optional[Mapping] = None) -> CascadeContract:
    """The **one** review/test-contract decision, made at admission.

    Called from the admission entry and nowhere else. ``receipt`` supplies the
    J7 half, which is **recorded** on the contract and deliberately not acted on
    here — see the module docstring for why that is a separate step.
    """
    ctx = context if isinstance(context, Mapping) else {}
    enforced = risk_tier_enforcement_enabled()
    if not enforced:
        contract = CascadeContract(enforced=False, source="admission")
    else:
        # A review→fix lineage created before Council #39 has no tier receipt.
        # Do not retroactively alter its already-running cap halfway through.
        if "fix_round" in ctx and "risk_tier" not in ctx:
            tier, metadata = TIER_2, {}
        else:
            metadata = cascade_metadata(description, ctx)
            tier = metadata["risk_tier"]
        contract = CascadeContract(
            enforced=True, tier=tier,
            tier_source=(metadata or {}).get("risk_tier_source"),
            # Tier 0 is intentionally implement-only. It remains observable via
            # its task/result and can still be manually reviewed by an operator.
            needs_reviewer=tier != TIER_0,
            needs_tester=tier != TIER_0,
            review_mode="adversarial" if tier == TIER_2 else None,
            # #272's tester consumes an explicit treatment rather than guessing
            # scope from the project/provider.
            test_scope="targeted" if tier == TIER_1 else None,
            test_scope_source="risk_tier" if tier == TIER_1 else None,
            # Tier 3 contains irreversible/external work: no new worker becomes
            # runnable until a human resolves the durable approval gate.
            human_gate_required=tier == TIER_3,
            metadata=dict(metadata or {}), source="admission")
    return _with_j7(contract, receipt)


def _with_j7(contract: CascadeContract, receipt: Optional[Mapping]) -> CascadeContract:
    """Record the receipt's J7 answer alongside the decision. Changes nothing else."""
    if not isinstance(receipt, Mapping):
        return contract
    from dataclasses import replace
    return replace(contract,
                   j7_reviewer=receipt.get("required_reviewer"),
                   j7_tester=receipt.get("required_tester"))


def stored(task) -> CascadeContract:
    """Read back what admission decided for ``task``. Decides nothing.

    A row with no stored contract gets the floor — see
    :class:`CascadeContract`'s ``source``.
    """
    context = getattr(task, "context", None)
    if not isinstance(context, Mapping):
        return CascadeContract()
    record = context.get(CONTEXT_KEY)
    if not isinstance(record, Mapping):
        return CascadeContract()
    return CascadeContract.from_record(record)


def record_shadow(queue, task, actual_action: str, ceiling: Optional[int] = None) -> None:
    """Append a best-effort counterfactual receipt without changing work.

    Lives here rather than in ``pipeline.py`` so the cascade has no reason to
    import ``risk_tier`` at all. It is a *recorder*: the tier it writes down is
    a counterfactual, and nothing reads it back to route work.
    """
    import logging
    try:
        context = task.context if isinstance(task.context, Mapping) else {}
        receipt = shadow_decision(task.description, context, task.task_id, actual_action, ceiling)
        receipts = context.get("risk_tier_shadow")
        receipts = list(receipts) if isinstance(receipts, list) else []
        if receipt not in receipts:
            queue.patch_context(task.task_id, {"risk_tier_shadow": receipts + [receipt]})
    except Exception:
        logging.getLogger(__name__).exception(
            "risk-tier shadow receipt failed for %s", getattr(task, "task_id", "unknown"))


__all__ = ["CONTEXT_KEY", "CascadeContract", "decide", "record_shadow", "stored",
           "TIER_0", "TIER_1", "TIER_2", "TIER_3",
           "classify_task", "effective_fix_round_cap"]
