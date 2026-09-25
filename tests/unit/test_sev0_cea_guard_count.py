"""SEV-0 CEA — ADR §11.2's ``<= 6`` guard count, as something that runs.

``docs/sev0/cea-guard-inventory.md`` counted the guards by hand and by grep. A
hand count is a claim about a moment; this file is the same count expressed as
static rules over ``src/agent_crew/``, so "the count is still <= 6" is checkable
on every commit rather than re-argued in review (Codex acceptance finding [2] at
``c18e092``: the inventory reported 7 against a limit of 6).

What is counted, per §11.2: **decision implementations**, not call sites. One
implementation counts once however many places call it. Two kinds of rows:

``TARGETS``
    the T1–T6 components the ADR *wants*. Each is asserted present — a target
    that vanished is as much a failure as an extra that appeared, because the
    count only means something if the components behind it exist.

``EXTRAS``
    probes for an independent decision implementation *outside* T1–T6. Each is a
    rule of the form "this symbol/comparison must not appear in these files".
    A probe that matches is an extra guard and is counted.

The assertion is ``len(TARGETS present) + len(EXTRAS matched) <= 6``.

⛔These probes are lexical on purpose. They are a tripwire, not a proof: they
  catch the decision coming back to a file the ADR moved it out of, which is the
  regression that actually happened here twice. They cannot see a decision
  re-implemented under a new name in a new file, and they do not claim to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "agent_crew"

ADR_MAX_GUARDS = 6
"""ADR §11.2: "6 or fewer". Contract 6cbce565, ``E11-ADR-DRAFT.md`` §11.2."""


def _read(relpath: str) -> str:
    path = SRC / relpath
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _strip_comments(source: str) -> str:
    """Drop ``#`` comments so a rule that *names* a forbidden symbol in prose —
    every one of these moves left a comment saying what moved — does not read as
    the symbol still being called."""
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


# ── T1–T6: the components the ADR keeps ─────────────────────────────────────

@dataclass(frozen=True)
class Target:
    id: str
    what: str
    relpath: str
    anchor: str          # a regex that must match the file

    def present(self) -> bool:
        return bool(re.search(self.anchor, _read(self.relpath), re.M))


TARGETS: tuple[Target, ...] = (
    Target("T1", "the admission engine's authorize()", "cea/engine.py",
           r"def authorize\("),
    Target("T2", "E4 capability lookup — the only matcher",
           "cea/input_providers/capability.py", r"def lookup\("),
    Target("T3", "the one receipt validator, behind five call sites",
           "cea/callsites.py", r"^VALIDATOR: ReceiptValidator = ContractReceiptValidator\(\)"),
    Target("T5", "G11 result-contract check", "pipeline.py",
           r"def artifact_gate_applies\("),
    Target("T6", "G_DT pane-topology delivery precondition", "queue.py",
           r"def defer_push_delivery\("),
)
"""T4 (the snapshot producer's ack ledger) is alfred-side and has no
implementation in this repo — fixture CXC-1 pins that it has none. It is
therefore not a row here: counting a component this repo does not implement
would inflate the very number the ADR bounds."""


# ── extras: an independent decision implementation outside T1–T6 ────────────

@dataclass(frozen=True)
class Extra:
    id: str                       # the §11.2 row it corresponds to
    what: str
    relpaths: tuple[str, ...]     # files that must NOT implement this decision
    pattern: str                  # what implementing it looks like
    moved_to: str                 # where the decision lives now
    scope: Optional[str] = None   # search only here, when the row is about a region

    def _regions(self, source: str) -> str:
        if self.scope is None:
            return source
        return "\n".join(m.group(0) for m in re.finditer(self.scope, source, re.S))

    def hits(self) -> tuple[str, ...]:
        found = []
        for relpath in self.relpaths:
            source = self._regions(_strip_comments(_read(relpath)))
            if re.search(self.pattern, source):
                found.append(relpath)
        return tuple(found)


EXTRAS: tuple[Extra, ...] = (
    Extra(
        "#12", "the P2 runtime_stop gates deciding what a runtime state permits",
        ("queue.py",),
        # The shape these methods used to have: `state != "ACTIVE"` as a verdict.
        r"""!=\s*['"]ACTIVE['"]|==\s*['"]ACTIVE['"]""",
        moved_to="cea.validator.runtime_state_verdict via cea.callsites.gate_runtime_state",
        # Scoped to the gate family the finding names (old `queue.py:1657-1663`
        # and its four call sites). Two `!= "ACTIVE"` comparisons survive
        # elsewhere in queue.py — successor suppression at RESULT, and the §8
        # requeue path — and they are deliberately out of this row: neither is a
        # P2 admission gate, and folding them in changes which work is
        # suppressed rather than where the decision lives. The inventory says so
        # in §9 rather than letting this probe imply they do not exist.
        scope=r"def _stop_active_\w+\(.*?(?=\n    def )"),
    Extra(
        "#14", "risk-tier classification/enforcement decided in the cascade",
        ("pipeline.py", "server.py"),
        r"\b(classify_task|risk_tier_enforcement_enabled|cascade_metadata|"
        r"effective_fix_round_cap|shadow_decision)\s*\(",
        moved_to="cea.cascade_contract.decide, called once on the admission path"),
)


# ── the count ───────────────────────────────────────────────────────────────

def _present_targets() -> tuple[Target, ...]:
    return tuple(t for t in TARGETS if t.present())


def _matched_extras() -> tuple[tuple[Extra, tuple[str, ...]], ...]:
    return tuple((e, hits) for e in EXTRAS if (hits := e.hits()))


@pytest.mark.parametrize("target", TARGETS, ids=[t.id for t in TARGETS])
def test_each_target_component_is_still_there(target: Target):
    """A count of 5 only means something if the 5 components exist."""
    assert target.present(), (
        f"{target.id} ({target.what}) is no longer at {target.relpath} matching "
        f"{target.anchor!r}. Either it moved — update the anchor — or a target "
        f"component was deleted, which is not a reduction in guards, it is a hole.")


@pytest.mark.parametrize("extra", EXTRAS, ids=[e.id for e in EXTRAS])
def test_no_extra_decision_implementation_came_back(extra: Extra):
    hits = extra.hits()
    assert not hits, (
        f"§11.2 {extra.id} — {extra.what} — is implemented again in "
        f"{', '.join(hits)}. That decision belongs to {extra.moved_to}; these "
        f"files consume it. Matched: {extra.pattern!r}")


def test_guard_count_is_within_the_adr_limit():
    """★ The acceptance check: ADR §11.2 wants 6 or fewer.

    Before this step the inventory counted 5 targets + 2 extras = 7. The extras
    were the ``runtime_stop`` comparison in ``queue.py`` (#12) and the risk-tier
    cascade in ``pipeline.py`` (#14); both are now relays to a single
    implementation, so the count is 5.
    """
    targets = _present_targets()
    extras = _matched_extras()
    total = len(targets) + len(extras)
    named_targets = ", ".join(t.id for t in targets)
    named_extras = ", ".join(f"{e.id} in {' '.join(h)}" for e, h in extras)
    assert total <= ADR_MAX_GUARDS, (
        f"guard count {total} > {ADR_MAX_GUARDS}: "
        f"{len(targets)} target components ({named_targets}) plus "
        f"{len(extras)} extra decision implementations ({named_extras})")


def test_the_count_is_exactly_what_the_inventory_documents():
    """The doc and the code say the same number, or this fails.

    ``docs/sev0/cea-guard-inventory.md`` is the artifact a reader trusts. If it
    and the probes disagree, one of them is stale — and a stale inventory is how
    a count of 7 got reported as closed once already.
    """
    doc = (SRC.parents[1] / "docs" / "sev0" / "cea-guard-inventory.md").read_text(encoding="utf-8")
    claimed = re.search(r"GUARD_COUNT\s*=\s*(\d+)", doc)
    assert claimed, ("the inventory must state its count machine-readably as "
                     "`GUARD_COUNT = <n>` so this test can compare against it")
    assert int(claimed.group(1)) == len(_present_targets()) + len(_matched_extras())


# ── the two reductions, at the level of behaviour ───────────────────────────

def test_runtime_state_verdicts_all_come_from_one_matrix():
    """#12: the queue relays; ``runtime_state_verdict`` decides.

    Pinned as behaviour, not only as a grep: the relay must reproduce the
    ``state != "ACTIVE"`` answer the queue used to compute for its two points,
    or the refactor silently changed when the fleet stops.
    """
    from agent_crew.cea.callsites import gate_runtime_state
    from agent_crew.cea.validator import ValidationPoint

    for point in (ValidationPoint.ENQUEUE, ValidationPoint.CLAIM):
        for state in ("ACTIVE", "DRAINING", "QUARANTINED", "STOPPED", "not-a-state"):
            expected = state == "ACTIVE"
            assert gate_runtime_state(state, point=point).proceed is expected, (
                f"{point.value} under {state}: the P6 relay disagrees with the "
                f"check queue.py used to implement inline")


def test_the_queue_only_relays_the_runtime_verdict():
    """#12: ``_stop_active_*`` must go through the gate, not through a comparison."""
    source = _strip_comments(_read("queue.py"))
    body = re.search(r"def _stop_active_in_txn\(.*?(?=\n    def )", source, re.S)
    assert body, "_stop_active_in_txn disappeared — update this rule deliberately"
    assert "_runtime_gate" in body.group(0), (
        "_stop_active_in_txn stopped relaying to the P6 gate")


def test_the_cascade_reads_the_contract_admission_stored():
    """#14: ``pipeline.py`` imports no decision from ``risk_tier``."""
    source = _strip_comments(_read("pipeline.py"))
    assert "from agent_crew.risk_tier import" not in source
    assert "import agent_crew.risk_tier" not in source
    assert "cascade_contract" in source, (
        "pipeline.py must read the stored contract; if it stopped, the cascade "
        "is deciding again somewhere else")


def test_admission_stores_the_contract_the_cascade_reads():
    """#14: the decision is made once, on the admission path, and persisted."""
    source = _strip_comments(_read("queue.py"))
    assert "_cea_cascade.decide(" in source, (
        "no admission-time cascade decision — pipeline.py would fall back to the "
        "floor for every task, which is safe but is not the contract")
    assert source.count("_cea_cascade.decide(") == 1, (
        "the review/test contract is decided in more than one place; that is the "
        "second implementation §11.2 #14 is about")
